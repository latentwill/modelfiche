"""Small OS boundaries shared by storage and the Windows distribution."""
from __future__ import annotations

import os
from pathlib import Path


def lock_file(fd: int, *, unlock: bool = False) -> None:
    """Acquire a nonblocking process-held lock, released automatically on exit."""
    if os.name == "nt":
        import msvcrt

        # Windows byte locks are mandatory. Keep the reserved byte outside the
        # metadata so status readers can still read the JSON while it is locked.
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0x7FFFFFFF, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def fsync_directory(path: Path) -> None:
    # Python's Windows CRT cannot open/fsync directories. File contents are
    # flushed before atomic replacement; POSIX additionally persists the entry.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not a dead PID.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
