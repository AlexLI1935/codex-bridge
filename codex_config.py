"""Small, validated runtime configuration and a cached local time observation."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


class BridgeError(Exception):
    pass


DEFAULT = {
    "schema_version": 1,
    "usage": {"five_hour_max_pct": 90, "weekly_max_pct": 95,
              "fresh_seconds": 300, "reset_grace_seconds": 60},
    "timezone": {"observed_utc_offset_minutes": None, "observed_at": None,
                 "os_timezone": None},
    "models": {"default": {"model": None, "reasoning_effort": "medium"},
               "complex": {"model": None, "reasoning_effort": "high"}},
}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def utc(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                  timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and value > 0:
            return float(value)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo:
            return dt.timestamp()
    raise ValueError("absolute timestamp required")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path, timeout=10):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeError("BUSY: lock held") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def validate(c):
    def shape(v, template):
        if not isinstance(v, dict) or set(v) != set(template):
            raise ValueError("unknown or missing config fields")
        for k, child in template.items():
            if isinstance(child, dict):
                shape(v[k], child)
    def number(v, low, high):
        return type(v) in (int, float) and math.isfinite(v) and low <= v <= high
    try:
        shape(c, DEFAULT)
        assert type(c["schema_version"]) is int and c["schema_version"] == 1
        u = c["usage"]
        assert all(number(u[k], 0, 100) for k in ("five_hour_max_pct", "weekly_max_pct"))
        assert number(u["fresh_seconds"], 1, 300)
        assert number(u["reset_grace_seconds"], 0, 3600)
        z = c["timezone"]
        assert z["observed_utc_offset_minutes"] is None or number(z["observed_utc_offset_minutes"], -840, 840)
        assert z["observed_at"] is None or epoch(z["observed_at"]) > 0
        assert z["os_timezone"] is None or isinstance(z["os_timezone"], str)
        for profile in c["models"].values():
            assert profile["model"] is None or (isinstance(profile["model"], str) and profile["model"].strip())
            assert profile["reasoning_effort"] in EFFORTS
    except (AssertionError, TypeError, ValueError, OverflowError):
        raise BridgeError("INVALID_CONFIG: check config.example.json") from None
    return c


def system_time():
    offset = int(datetime.now().astimezone().utcoffset().total_seconds() / 60)
    if os.name == "nt":
        result = subprocess.run(["tzutil", "/g"], capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        label = result.stdout.strip() if result.returncode == 0 else " / ".join(time.tzname)
    else:
        link = Path("/etc/localtime").resolve()
        label = str(link).split("/zoneinfo/")[-1] if "/zoneinfo/" in str(link) else " / ".join(time.tzname)
    return offset, label


def external_utc():
    request = urllib.request.Request("https://www.openai.com/", method="HEAD",
                                     headers={"Cache-Control": "no-cache"})
    try:
        response = urllib.request.urlopen(request, timeout=8)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        if float(response.headers.get("Age", "0")) > 10:
            raise ValueError("cached external clock")
        return parsedate_to_datetime(response.headers["Date"]).timestamp()


class Config:
    def __init__(self, home=None):
        fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "codex-bridge" if os.name == "nt" else Path.home() / ".codex-bridge"
        self.home = Path(home or os.environ.get("CODEX_BRIDGE_HOME", fallback)).expanduser().resolve()
        if self.home == Path(__file__).parent or Path(__file__).parent in self.home.parents:
            raise BridgeError("RUNTIME_HOME_INSIDE_CHECKOUT: choose a private external directory")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.home / "config.json"
        self.guard = self.home / "runtime" / "config.lock"

    def load(self):
        with lock(self.guard):
            if not self.path.exists():
                atomic_json(self.path, DEFAULT)
            try:
                return validate(read_json(self.path))
            except (ValueError, OSError):
                raise BridgeError("INVALID_CONFIG: config is unreadable or malformed") from None

    def set(self, dotted, value):
        self.load()
        with lock(self.guard):
            c = read_json(self.path)
            try:
                parts = dotted.split(".")
                target = c
                for part in parts[:-1]:
                    target = target[part]
                if parts[-1] not in target:
                    raise KeyError()
                target[parts[-1]] = value
            except (KeyError, TypeError):
                raise BridgeError("INVALID_CONFIG: unknown key") from None
            atomic_json(self.path, validate(c))
            return c

    def observe(self, force=False):
        self.load()
        with lock(self.guard):
            c = read_json(self.path)
            offset, label = system_time()
            now, mono = time.time(), time.monotonic()
            baseline = self.home / "runtime" / "time-baseline.json"
            try:
                b = read_json(baseline)
                valid = (c["timezone"] == b["timezone"] and b["system_offset"] == offset
                         and b["timezone"]["os_timezone"] == label
                         and mono >= b["monotonic"]
                         and abs((now-b["epoch"])-(mono-b["monotonic"])) < 120)
                if valid and not force:
                    return c["timezone"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                external = external_utc()
            except (OSError, ValueError, TypeError):
                return {"status": "UNVERIFIED", "os_timezone": label,
                        "observed_utc_offset_minutes": None}
            skew = now - external
            if abs(skew) > 120:
                raise BridgeError("CLOCK_SKEW: system UTC differs from external UTC by over 120 seconds")
            observed = int(round((now + offset * 60 - external) / 60))
            c["timezone"] = {"observed_utc_offset_minutes": observed,
                             "observed_at": utc(now), "os_timezone": label}
            atomic_json(self.path, validate(c))
            atomic_json(baseline, {"timezone": c["timezone"], "system_offset": offset,
                                  "epoch": now, "monotonic": mono, "clock_skew_seconds": skew})
            return c["timezone"]
