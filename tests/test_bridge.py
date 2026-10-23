import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_config import Config, BridgeError, utc, atomic_json
from codex_usage import normalize, read_sessions, Usage, gate, reset_details
from codex_bridge import Bridge
from codex_process import OwnedProcess


def limits(five=20, weekly=10, reset=None):
    reset = reset or time.time() + 3600
    return {"limit_id": "codex", "arbitrary_slot": {
        "window_minutes": 300, "used_percent": five, "resets_at": reset},
        "another_slot": {"window_minutes": 10080, "used_percent": weekly,
                         "resets_at": reset + 7200}}


def snapshot(five=20, weekly=10):
    return normalize(limits(five, weekly), time.time())


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.cfg = Config(self.home)
        self.cfg.load()

    def tearDown(self):
        self.tmp.cleanup()


class ConfigTests(Isolated):
    def test_default(self):
        self.assertEqual(self.cfg.load()["usage"]["five_hour_max_pct"], 90)
        self.assertTrue((self.home / "config.json").is_file())

    def test_roundtrip(self):
        self.cfg.set("usage.weekly_max_pct", 92)
        self.assertEqual(Config(self.home).load()["usage"]["weekly_max_pct"], 92)

    def test_invalid_values(self):
        for value in [-1, 101, float("nan"), True, "90"]:
            with self.subTest(value=value), self.assertRaises(BridgeError):
                self.cfg.set("usage.five_hour_max_pct", value)

    def test_secret_and_unknown_fields_rejected(self):
        with self.assertRaises(BridgeError):
            self.cfg.set("accessToken", "never-store")

    def test_malformed(self):
        (self.home / "config.json").write_text("{bad")
        with self.assertRaises(BridgeError):
            self.cfg.load()

    def test_time_first_and_cached(self):
        with patch("codex_config.system_time", return_value=(180, "os-label")), \
             patch("codex_config.external_utc", return_value=time.time()) as probe:
            a = self.cfg.observe()
            b = self.cfg.observe()
            self.assertEqual(a["observed_utc_offset_minutes"], 180)
            self.assertEqual(a, b)
            self.assertEqual(probe.call_count, 1)

    def test_time_explicit_refresh(self):
        with patch("codex_config.external_utc", side_effect=lambda: time.time()) as probe:
            self.cfg.observe()
            self.cfg.observe(force=True)
            self.assertEqual(probe.call_count, 2)

    def test_timezone_change(self):
        with patch("codex_config.system_time", side_effect=[(180, "a"), (180, "b")]), \
             patch("codex_config.external_utc", side_effect=lambda: time.time()) as probe:
            self.cfg.observe()
            self.cfg.observe()
            self.assertEqual(probe.call_count, 2)

    def test_offset_change(self):
        with patch("codex_config.system_time", side_effect=[(60, "a"), (120, "a")]), \
             patch("codex_config.external_utc", side_effect=lambda: time.time()) as probe:
            self.cfg.observe()
            self.assertEqual(self.cfg.observe()["observed_utc_offset_minutes"], 120)
            self.assertEqual(probe.call_count, 2)

    def test_clock_skew(self):
        with patch("codex_config.external_utc", return_value=time.time() - 1000):
            with self.assertRaisesRegex(BridgeError, "CLOCK_SKEW"):
                self.cfg.observe()

    def test_clock_discontinuity_reprobes(self):
        with patch("codex_config.external_utc", side_effect=lambda: time.time()) as probe:
            self.cfg.observe()
            path = self.home / "runtime" / "time-baseline.json"
            baseline = json.loads(path.read_text())
            baseline["epoch"] -= 1000
            atomic_json(path, baseline)
            self.cfg.observe()
            self.assertEqual(probe.call_count, 2)

    def test_failed_time_probe_is_unverified(self):
        with patch("codex_config.external_utc", side_effect=OSError()):
            self.assertEqual(self.cfg.observe()["status"], "UNVERIFIED")
            self.assertIsNone(self.cfg.load()["timezone"]["observed_utc_offset_minutes"])

    def test_replace_retries_transient_sharing_violation(self):
        target=self.root / "atomic.json"
        atomic_json(target,{"value":1})
        replace=os.replace
        attempted=[]
        def transient(source,destination):
            attempted.append(1)
            if len(attempted)==1:
                raise PermissionError("transient sharing violation")
            replace(source,destination)
        with patch("codex_config.os.replace",side_effect=transient):
            atomic_json(target,{"value":2})
        self.assertEqual(json.loads(target.read_text()),{"value":2})

    @unittest.skipUnless(os.name=="nt","Windows delete-sharing regression")
    def test_replace_waits_for_existing_windows_reader(self):
        target=self.root / "atomic.json"
        atomic_json(target,{"value":1})
        errors=[]
        def write():
            try:
                atomic_json(target,{"value":2})
            except OSError as exc:
                errors.append(exc)
        with target.open("r") as reader:
            worker=threading.Thread(target=write)
            worker.start()
            time.sleep(0.12)
            self.assertEqual(json.load(reader),{"value":1})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(json.loads(target.read_text()),{"value":2})


class UsageTests(Isolated):
    def test_fresh_and_durations(self):
        s = snapshot()
        self.assertEqual(s["status"], "FRESH")
        self.assertEqual(s["five_hour"]["utilization_pct"], 20)
        self.assertEqual(s["weekly"]["utilization_pct"], 10)

    def test_stale(self):
        self.assertEqual(normalize(limits(), time.time() - 301)["status"], "STALE")

    def test_unknown(self):
        self.assertEqual(normalize({}, time.time())["status"], "UNKNOWN")

    def test_one_window(self):
        raw = limits()
        del raw["another_slot"]
        s = normalize(raw, time.time())
        self.assertEqual(s["status"], "FRESH")
        self.assertNotIn("weekly", s)
        self.assertEqual(gate(s, self.cfg.load())["reason"], "USAGE_UNKNOWN")

    def test_thresholds(self):
        for five, weekly, allowed in [(89, 94, True), (90, 94, False), (89, 95, False)]:
            self.assertEqual(gate(snapshot(five, weekly), self.cfg.load())["allowed"], allowed)

    def test_configurable_and_later_reset(self):
        self.cfg.set("usage.five_hour_max_pct", 95)
        self.assertTrue(gate(snapshot(94, 94), self.cfg.load())["allowed"])
        s = snapshot(96, 96)
        g = gate(s, self.cfg.load())
        self.assertEqual(g["blocked_windows"], ["five_hour", "weekly"])
        self.assertEqual(g["reset_at_epoch"], s["weekly"]["reset_at_epoch"])

    def test_past_reset_and_future_snapshot(self):
        self.assertEqual(normalize(limits(reset=time.time()-8000), time.time())["status"], "STALE")
        self.assertNotEqual(normalize(limits(), time.time()+1000)["status"], "FRESH")

    def test_malformed_jsonl_and_schema_change(self):
        p = self.root / "sessions" / "session.jsonl"
        p.parent.mkdir()
        p.write_text('broken\n' + json.dumps({"timestamp": utc(), "type": "event_msg",
                     "payload": {"type": "token_count", "rate_limits": limits()}}) + '\n{partial')
        self.assertEqual(read_sessions(p.parent)["status"], "FRESH")
        p.write_text(json.dumps({"type": "new_schema", "payload": limits()}))
        self.assertEqual(read_sessions(p.parent)["status"], "UNKNOWN")

    def test_account_shape(self):
        raw = {"rateLimitsByLimitId": {"codex": {"some": {
            "windowDurationMins": 300, "usedPercent": 12, "resetsAt": time.time()+1000}}}}
        self.assertEqual(normalize(raw, time.time())["five_hour"]["utilization_pct"], 12)

    def test_wrong_account_bucket(self):
        self.assertEqual(normalize({"rateLimitsByLimitId":{"another":limits()}},time.time())["status"],"UNKNOWN")

    def test_invalid_percent(self):
        for value in [True, -1, 101, float("nan"), "50"]:
            self.assertEqual(normalize(limits(value),time.time())["status"],"UNKNOWN")

    def test_latest_incompatible_event_not_replaced_with_old(self):
        p = self.root / "sessions" / "s.jsonl"
        p.parent.mkdir()
        events = [{"timestamp":utc(time.time()-10),"type":"event_msg","payload":{"type":"token_count","rate_limits":limits()}},
                  {"timestamp":utc(),"type":"event_msg","payload":{"type":"token_count","rate_limits":{"unknown_new_format":{}}}}]
        p.write_text('\n'.join(map(json.dumps,events)))
        self.assertEqual(read_sessions(p.parent)["status"],"UNKNOWN")

    def test_utf16_snapshot(self):
        p=self.root / "s.jsonl"
        p.write_text(json.dumps({"timestamp":utc(),"type":"event_msg","payload":{"type":"token_count","rate_limits":limits()}}),encoding="utf-16")
        self.assertEqual(read_sessions(self.root)["status"],"FRESH")

    def test_real_refresh_cache_and_cooldown(self):
        u=Usage(self.home,self.cfg.load(),[sys.executable])
        u.sessions=self.root / "empty"
        with patch("codex_usage.account_read",return_value=limits()) as rpc:
            self.assertEqual(u.get()["status"],"FRESH")
            self.assertEqual(u.get(refresh=True)["status"],"FRESH")
            self.assertEqual(rpc.call_count,1)
        self.assertEqual(u.read()["source"],"account")

    def test_unsupported_refresh_falls_back_then_rereads(self):
        u=Usage(self.home,self.cfg.load(),[sys.executable])
        u.sessions=self.root / "sessions"
        def fake_probe():
            u.sessions.mkdir()
            (u.sessions / "s.jsonl").write_text(json.dumps({"timestamp":utc(),"type":"event_msg","payload":{"type":"token_count","rate_limits":limits()}}))
        with patch("codex_usage.account_read",side_effect=BridgeError("USAGE_INTERFACE_UNSUPPORTED")), patch.object(u,"probe",side_effect=fake_probe) as probe:
            self.assertEqual(u.get()["status"],"FRESH")
            probe.assert_called_once()

    def test_auth_refresh_never_probes(self):
        u=Usage(self.home,self.cfg.load(),[sys.executable])
        u.sessions=self.root / "empty"
        with patch("codex_usage.account_read",side_effect=BridgeError("AUTH_REQUIRED")), patch.object(u,"probe") as probe:
            sample=u.get()
            self.assertEqual(sample["status"],"UNKNOWN")
            self.assertEqual(sample["refresh_reason"],"AUTH_REQUIRED")
            probe.assert_not_called()

    def test_display_only_changes_local_text(self):
        c=self.cfg.load()
        c["timezone"]["observed_utc_offset_minutes"]=180
        a=reset_details(2000000000,c)
        c["timezone"]["observed_utc_offset_minutes"]=480
        b=reset_details(2000000000,c)
        self.assertEqual(a["reset_at_epoch"],b["reset_at_epoch"])
        self.assertNotEqual(a["reset_local"],b["reset_local"])

    def test_refresh_then_reread(self):
        u = Usage(self.home, self.cfg.load(), [sys.executable])
        with patch.object(u, "read", side_effect=[{"status": "UNKNOWN"}, snapshot()]), \
             patch.object(u, "refresh", return_value={}) as refresh:
            self.assertEqual(u.get()["status"], "FRESH")
            refresh.assert_called_once()

    def test_fresh_skips_refresh(self):
        u = Usage(self.home, self.cfg.load(), [sys.executable])
        with patch.object(u, "read", return_value=snapshot()), patch.object(u, "refresh") as refresh:
            u.get()
            refresh.assert_not_called()


FAKE = r'''
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
session = sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else "11111111-1111-4111-8111-111111111111"
def out(d): print(json.dumps(d), flush=True)
out({"type":"thread.started","thread_id":session})
if "SLEEP" in prompt: time.sleep(20)
if "SHORT" in prompt: time.sleep(0.5)
if "MALFORMED" in prompt: print("broken",flush=True)
if "BADFINAL" in prompt:
    out({"type":"item.completed","item":{"type":"agent_message","text":["bad"]}})
    out({"type":"turn.completed"})
    sys.exit(0)
if "RECONNECT" in prompt:
    out({"type":"error","message":"Reconnecting... 2/5 (temporary transport failure)"})
    out({"type":"item.completed","item":{"type":"error","message":"Falling back from WebSockets to HTTPS transport."}})
if "QUOTA" in prompt:
    error={"code":"usage_limit_reached","message":"Usage limit reached"}
    if "RESET" in prompt: error["resets_at"] = 2000000000
    out({"type":"turn.failed","error":error})
    sys.exit(1)
if "AUTH" in prompt:
    out({"type":"error","message":"401 Unauthorized; please log in"})
    sys.exit(1)
if "NOFINAL" not in prompt:
    out({"type":"item.completed","item":{"type":"agent_message","text":"Complete"}})
out({"type":"turn.completed","usage":{}})
sys.exit(1 if "NONZERO" in prompt else 0)
'''


class RunTests(Isolated):
    def setUp(self):
        super().setUp()
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE)
        self.bridge = Bridge(self.home, executable=[sys.executable, str(self.fake)],
                             usage_reader=lambda: snapshot(), time_observer=lambda: {})

    def run_task(self, prompt="OK", **kwargs):
        t = self.bridge.create(prompt, str(self.root), **kwargs)
        return self.bridge.execute(t["task_id"])

    def test_normal_completion_and_session(self):
        t = self.run_task()
        self.assertEqual(t["status"], "COMPLETED")
        self.assertEqual(t["codex_session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertTrue(self.bridge.status(t["task_id"])["report_validated"])

    def test_nonzero(self):
        self.assertEqual(self.run_task("NONZERO")["status"], "FAILED")

    def test_recovered_transport(self):
        t = self.run_task("RECONNECT")
        self.assertEqual(t["status"], "COMPLETED")
        self.assertEqual(t["recovered_transport_warnings"], 2)

    def test_final_json_parse(self):
        self.assertEqual(self.run_task("MALFORMED")["status"], "FAILED")
        self.assertEqual(self.run_task("NOFINAL")["status"], "FAILED")
        self.assertEqual(self.run_task("BADFINAL")["status"], "FAILED")

    def test_timeout(self):
        t = self.run_task("SLEEP", timeout_seconds=0.3)
        self.assertEqual(t["reason"], "TIMEOUT")
        self.assertEqual(t["status"], "FAILED")

    def test_auth(self):
        self.assertEqual(self.run_task("AUTH")["reason"], "AUTH_REQUIRED")

    def test_native_resume(self):
        t = self.run_task()
        self.bridge.prepare_resume(t["task_id"], "boundary")
        after = self.bridge.execute(t["task_id"])
        self.assertEqual(after["status"], "COMPLETED")
        self.assertEqual(after["attempt"], 2)
        self.assertEqual(after["codex_session_id"], t["codex_session_id"])
        self.assertIn("resume", after["command"])

    def test_missing_session(self):
        t = self.run_task()
        t["codex_session_id"] = None
        self.bridge.save(t)
        with self.assertRaisesRegex(BridgeError, "SESSION_REQUIRED"):
            self.bridge.prepare_resume(t["task_id"], "continue")

    def test_midrun_limit(self):
        t = self.run_task("QUOTA")
        self.assertEqual(t["status"], "WAITING_USAGE_RESET")
        self.assertIsNotNone(t["codex_session_id"])
        self.assertIsNone(t["reset_at_epoch"])

    def test_structured_limit_reset(self):
        t = self.run_task("QUOTA RESET")
        self.assertEqual(t["reset_at_epoch"], 2000000000)

    def test_wait_and_fresh_recheck(self):
        self.bridge.usage_reader = lambda: snapshot(90, 10)
        t = self.run_task()
        self.assertEqual(t["status"], "WAITING_USAGE_RESET")
        self.assertEqual(t["attempt"], 0)
        self.bridge.prepare_resume(t["task_id"], None)
        self.assertEqual(self.bridge.execute(t["task_id"])["status"], "WAITING_USAGE_RESET")
        self.bridge.usage_reader = lambda: snapshot()
        self.bridge.prepare_resume(t["task_id"], None)
        self.assertEqual(self.bridge.execute(t["task_id"])["status"], "COMPLETED")

    def test_resume_after_exhaustion(self):
        t = self.run_task("QUOTA")
        self.bridge.prepare_resume(t["task_id"], "continue")
        t = self.bridge.execute(t["task_id"])
        self.assertEqual(t["status"], "COMPLETED")
        self.assertIn("resume", t["command"])

    def test_invalid_receipt(self):
        t = self.run_task()
        Path(t["report_path"]).write_text("tampered")
        self.assertFalse(self.bridge.status(t["task_id"])["report_validated"])

    def test_invalid_workdir(self):
        with self.assertRaises(BridgeError):
            self.bridge.create("OK", str(self.root / "missing"))

    def test_modes(self):
        for mode, sandbox in [("read_only", "read-only"), ("write", "workspace-write"), ("full", "danger-full-access")]:
            t = self.run_task(mode=mode)
            self.assertIn('sandbox_mode="'+sandbox+'"', t["command"])
            self.assertIn('approval_policy="never"', t["command"])

    def test_utc_reset_invariant(self):
        self.bridge.usage_reader = lambda: snapshot(90, 95)
        t = self.run_task()
        self.cfg.set("timezone.observed_utc_offset_minutes", 480)
        self.assertEqual(self.bridge.status(t["task_id"])["reset_at_epoch"], t["reset_at_epoch"])

    def test_restart_recovery(self):
        t = self.bridge.create("OK", str(self.root))
        t.update(status="RUNNING", attempt=1, codex_session_id="11111111-1111-4111-8111-111111111111")
        self.bridge.save(t)
        self.assertEqual(self.bridge.status(t["task_id"])["reason"], "RUNNER_INTERRUPTED")

    def test_duplicate_dispatch_and_cancel(self):
        t = self.bridge.create("SLEEP", str(self.root))
        result = []
        th = threading.Thread(target=lambda: result.append(self.bridge.execute(t["task_id"])))
        th.start()
        for _ in range(100):
            if self.bridge.load(t["task_id"])["status"] == "RUNNING":
                break
            time.sleep(0.02)
        with self.assertRaises(BridgeError):
            self.bridge.execute(t["task_id"])
        self.bridge.cancel(t["task_id"])
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(result[0]["status"], "CANCELLED")

    def test_independent_tasks_concurrent(self):
        tasks = [self.bridge.create("SHORT", str(self.root)) for _ in range(2)]
        results = []
        threads = [threading.Thread(target=lambda t=t: results.append(self.bridge.execute(t["task_id"]))) for t in tasks]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
        self.assertEqual([t["status"] for t in results], ["COMPLETED", "COMPLETED"])

    def test_cancel_waiting_survives_restart(self):
        self.bridge.usage_reader = lambda: snapshot(90, 95)
        t = self.run_task()
        self.bridge.cancel(t["task_id"])
        self.assertEqual(self.bridge.load(t["task_id"])["status"], "CANCELLED")
        self.bridge.prepare_resume(t["task_id"], None)
        self.bridge.usage_reader = lambda: snapshot()
        self.assertEqual(self.bridge.execute(t["task_id"])["status"], "COMPLETED")

    def test_concurrent_state_read_write(self):
        t=self.bridge.create("OK",str(self.root))
        errors=[]
        def write():
            try:
                for i in range(60):
                    self.bridge.save(dict(t,sequence=i))
            except Exception as exc:
                errors.append(exc)
        worker=threading.Thread(target=write)
        worker.start()
        try:
            for _ in range(100):
                self.assertEqual(self.bridge.load(t["task_id"])["task_id"],t["task_id"])
        finally:
            worker.join(5)
        self.assertEqual(errors,[])
        self.assertFalse(worker.is_alive())


class ProcessTests(Isolated):
    def test_own_tree_kill_unrelated_untouched(self):
        other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        tree = None
        try:
            script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
            tree = OwnedProcess([sys.executable, "-c", script], stdout=subprocess.PIPE)
            child = int(tree.p.stdout.readline())
            tree.kill()
            self.assertIsNotNone(tree.p.poll())
            self.assertIsNone(other.poll())
            if os.name == "nt":
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, child)
                if h:
                    code = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                    ctypes.windll.kernel32.CloseHandle(h)
                    self.assertNotEqual(code.value, 259)
        finally:
            if tree:
                tree.p.stdout.close()
                tree.close()
            other.terminate()
            other.wait()


if __name__ == "__main__":
    unittest.main()
