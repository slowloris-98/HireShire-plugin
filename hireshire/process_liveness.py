"""Is the process in the pid file still alive?

**One caller**: the guard in `run_orchestration.py` that refuses to become a second
sweeper. Two sweepers are two writers on one SQLite database doing identical work, and
`hireshire.sweep_pid` alone cannot tell a live sweep from a file a killed one left
behind — so a stale pid would block every future sweep, which is worse than the
duplicate it was preventing.

**This module is not what broke, and it must not be deleted along with what did.** It
used to have a second caller: a watchdog that polled the Claude Code session and killed
the sweep when that process vanished. That failed twice, and both times the fault was
the *identity* being handed in, never the probe. First `$$` and `$PPID` from Git Bash —
MSYS keeps its own pid namespace, so those are not the pids Win32 `OpenProcess` knows.
Then `CLAUDE_PID`, which some hosts do not publish at all, and whose absence was read
as "stop every sweep on this machine". The pid asked about here is one the sweeper
recorded about **itself**, which is the only identity in this plugin that needs no
guessing.

The residual risk is a recycled PID reading as alive, which would refuse a sweep the
user asked for. That is the safe direction: the cost is one message telling them a
sweep is already running, against two writers corrupting a run.

Deliberately **stdlib only**, like everything else on the launcher's path.
"""

from __future__ import annotations

import os
import sys

# OpenProcess access right: enough to wait on the handle, nothing more.
_SYNCHRONIZE = 0x00100000
# WaitForSingleObject: signalled means the process has exited; a timeout means it is
# still running. This reads backwards at a glance, which is why it is spelled out.
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5


def _is_alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Handles are pointer-sized. ctypes defaults a return type to C int, which
    # truncates on 64-bit and hands back a handle that cannot be closed.
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        # Access denied means the process exists and belongs to someone else. Any
        # other failure means it is gone.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _is_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it is just not ours to signal.
        return True
    except OSError:
        # Unknown failure: answer "alive", which costs a sweep that keeps running
        # rather than one killed on a bad reading.
        return True
    return True


def is_alive(pid: int) -> bool:
    """Whether `pid` names a running process.

    `pid` must be an **OS** pid — the number the kernel uses, which is what Win32
    `OpenProcess` and `os.kill` understand. This is not pedantry: the first version of
    the caller passed `$$` from Git Bash, and MSYS keeps a pid namespace of its own, so
    a live shell that Windows called 14072 arrived here as 1684. Nothing failed loudly;
    the function simply answered "not running" about a process that was fine, and a
    healthy sweep was killed a minute after starting. Anything sourced from a shell,
    `ps`, or an MSYS tool needs converting to a WINPID before it comes here.

    Never raises: every caller is a watchdog deciding whether to keep going, and an
    exception there would end the sweep for the wrong reason.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            return _is_alive_windows(pid)
        return _is_alive_posix(pid)
    except Exception:  # noqa: BLE001 - see the docstring: never raise
        return True
