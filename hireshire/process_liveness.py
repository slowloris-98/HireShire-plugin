"""Is *another* process still alive?

Asked by the recurring sweep about the session that started it. `run_orchestration.py`
watches the Claude Code process and the shell it was launched from, and exits when
either disappears. The sweep is documented as ending with its session, and on Windows
that was simply untrue: nothing signals an orphan there — no process groups, no SIGHUP
— so a monitor outlived its session repeatedly, once with a live shortlist and
auto-apply enabled. It has to notice for itself.

**This is not the mechanism `orchestration_status` rejected.** That module answers "is
*my own* sweeper running" and deliberately uses heartbeat freshness instead of a PID
probe. It can: the sweeper writes a heartbeat. Here the subject is someone else's
process, which writes nothing this code can read, so a probe is the only thing left.

The residual risk is the one that module named — a recycled PID reads as alive — and
the direction is deliberate. Being wrong here means failing to stop a sweep, never
stopping a live session, and the exposure is one heartbeat interval rather than the
lifetime of a status file.

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
