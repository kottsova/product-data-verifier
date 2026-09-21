"""Kill-able process-tree containment for externally bounded work.

A Python thread or ``ThreadPoolExecutor`` cannot be stopped from outside, and a
Playwright driver plus its browser live in *other* processes that outlive a
cancelled Python call.  The only reliable hard limit is therefore an OS-level
one: run the work in a child process, put the whole tree in one Windows Job
Object (or POSIX process group) and terminate that container at the deadline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Sequence

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_CLASS = 9
    _BASIC_PROCESS_ID_LIST_CLASS = 3
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _MAX_LISTED_PIDS = 256

    class _PidList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED_PIDS),
        ]


def pid_alive(pid: int) -> bool:
    """Whether a process id refers to a running process (never signals it)."""
    if _IS_WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ContainedProcess:
    """A child process whose whole descendant tree can be killed at once."""

    def __init__(self, argv: Sequence[str], *, cwd: str, env: dict[str, str] | None,
                 stdout, stderr) -> None:
        kwargs: dict[str, object] = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        self._job = None
        self.contained = False
        self.popen = subprocess.Popen(
            list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, **kwargs,
        )
        if _IS_WINDOWS:
            self._adopt_windows()
        else:
            self.contained = True
        self.seen_pids: set[int] = {self.popen.pid}

    # -- Windows job object -------------------------------------------------
    def _adopt_windows(self) -> None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job, _EXTENDED_LIMIT_CLASS, ctypes.byref(info), ctypes.sizeof(info),
        )
        if ok and _kernel32.AssignProcessToJobObject(job, int(self.popen._handle)):
            self._job = job
            self.contained = True
        else:
            _kernel32.CloseHandle(job)

    def _job_pids(self) -> set[int]:
        if not self._job:
            return set()
        listing = _PidList()
        if not _kernel32.QueryInformationJobObject(
            self._job, _BASIC_PROCESS_ID_LIST_CLASS, ctypes.byref(listing),
            ctypes.sizeof(listing), None,
        ):
            return set()
        return {int(listing.ProcessIdList[i]) for i in range(listing.NumberOfProcessIdsInList)}

    # -- public -------------------------------------------------------------
    def poll(self) -> int | None:
        return self.popen.poll()

    def descendant_pids(self) -> set[int]:
        """Best-effort pids currently in the container (also remembered)."""
        if _IS_WINDOWS:
            self.seen_pids |= self._job_pids()
        return set(self.seen_pids)

    def kill_tree(self) -> set[int]:
        """Terminate the whole container; returns pids still alive afterwards."""
        pids = self.descendant_pids()
        if _IS_WINDOWS:
            if self._job:
                _kernel32.TerminateJobObject(self._job, 1)
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(self.popen.pid), "/T", "/F"],
                    capture_output=True, check=False, timeout=10,
                )
        else:
            try:
                os.killpg(self.popen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Termination is asynchronous for grandchildren; give it a short bound.
        import time
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
            time.sleep(0.05)
        return {pid for pid in pids if pid_alive(pid)}

    def close(self) -> None:
        if _IS_WINDOWS and self._job:
            _kernel32.CloseHandle(self._job)
            self._job = None
