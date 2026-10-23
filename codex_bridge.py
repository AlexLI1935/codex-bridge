"""Codex Bridge: durable CLI delegation, with Codex retaining its native harness."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from codex_config import BridgeError, Config, EFFORTS, atomic_json, epoch, lock, read_json, utc
from codex_process import OwnedProcess
from codex_usage import Usage, gate, reset_details

VERSION = "1.0.0"
ACTIVE = {"QUEUED", "RUNNING"}


def executable():
    path = os.environ.get("CODEX_BRIDGE_CODEX") or shutil.which("codex")
    if not path or not Path(path).is_file():
        raise BridgeError("CODEX_NOT_FOUND: install Codex CLI or set CODEX_BRIDGE_CODEX")
    if os.name == "nt" and Path(path).suffix.lower() in (".cmd", ".bat"):
        raise BridgeError("NATIVE_EXECUTABLE_REQUIRED: set CODEX_BRIDGE_CODEX to codex.exe")
    return [str(Path(path).resolve())]


class Bridge:
    def __init__(self, home=None, executable=None, usage_reader=None, time_observer=None):
        self.config = Config(home)
        self.home = self.config.home
        self.config.load()
        self.executable = executable or globals()["executable"]()
        self.usage_reader = usage_reader
        self.time_observer = time_observer or self.config.observe

    def path(self, identifier):
        try:
            identifier = str(uuid.UUID(identifier))
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("INVALID_TASK_ID") from None
        return self.home / "tasks" / (identifier+".json")

    def guard(self, identifier):
        return self.path(identifier).with_suffix(".run.lock")

    def load(self, identifier):
        try:
            return read_json(self.path(identifier))
        except (OSError, ValueError):
            raise BridgeError("TASK_NOT_FOUND_OR_UNREADABLE") from None

    def save(self, task):
        with lock(self.path(task["task_id"]).with_suffix(".state.lock")):
            atomic_json(self.path(task["task_id"]), task)
        return task

    def usage(self):
        return self.usage_reader() if self.usage_reader else Usage(self.home, self.config.load(), self.executable).get()

    def create(self, prompt, workdir, mode="read_only", model=None, reasoning_effort=None,
               profile="default", timeout_seconds=3600, stall_seconds=300):
        c = self.config.load()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1_000_000:
            raise BridgeError("INVALID_PROMPT")
        directory = Path(workdir).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise BridgeError("INVALID_WORKDIR: an existing absolute directory is required")
        if mode not in ("read_only", "write", "full") or profile not in c["models"]:
            raise BridgeError("INVALID_MODE_OR_PROFILE")
        effort = reasoning_effort or c["models"][profile]["reasoning_effort"]
        if effort not in EFFORTS or not 0 < timeout_seconds <= 604800 or not 0 < stall_seconds <= 604800:
            raise BridgeError("INVALID_EXECUTION_OPTIONS")
        t = {"task_id": str(uuid.uuid4()), "prompt": prompt, "workdir": str(directory.resolve()),
             "mode": mode, "model": model or c["models"][profile]["model"], "reasoning_effort": effort,
             "timeout_seconds": timeout_seconds, "stall_seconds": stall_seconds,
             "created_at": utc(), "queued_at_epoch": time.time(), "started_at": None,
             "ended_at": None, "status": "QUEUED", "stage": "preflight", "attempt": 0,
             "codex_session_id": None, "exit_code": None, "usage_before": None, "usage_after": None}
        return self.save(t)

    def prepare_resume(self, identifier, prompt=None):
        self.status(identifier)  # recover a dead runner before testing resumability
        with lock(self.guard(identifier), timeout=0):
            t = self.load(identifier)
            if t["status"] in ACTIVE:
                raise BridgeError("BUSY: task already dispatched")
            if t["attempt"] and not t["codex_session_id"]:
                raise BridgeError("SESSION_REQUIRED: interrupted work cannot be replayed automatically")
            if t.get("cleanup_required"):
                raise BridgeError("OWNED_TREE_CHECK_REQUIRED: inspect interrupted runner children before resuming")
            t.update(status="QUEUED", stage="preflight", queued_at_epoch=time.time(), ended_at=None,
                     resume_prompt=prompt or "Continue the original task from the existing session. Report the result.")
            self.path(identifier).with_suffix(".cancel.json").unlink(missing_ok=True)
            return self.save(t)

    def launch(self, identifier):
        t = self.load(identifier)
        if t["status"] != "QUEUED":
            raise BridgeError("TASK_NOT_QUEUED")
        env = dict(os.environ, CODEX_BRIDGE_HOME=str(self.home), CODEX_BRIDGE_CODEX=self.executable[0])
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker", identifier],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, **kwargs)
        return t

    def cancel(self, identifier):
        t = self.load(identifier)
        atomic_json(self.path(identifier).with_suffix(".cancel.json"), {"requested_at": utc()})
        try:
            with lock(self.guard(identifier), timeout=0):
                t = self.load(identifier)
                if t["status"] in ACTIVE or t["status"] == "WAITING_USAGE_RESET":
                    t.update(status="CANCELLED", reason="CANCELLED", ended_at=utc(), stage="finished")
                    self.save(t)
        except BridgeError:
            pass  # the owning runner observes the durable request
        return self.status(identifier)

    def status(self, identifier):
        t = self.load(identifier)
        if t["status"] == "RUNNING" or (t["status"] == "QUEUED" and time.time()-t["queued_at_epoch"] > 30):
            try:
                with lock(self.guard(identifier), timeout=0):
                    t = self.load(identifier)
                    if t["status"] in ACTIVE:
                        t.update(status="NEEDS_INPUT", reason="RUNNER_INTERRUPTED", stage="finished", ended_at=utc(),
                                 cleanup_required=os.name != "nt")
                        self.save(t)
            except BridgeError:
                pass
        if t.get("report_path"):
            try:
                report = Path(t["report_path"]).read_bytes()
                receipt = read_json(Path(t["report_path"]).with_name("receipt.json"))
                t["report_validated"] = (receipt["task_id"] == t["task_id"] and receipt["attempt"] == t["attempt"]
                                         and receipt["report_sha256"] == hashlib.sha256(report).hexdigest())
            except (OSError, ValueError, KeyError):
                t["report_validated"] = False
        return t

    def execute(self, identifier):
        with lock(self.guard(identifier), timeout=0):
            t = self.load(identifier)
            if t["status"] != "QUEUED":
                raise BridgeError("TASK_NOT_QUEUED")
            cancellation = self.path(identifier).with_suffix(".cancel.json")
            if cancellation.exists():
                t.update(status="CANCELLED", reason="CANCELLED", ended_at=utc())
                return self.save(t)
            try:
                c = self.config.load()
                if not Path(t["workdir"]).is_dir():
                    raise BridgeError("INVALID_WORKDIR")
                t["time_observation"] = self.time_observer()
                t["usage_before"] = self.usage()
                decision = gate(t["usage_before"], self.config.load())
                if not decision["allowed"]:
                    t.update(decision)
                    t["status"] = "WAITING_USAGE_RESET" if decision["reason"] == "USAGE_THRESHOLD" else "NEEDS_INPUT"
                    t["reason"] = t["usage_before"].get("refresh_reason", decision["reason"])
                    t.update(stage="preflight_blocked", ended_at=utc())
                    return self.save(t)
                t.update(status="RUNNING", stage="executing", reason=None, started_at=utc(),
                         ended_at=None, attempt=t["attempt"]+1, reset_at_epoch=None, reset_at=None,
                         reset_local=None, resume_after_epoch=None, blocked_windows=[], allowed=True,
                         report_path=None, report_validated=False, pid=None)
                self.save(t)
                return self._run(t, cancellation)
            except (BridgeError, OSError, ValueError) as exc:
                # No raw child output or authentication data in diagnostic errors.
                t.update(status="NEEDS_INPUT", reason=str(exc) if isinstance(exc, BridgeError) else "RUNNER_ERROR",
                         stage="finished", ended_at=utc())
                return self.save(t)

    def _run(self, t, cancellation):
        identifier = t["task_id"]
        sandbox = {"read_only": "read-only", "write": "workspace-write", "full": "danger-full-access"}[t["mode"]]
        argv = self.executable+["exec"]
        if t["codex_session_id"]:
            argv += ["resume", t["codex_session_id"]]
        argv += ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"',
                 "-c", "sandbox_mode="+json.dumps(sandbox), "-c", 'sandbox_workspace_write.writable_roots=[]',
                 "-c", "model_reasoning_effort="+json.dumps(t["reasoning_effort"])]
        if t["model"]:
            argv += ["-m", t["model"]]
        argv += ["-"]
        t["command"] = argv  # prompt goes through stdin, never shell interpolation
        output = self.home / "results" / identifier / ("attempt-"+str(t["attempt"]))
        output.mkdir(parents=True, exist_ok=True)
        prompt = t.get("resume_prompt") if t["codex_session_id"] else t["prompt"]
        prompt += ("\n\nTask contract: work only within the specified working directory and permission mode. "
                   "Do not push, deploy, change system services or schedules unless explicitly requested by the caller. "
                   "Report changes, checks and unresolved issues honestly.")
        q = queue.Queue()
        errors, final = [], ""
        transport_warnings = []
        malformed, completed = False, False
        started, activity = time.monotonic(), time.monotonic()
        last_check, last_progress, previous_signature = started, started, None
        reason = None
        with (output/"stdout.jsonl").open("w", encoding="utf-8", newline="\n") as stdout, \
             (output/"stderr.log").open("w", encoding="utf-8", newline="\n") as stderr:
            with OwnedProcess(argv, cwd=t["workdir"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=stderr, text=True, encoding="utf-8", errors="replace") as owned:
                p = owned.p
                t.update(pid=p.pid, process_started_at=utc(), runner_pid=os.getpid())
                self.save(t)
                def reader():
                    for line in p.stdout:
                        q.put(line)
                    q.put(None)
                def writer():
                    try:
                        p.stdin.write(prompt)
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        try:
                            p.stdin.close()
                        except OSError:
                            pass
                reading = threading.Thread(target=reader, daemon=True)
                reading.start()
                writing = threading.Thread(target=writer, daemon=True)
                writing.start()
                eof = False
                exited = None
                while not (eof and p.poll() is not None):
                    now = time.monotonic()
                    if p.poll() is None and (cancellation.exists() or now-started >= t["timeout_seconds"]):
                        reason = "CANCELLED" if cancellation.exists() else "TIMEOUT"
                        owned.kill()
                    if p.poll() is not None:
                        if exited is None:
                            exited = now
                            owned.close()  # release inherited pipes from owned descendants
                        if now-exited > 3 and q.empty():
                            malformed = True
                            break
                    try:
                        line = q.get(timeout=0.1)
                    except queue.Empty:
                        if p.poll() is None and now-last_check >= min(10, t["stall_seconds"]):
                            last_check = now
                            signature = self._progress(t)
                            if signature != previous_signature:
                                last_progress, previous_signature = now, signature
                            t["stalled_suspected"] = (not completed and now-activity > t["stall_seconds"]
                                                       and now-last_progress > t["stall_seconds"])
                            t["stall_evidence"] = {"output_idle_seconds": now-activity,
                                                   "filesystem_idle_seconds": now-last_progress,
                                                   **signature}
                            self.save(t)
                        continue
                    if line is None:
                        eof = True
                        continue
                    activity = time.monotonic()
                    t["stalled_suspected"] = False
                    stdout.write(line)
                    stdout.flush()
                    try:
                        event = json.loads(line)
                        kind = event.get("type")
                        if kind == "thread.started":
                            session = str(uuid.UUID(event["thread_id"]))
                            if t["codex_session_id"] and session != t["codex_session_id"]:
                                raise ValueError("session changed")
                            t["codex_session_id"] = session
                            self.save(t)
                        elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                            final = event["item"]["text"]
                            if not isinstance(final, str):
                                final = ""
                                raise ValueError("invalid final message")
                        elif kind == "turn.completed":
                            completed = True
                        elif kind in ("error", "turn.failed") or (kind == "item.completed" and event.get("item", {}).get("type") == "error"):
                            message = str(event.get("message", event.get("item", {}).get("message", "")))
                            if message.startswith(("Reconnecting...", "Falling back from WebSockets to HTTPS transport.")):
                                transport_warnings.append(event)
                            else:
                                errors.append(event)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        malformed = True
                t["exit_code"] = p.wait()
                reading.join(timeout=2)
                writing.join(timeout=2)
                p.stdout.close()
        stderr_text = (output/"stderr.log").read_text(encoding="utf-8", errors="replace")[-65536:]
        error_text = json.dumps(errors + (transport_warnings if not completed else [])).lower() + (stderr_text.lower() if t["exit_code"] else "")
        quota = bool(re.search(r"usage[ _-]*(?:limit|exhaust)|quota[ _-]*(?:exhaust|exceed)|rate[ _-]*limit|limit_reached", error_text))
        auth = bool(re.search(r"auth_required|unauthorized|\b401\b|please (?:log|sign) in|not authenticated", error_text))
        t["usage_after"] = self.usage_reader() if self.usage_reader else Usage(self.home, self.config.load(), self.executable).read()
        if reason:
            t.update(status="CANCELLED" if reason == "CANCELLED" else "FAILED", reason=reason)
        elif auth:
            t.update(status="NEEDS_INPUT", reason="AUTH_REQUIRED")
        elif quota:
            decision = gate(t["usage_after"], self.config.load())
            resets = []
            def collect_reset(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k in ("reset_at", "resets_at", "resetsAt"):
                            try:
                                resets.append(epoch(v))
                            except (ValueError, TypeError, OverflowError):
                                pass
                        elif isinstance(v, (dict, list)):
                            collect_reset(v)
                elif isinstance(value, list):
                    for v in value:
                        collect_reset(v)
            collect_reset(errors)
            if decision.get("reset_at_epoch"):
                resets.append(decision["reset_at_epoch"])
            if resets:
                decision.update(reset_details(max(resets), self.config.load()))
            t.update(status="WAITING_USAGE_RESET", reason="USAGE_EXHAUSTED", reset_at_epoch=decision.get("reset_at_epoch"),
                     reset_at=decision.get("reset_at"), reset_local=decision.get("reset_local"),
                     blocked_windows=decision.get("blocked_windows", []), resume_after_epoch=decision.get("resume_after_epoch"))
        elif t["exit_code"] == 0 and completed and final and not malformed and not errors:
            t.update(status="COMPLETED", reason=None)
        else:
            t.update(status="FAILED", reason="INVALID_JSON_STREAM" if malformed else "INCOMPLETE_EXECUTION")
        report = output/"report.md"
        report.write_text(final or "No final response was produced.\n", encoding="utf-8", newline="\n")
        atomic_json(output/"receipt.json", {"task_id": identifier, "attempt": t["attempt"],
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "created_at": utc()})
        t.update(report_path=str(report), stage="finished", ended_at=utc(), report_validated=True,
                 recovered_transport_warnings=len(transport_warnings) if completed else 0)
        self.save(t)
        return t

    def _progress(self, t):
        """Cheap evidence only; a quiet process is never killed as a 'stall'."""
        newest, count, session_mtime = 0, 0, 0
        try:
            for root, directories, files in os.walk(t["workdir"]):
                directories[:] = [d for d in directories if d not in (".git", "node_modules", ".venv")]
                for filename in files:
                    newest = max(newest, (Path(root)/filename).stat().st_mtime_ns)
                    count += 1
                    if count >= 2000:
                        break
                if count >= 2000:
                    break
            if t["codex_session_id"]:
                sessions = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))/"sessions"
                for path in sessions.rglob("*"+t["codex_session_id"]+"*.jsonl"):
                    session_mtime = max(session_mtime, path.stat().st_mtime_ns)
        except OSError:
            pass
        return {"workspace_mtime_ns": newest, "session_mtime_ns": session_mtime, "sampled_files": count}


def main():
    p = argparse.ArgumentParser(description="Codex Bridge: governed, resumable local Codex delegation")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--refresh-time", action="store_true")
    sub.add_parser("environment")
    sub.add_parser("usage")
    sub.add_parser("usage-refresh")
    config = sub.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("show")
    config.add_parser("validate")
    setter = config.add_parser("set")
    setter.add_argument("key")
    setter.add_argument("value", help="JSON value (quote a model name as a JSON string)")
    run = sub.add_parser("run")
    run.add_argument("--workdir", required=True)
    content = run.add_mutually_exclusive_group(required=True)
    content.add_argument("--prompt")
    content.add_argument("--prompt-file")
    run.add_argument("--mode", choices=["read_only", "write", "full"], default="read_only")
    run.add_argument("--profile", choices=["default", "complex"], default="default")
    run.add_argument("--model")
    run.add_argument("--reasoning-effort", choices=EFFORTS)
    run.add_argument("--timeout-seconds", type=float, default=3600)
    run.add_argument("--stall-seconds", type=float, default=300)
    run.add_argument("--wait", action="store_true", help="run in foreground instead of background")
    for name in ("status", "resume", "cancel", "_worker"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task_id")
        if name == "resume":
            cmd.add_argument("--prompt")
            cmd.add_argument("--wait", action="store_true")
    args = p.parse_args()
    try:
        cfg = Config()
        if args.command == "config":
            result = cfg.set(args.key, json.loads(args.value)) if args.action == "set" else cfg.load()
        else:
            bridge = Bridge()
            if args.command == "init":
                result = {"version": VERSION, "home": str(bridge.home), "timezone": cfg.observe(args.refresh_time)}
            elif args.command == "environment":
                version = subprocess.run(bridge.executable+["--version"], capture_output=True, text=True, timeout=10,
                                         **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                result = {"version": VERSION, "python": sys.version.split()[0], "codex_version": version.stdout.strip(),
                          "codex": bridge.executable[0], "home": str(bridge.home)}
            elif args.command in ("usage", "usage-refresh"):
                usage = Usage(bridge.home, cfg.load(), bridge.executable)
                result = usage.read() if args.command == "usage" else usage.get(refresh=True)
            elif args.command == "run":
                prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
                result = bridge.create(prompt, args.workdir, args.mode, args.model, args.reasoning_effort,
                                       args.profile, args.timeout_seconds, args.stall_seconds)
                result = bridge.execute(result["task_id"]) if args.wait else bridge.launch(result["task_id"])
            elif args.command == "status":
                result = bridge.status(args.task_id)
            elif args.command == "cancel":
                result = bridge.cancel(args.task_id)
            elif args.command == "resume":
                bridge.prepare_resume(args.task_id, args.prompt)
                result = bridge.execute(args.task_id) if args.wait else bridge.launch(args.task_id)
            else:
                result = bridge.execute(args.task_id)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("status") in {"FAILED", "NEEDS_INPUT", "WAITING_USAGE_RESET", "CANCELLED"} else 0
    except (BridgeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, BridgeError) else "OPERATION_FAILED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
