"""Independent Codex quota reader. No authentication files are opened."""
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time

from codex_config import BridgeError, atomic_json, epoch, lock, read_json, utc
from codex_process import OwnedProcess

WINDOWS = {300: "five_hour", 10080: "weekly"}


def reset_details(reset, config):
    offset = config["timezone"]["observed_utc_offset_minutes"]
    local = datetime.fromtimestamp(reset, timezone(timedelta(minutes=offset))).isoformat() if offset is not None else None
    return {"reset_at_epoch": reset, "reset_at": utc(reset), "reset_local": local,
            "resume_after_epoch": reset+config["usage"]["reset_grace_seconds"]}


def normalize(raw, fetched_at, now=None, fresh_seconds=300, source="session"):
    now = time.time() if now is None else now
    result = {"status": "UNKNOWN", "source": source}
    try:
        fetched = epoch(fetched_at)
        if "rateLimitsByLimitId" in raw:
            raw = raw["rateLimitsByLimitId"].get("codex", {})
        elif "rateLimits" in raw:
            raw = raw["rateLimits"]
        if raw.get("limit_id", raw.get("limitId", "codex")) != "codex":
            return result
        for window in raw.values():
            if not isinstance(window, dict):
                continue
            duration = window.get("window_minutes", window.get("windowDurationMins"))
            if duration not in WINDOWS:
                continue
            used = window.get("used_percent", window.get("usedPercent"))
            reset = epoch(window.get("resets_at", window.get("resetsAt")))
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError()
            key = WINDOWS[duration]
            if key in result:
                raise ValueError()  # ambiguous duplicate duration
            result[key] = {"utilization_pct": used, "remaining_pct": 100-used,
                           "reset_at": utc(reset), "reset_at_epoch": reset}
        if not any(k in result for k in WINDOWS.values()):
            return result
        result["fetched_at"] = utc(fetched)
        result["fetched_at_epoch"] = fetched
        result["age_seconds"] = max(0, now-fetched)
        valid = (-2 <= now-fetched < fresh_seconds
                 and all(result[k]["reset_at_epoch"] > now for k in WINDOWS.values() if k in result))
        result["status"] = "FRESH" if valid else "STALE"
        result["missing_windows"] = [k for k in WINDOWS.values() if k not in result]
        return result
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return {"status": "UNKNOWN", "source": source, "reason": "INCOMPATIBLE_USAGE_DATA"}


def read_sessions(root, now=None, fresh_seconds=300):
    candidates = []
    try:
        files = sorted(Path(root).rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
        for path in files:
            with path.open("rb") as stream:
                bom = stream.read(2)
                size = path.stat().st_size
                start = max(0, size-4*1024*1024)
                if bom in (b"\xff\xfe", b"\xfe\xff"):
                    start -= start % 2
                    encoding = "utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"
                else:
                    encoding = "utf-8-sig"
                stream.seek(start)
                lines = stream.read().decode(encoding, errors="replace").splitlines()
            for line in lines:
                try:
                    event = json.loads(line.lstrip("\ufeff"))
                    payload = event.get("payload", {})
                    if event.get("type") == "event_msg" and payload.get("type") == "token_count" and "rate_limits" in payload:
                        candidates.append((epoch(event["timestamp"]), payload["rate_limits"]))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    if not candidates:
        return {"status": "UNKNOWN", "source": "session"}
    stamp, raw = max(candidates, key=lambda x: x[0])
    return normalize(raw, stamp, now, fresh_seconds)


def gate(snapshot, config):
    blocked = []
    if snapshot.get("status") != "FRESH" or any(k not in snapshot for k in WINDOWS.values()):
        return {"allowed": False, "reason": "USAGE_UNKNOWN", "blocked_windows": []}
    for key in WINDOWS.values():
        if snapshot[key]["utilization_pct"] >= config["usage"][key+"_max_pct"]:
            blocked.append(key)
    if not blocked:
        return {"allowed": True}
    reset = max(snapshot[k]["reset_at_epoch"] for k in blocked)
    return {"allowed": False, "reason": "USAGE_THRESHOLD", "blocked_windows": blocked,
            **reset_details(reset, config)}


def account_read(executable):
    with tempfile.TemporaryDirectory(prefix="codex-bridge-usage-") as directory:
        with OwnedProcess(executable+["app-server", "--listen", "stdio://"], cwd=directory,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8") as child:
            q = queue.Queue()
            def reader():
                for line in child.p.stdout:
                    try:
                        q.put(json.loads(line))
                    except ValueError:
                        continue
                q.put(None)
            reading = threading.Thread(target=reader, daemon=True)
            reading.start()
            def send(value):
                child.p.stdin.write(json.dumps(value)+"\n")
                child.p.stdin.flush()
            def reply(identifier):
                deadline = time.monotonic()+20
                while time.monotonic() < deadline:
                    item = q.get(timeout=max(0.01, deadline-time.monotonic()))
                    if item is None:
                        raise BridgeError("USAGE_INTERFACE_UNSUPPORTED")
                    if item.get("id") == identifier:
                        if "error" in item:
                            error = item["error"]
                            message = str(error.get("message", "")).lower()
                            if any(s in message for s in ("authentication required", "unauthorized", "not authenticated", "401")):
                                raise BridgeError("AUTH_REQUIRED")
                            raise BridgeError("USAGE_INTERFACE_UNSUPPORTED" if error.get("code") == -32601 else "USAGE_QUERY_FAILED")
                        return item["result"]
                raise BridgeError("USAGE_QUERY_TIMEOUT")
            try:
                send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex_bridge", "version": "1.0.0"}}})
                reply(1)
                send({"method": "initialized", "params": {}})
                send({"id": 2, "method": "account/rateLimits/read"})
                return reply(2)
            except queue.Empty:
                raise BridgeError("USAGE_QUERY_TIMEOUT") from None
            finally:
                child.kill()
                reading.join(timeout=2)


class Usage:
    def __init__(self, home, config, executable):
        self.home, self.config, self.executable = Path(home), config, executable
        self.cache = self.home / "runtime" / "usage.json"
        self.sessions = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))) / "sessions"

    def read(self):
        snapshots = [read_sessions(self.sessions, fresh_seconds=self.config["usage"]["fresh_seconds"])]
        try:
            cache = read_json(self.cache)
            snapshots.append(normalize(cache["windows"], cache["fetched_at_epoch"],
                                       fresh_seconds=self.config["usage"]["fresh_seconds"], source=cache["source"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return max(snapshots, key=lambda s: s.get("fetched_at_epoch", 0))

    def probe(self):
        with tempfile.TemporaryDirectory(prefix="codex-bridge-probe-") as directory:
            argv = self.executable+["exec", "--json", "--skip-git-repo-check", "-c", 'approval_policy="never"',
                                    "-c", 'sandbox_mode="read-only"', "-"]
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors, OwnedProcess(
                    argv, cwd=directory, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=errors, text=True) as child:
                try:
                    child.p.communicate("Reply exactly: OK. Do not use tools or modify files.", timeout=60)
                except subprocess.TimeoutExpired:
                    raise BridgeError("USAGE_PROBE_TIMEOUT") from None
                if child.p.returncode:
                    errors.seek(0)
                    message = errors.read()[-65536:].lower()
                    if any(s in message for s in ("401", "unauthorized", "please log in", "not authenticated")):
                        raise BridgeError("AUTH_REQUIRED")
                    raise BridgeError("USAGE_PROBE_FAILED")

    def refresh(self):
        with lock(self.home / "runtime" / "usage.lock", timeout=25):
            marker = self.home / "runtime" / "usage-query.json"
            now = time.time()
            try:
                previous = read_json(marker)
                if 0 <= now-previous["attempted_at_epoch"] < self.config["usage"]["fresh_seconds"]:
                    return {"reason": "USAGE_REFRESH_COOLDOWN"}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            atomic_json(marker, {"attempted_at_epoch": now})
            try:
                raw = account_read(self.executable)
            except BridgeError as exc:
                if str(exc) != "USAGE_INTERFACE_UNSUPPORTED":
                    return {"reason": str(exc)}
                self.probe()
                return {"source": "probe"}
            sample = normalize(raw, time.time(), source="account")
            if sample["status"] != "FRESH":
                return {"reason": "USAGE_UNKNOWN"}
            windows = {k: {"window_minutes": minutes,
                            "used_percent": sample[k]["utilization_pct"],
                            "resets_at": sample[k]["reset_at_epoch"]}
                       for minutes, k in WINDOWS.items() if k in sample}
            atomic_json(self.cache, {"windows": windows, "fetched_at_epoch": sample["fetched_at_epoch"], "source": "account"})
            return {"source": "account"}

    def get(self, refresh=False):
        current = self.read()
        if current["status"] == "FRESH" and not refresh:
            return current
        refreshed = self.refresh()
        current = self.read()  # never assume refresh succeeded
        if refreshed.get("reason"):
            current["refresh_reason"] = refreshed["reason"]
            if refreshed["reason"] == "AUTH_REQUIRED":
                current["status"] = "UNKNOWN"
        return current
