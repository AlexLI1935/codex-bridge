import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_BRIDGE_HOME=str(Path(self.tmp.name)/"home"),
                        CODEX_HOME=str(Path(self.tmp.name)/"codex"), CODEX_BRIDGE_CODEX=sys.executable)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        result = subprocess.run([sys.executable, str(PROJECT/"codex_bridge.py"), *args],
                                env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_config_cli_roundtrip(self):
        rc, c = self.call("config", "show")
        self.assertEqual(rc, 0)
        self.assertEqual(c["usage"]["weekly_max_pct"], 95)
        self.assertEqual(self.call("config", "set", "usage.weekly_max_pct", "91")[0], 0)
        self.assertEqual(self.call("config", "validate")[1]["usage"]["weekly_max_pct"], 91)

    def test_invalid_config_cli(self):
        self.assertEqual(self.call("config", "set", "usage.weekly_max_pct", "101")[0], 2)
        self.assertEqual(self.call("config", "validate")[1]["usage"]["weekly_max_pct"], 95)

    def test_usage_is_read_only_and_unknown(self):
        rc, sample = self.call("usage")
        self.assertEqual(rc, 0)
        self.assertEqual(sample["status"], "UNKNOWN")
        self.assertFalse((Path(self.env["CODEX_BRIDGE_HOME"])/"runtime"/"usage-query.json").exists())

    def test_bad_task_identifier(self):
        rc, result = self.call("status", "../escape")
        self.assertEqual(rc, 2)
        self.assertEqual(result["error"], "INVALID_TASK_ID")

    def test_environment_does_not_start_task(self):
        rc, result = self.call("environment")
        self.assertEqual(rc, 0)
        self.assertEqual(result["version"], "1.0.0")
        self.assertFalse((Path(self.env["CODEX_BRIDGE_HOME"])/"tasks").exists())
