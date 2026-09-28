"""Small helpers shared by the command-line and graphical interfaces."""

from __future__ import annotations

import errno
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path


class Cancelled(Exception):
    """Raised when the user cancels a running scan."""


def format_size(size: int | None) -> str:
    """Format a byte count the same way GLib.format_size() does (SI units)."""
    if size is None:
        return "Unknown"
    if size < 1000:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000
        if value < 1000 or unit == "PB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count:,} {word}"


def allocated_bytes(st: os.stat_result) -> int:
    """Disk space actually used by a file, which is what deleting it frees."""
    return st.st_blocks * 512


@dataclass
class Usage:
    bytes: int = 0
    files: int = 0
    partial: bool = False  # some entries could not be read


def disk_usage(path: str | os.PathLike, *, cancel: threading.Event | None = None) -> Usage:
    """Measure everything below *path* (not the directory itself).

    Symlinks are not followed, other filesystems are skipped and hard links
    are counted once.
    """
    usage = Usage()
    try:
        top = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return usage
    except OSError:
        usage.partial = True
        return usage
    if not stat.S_ISDIR(top.st_mode):
        return usage

    seen: set[tuple[int, int]] = set()
    pending = [os.fspath(path)]
    while pending:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        usage.partial = True
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev == top.st_dev:
                            pending.append(entry.path)
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    usage.bytes += allocated_bytes(st)
                    if stat.S_ISREG(st.st_mode):
                        usage.files += 1
        except FileNotFoundError:
            continue
        except OSError:
            usage.partial = True
    return usage


@dataclass
class Removal:
    freed: int = 0
    removed: int = 0
    errors: list[str] | None = None

    def add_error(self, message: str) -> None:
        if self.errors is None:
            self.errors = []
        self.errors.append(message)


def remove_contents(path: str | os.PathLike, removal: Removal | None = None) -> Removal:
    """Delete everything inside *path*, keeping the directory itself.

    Works relative to open directory descriptors (os.fwalk), so symlinks are
    removed rather than followed and a path swapped mid-walk cannot redirect
    the deletion somewhere else. Read-only folders owned by the user (common
    in extracted archives) are made writable so they can be emptied.
    """
    removal = removal or Removal()
    top = os.fspath(path)
    try:
        top_st = os.stat(top, follow_symlinks=False)
    except FileNotFoundError:
        return removal
    except OSError as error:
        removal.add_error(f"{top}: {error.strerror}")
        return removal
    if not stat.S_ISDIR(top_st.st_mode):
        return removal
    _grant_owner_access(top_st, lambda mode: os.chmod(top, mode))

    # Unreadable subfolders are retried from _remove(), so walk errors are not final.
    for dirpath, dirnames, filenames, dirfd in os.fwalk(top, topdown=False):
        try:
            _grant_owner_access(os.fstat(dirfd), lambda mode: os.fchmod(dirfd, mode))
        except OSError:
            pass
        for name in filenames:
            _remove(dirfd, dirpath, name, removal)
        for name in dirnames:
            _remove(dirfd, dirpath, name, removal)
    return removal


def _grant_owner_access(st: os.stat_result, chmod) -> None:
    if st.st_uid == os.getuid() and st.st_mode & 0o700 != 0o700:
        try:
            chmod(stat.S_IMODE(st.st_mode) | 0o700)
        except OSError:
            pass


def _remove(dirfd: int, dirpath: str, name: str, removal: Removal) -> None:
    try:
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            try:
                os.rmdir(name, dir_fd=dirfd)
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.EACCES, errno.EPERM):
                    raise
                _grant_owner_access(st, lambda mode: os.chmod(name, mode, dir_fd=dirfd))
                remove_contents(os.path.join(dirpath, name), removal)
                os.rmdir(name, dir_fd=dirfd)
        else:
            os.unlink(name, dir_fd=dirfd)
    except FileNotFoundError:
        return
    except OSError as error:
        removal.add_error(f"{os.path.join(dirpath, name)}: {error.strerror}")
        return
    removal.freed += allocated_bytes(st)
    if not stat.S_ISDIR(st.st_mode):
        removal.removed += 1


@dataclass
class DiskSpace:
    mount_point: str
    total: int
    free: int

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def used_fraction(self) -> float:
        return self.used / self.total if self.total else 0.0


def mount_point(path: str | os.PathLike) -> str:
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def disk_spaces(paths: tuple[str, ...] | None = None) -> list[DiskSpace]:
    """Free space of the filesystems that hold the system and the home folder."""
    paths = paths or ("/", "/var", str(Path.home()))
    seen: set[int] = set()
    spaces = []
    for path in paths:
        try:
            device = os.stat(path).st_dev
            info = os.statvfs(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        spaces.append(DiskSpace(
            mount_point=mount_point(path),
            total=info.f_blocks * info.f_frsize,
            free=info.f_bavail * info.f_frsize,
        ))
    return spaces


def running_process_names() -> set[str]:
    """Command names (as in /proc/PID/comm) of every running process."""
    names = set()
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return names
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as handle:
                names.add(handle.read().strip())
        except OSError:
            continue
    return names
