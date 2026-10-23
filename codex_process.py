"""Own one child tree. Never discover or kill processes by executable name."""
import os
import signal
import subprocess


class OwnedProcess:
    def __init__(self, argv, **kwargs):
        self.job = None
        self.closed = False
        if os.name == "nt":
            import ctypes as C
            from ctypes import wintypes as W
            class Basic(C.Structure):
                _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64),
                            ("flags", W.DWORD), ("min_ws", C.c_size_t),
                            ("max_ws", C.c_size_t), ("active_limit", W.DWORD),
                            ("affinity", C.c_size_t), ("priority", W.DWORD),
                            ("scheduling", W.DWORD)]
            class IO(C.Structure):
                _fields_ = [(name, C.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]
            class Extended(C.Structure):
                _fields_ = [("basic", Basic), ("io", IO), ("process_memory", C.c_size_t),
                            ("job_memory", C.c_size_t), ("peak_process", C.c_size_t),
                            ("peak_job", C.c_size_t)]
            self.k = C.WinDLL("kernel32", use_last_error=True)
            self.k.CreateJobObjectW.argtypes = [C.c_void_p, W.LPCWSTR]
            self.k.CreateJobObjectW.restype = W.HANDLE
            self.k.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
            self.k.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
            self.k.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
            self.k.CloseHandle.argtypes = [W.HANDLE]
            self.job = self.k.CreateJobObjectW(None, None)
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.job or not self.k.SetInformationJobObject(self.job, 9, C.byref(info), C.sizeof(info)):
                self.close()
                raise OSError("cannot establish owned Windows job")
            try:
                # Suspend before any Codex/MCP descendants can escape assignment.
                self.p = subprocess.Popen(argv, creationflags=0x4 | subprocess.CREATE_NO_WINDOW, **kwargs)
                if not self.k.AssignProcessToJobObject(self.job, W.HANDLE(int(self.p._handle))):
                    raise OSError("cannot assign child to owned Windows job")
                resume = C.WinDLL("ntdll").NtResumeProcess
                resume.argtypes = [W.HANDLE]
                resume.restype = C.c_long
                if resume(W.HANDLE(int(self.p._handle))) < 0:
                    raise OSError("cannot resume owned child")
            except BaseException:
                if hasattr(self, "p"):
                    self.p.kill()
                    self.p.wait()
                self.close()
                raise
        else:
            self.p = subprocess.Popen(argv, start_new_session=True, **kwargs)

    def kill(self):
        if os.name == "nt":
            if self.job:
                self.k.TerminateJobObject(self.job, 1)
        else:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.p.wait(timeout=10)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.job:
            self.k.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt" and hasattr(self, "p"):
            self.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if hasattr(self, "p") and self.p.poll() is None:
            self.kill()
        self.close()
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.p, name, None)
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
