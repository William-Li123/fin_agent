"""Process ownership for trusted local execution (not a security boundary)."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from typing import Any


class ProcessTree:
    """Own a process group, or an inherited Windows Job Object.

    Windows launches suspended so descendants cannot escape the job before the
    parent is assigned. POSIX descendants that deliberately create new sessions
    can escape a process group; use Docker for untrusted code.
    """

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.process: subprocess.Popen | None = None
        self._job = None
        self._closed = False
        if os.name != "nt":
            self.process = subprocess.Popen(argv, start_new_session=True, **kwargs)
            return
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._job = kernel.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not kernel.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            flags = 0x00000004 | subprocess.CREATE_NO_WINDOW  # CREATE_SUSPENDED is not exported by subprocess
            self.process = subprocess.Popen(argv, creationflags=flags, **kwargs)
            if not kernel.AssignProcessToJobObject(self._job, int(self.process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = ctypes.c_long
            if resume(int(self.process._handle)) != 0:
                raise OSError("Failed to resume the owned Windows process")
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.wait(timeout=5)
            kernel.CloseHandle(self._job)
            self._job = None
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if os.name == "nt":
            if self._job:
                self._kernel.CloseHandle(self._job)
                self._job = None
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
