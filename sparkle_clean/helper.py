#!/usr/bin/python3 -I
"""Privileged helper for Sparkle Clean.

pkexec runs this program as root. It performs a fixed set of system cleanup
tasks and never runs caller-supplied commands or paths: task names come from
an allow-list and numeric options are range-checked. Progress is reported on
stdout as one JSON object per line.

The selection functions below are also imported, unprivileged, by the
scanner so its estimates match exactly what this helper deletes. Keep this
file free of imports outside the standard library: it is installed on its
own as /usr/libexec/sparkle-clean/sparkle-clean-helper.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from typing import Callable, Iterator

APT_GET = "/usr/bin/apt-get"
DPKG = "/usr/bin/dpkg"
DPKG_QUERY = "/usr/bin/dpkg-query"
JOURNALCTL = "/usr/bin/journalctl"
SNAP = "/usr/bin/snap"

APT_ARCHIVES = "/var/cache/apt/archives"
APT_LOCK_TIMEOUT = "DPkg::Lock::Timeout=120"
SNAP_DIR = "/var/lib/snapd/snaps"
LOG_ROOT = "/var/log"
LOG_PATTERNS = ("*.gz", "*.[0-9]", "*.old")
JOURNAL_ROOTS = ("/var/log/journal", "/run/log/journal")
TMP_ROOTS = ("/tmp", "/var/tmp")
# Entries directly below a temp root that running sessions and services rely
# on, whatever their age.
TMP_KEEP_DIRS = (
    ".X11-unix", ".ICE-unix", ".XIM-unix", ".font-unix", ".Test-unix",
    "systemd-private-*", "snap-private-tmp",
)
TMP_KEEP_FILES = (".X*-lock",)
CRASH_ROOT = "/var/crash"
CRASH_PATTERNS = ("*.crash", "*.upload", "*.uploaded")

TASKS = ("apt_cache", "orphans", "snaps", "journal", "old_logs", "tmp", "crash")
DAY = 86400

SNAP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}(_[a-z0-9]{1,10})?")
SNAP_REVISION = re.compile(r"x?[0-9]{1,12}")

Found = tuple[int, str, str, os.stat_result]  # dirfd, name, full path, lstat


class HelperError(Exception):
    """A system query failed."""


class TaskError(Exception):
    """A cleanup task failed, possibly after freeing some space."""

    def __init__(self, message: str, freed: int | None = None):
        super().__init__(message)
        self.freed = freed


def allocated(st: os.stat_result) -> int:
    return st.st_blocks * 512


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def command_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C.UTF-8",
        "DEBIAN_FRONTEND": "noninteractive",
        "APT_LISTCHANGES_FRONTEND": "none",
    }
    if os.geteuid() == 0:
        env["HOME"] = "/root"
    elif os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


# ───────────────────────────── File selection ──────────────────────────────

def walk_files(
    root: str,
    want: Callable[[str, str, os.stat_result], bool],
    *,
    keep_dirs: tuple[str, ...] = (),
    errors: list[OSError] | None = None,
) -> Iterator[Found]:
    """Yield regular files below *root* for which want(dirpath, name, st) is true.

    Every lookup is relative to an open directory descriptor (os.fwalk), so
    symlinks are never followed and a directory swapped for a symlink during
    the walk cannot redirect a deletion. Other filesystems are skipped, as are
    top-level directories matching *keep_dirs*. The yielded dirfd is only
    valid until the generator is resumed.
    """
    try:
        root_st = os.stat(root, follow_symlinks=False)
    except OSError as error:
        if errors is not None and error.errno != errno.ENOENT:
            errors.append(error)
        return
    if not stat.S_ISDIR(root_st.st_mode):
        return

    def on_error(error: OSError) -> None:
        if errors is not None and error.errno != errno.ENOENT:
            errors.append(error)

    for dirpath, dirnames, filenames, dirfd in os.fwalk(root, onerror=on_error):
        at_top = dirpath == root
        kept = []
        for name in dirnames:
            if at_top and _matches(name, keep_dirs):
                continue
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and st.st_dev == root_st.st_dev:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and want(dirpath, name, st):
                yield dirfd, name, os.path.join(dirpath, name), st


def tree_size(root: str) -> int:
    return sum(allocated(found[3]) for found in walk_files(root, lambda *_: True))


def iter_rotated_logs(
    *, min_age_days: int = 0, now: float | None = None,
    errors: list[OSError] | None = None, root: str = LOG_ROOT,
) -> Iterator[Found]:
    """Compressed and rotated logs (syslog.1, kern.log.2.gz, Xorg.0.log.old…)."""
    cutoff = (now or time.time()) - min_age_days * DAY

    def want(dirpath: str, name: str, st: os.stat_result) -> bool:
        return _matches(name, LOG_PATTERNS) and (min_age_days == 0 or st.st_mtime < cutoff)

    return walk_files(root, want, keep_dirs=("journal",), errors=errors)


def iter_stale_tmp(
    *, age_days: int, open_files: frozenset[tuple[int, int]] = frozenset(),
    now: float | None = None, errors: list[OSError] | None = None,
    roots: tuple[str, ...] = TMP_ROOTS,
) -> Iterator[Found]:
    """Temporary files not read, written or changed for *age_days*, and not open."""
    cutoff = (now or time.time()) - age_days * DAY
    for root in roots:
        def want(dirpath: str, name: str, st: os.stat_result, root: str = root) -> bool:
            if dirpath == root and _matches(name, TMP_KEEP_FILES):
                return False
            if max(st.st_atime, st.st_mtime, st.st_ctime) >= cutoff:
                return False
            return (st.st_dev, st.st_ino) not in open_files

        yield from walk_files(root, want, keep_dirs=TMP_KEEP_DIRS, errors=errors)


def iter_crash_reports(*, errors: list[OSError] | None = None, root: str = CRASH_ROOT) -> Iterator[Found]:
    def want(dirpath: str, name: str, st: os.stat_result) -> bool:
        return dirpath == root and _matches(name, CRASH_PATTERNS)

    return walk_files(root, want, keep_dirs=("*",), errors=errors)


def journal_head_time(name: str) -> float | None:
    """Time of the first entry of an archived journal file, read from its name.

    journald names archived files PREFIX@SEQNUM_ID-SEQNUM-REALTIME.journal and
    disposed ones PREFIX@REALTIME-RANDOM.journal~, with REALTIME in hex
    microseconds. `journalctl --vacuum-time` compares that value, not mtime.
    """
    if "@" not in name:
        return None
    if name.endswith(".journal~"):
        fields, index = name[: -len(".journal~")].rpartition("@")[2].split("-"), 0
        expected = 2
    elif name.endswith(".journal"):
        fields, index = name[: -len(".journal")].rpartition("@")[2].split("-"), 2
        expected = 3
    else:
        return None
    if len(fields) != expected:
        return None
    try:
        return int(fields[index], 16) / 1_000_000
    except ValueError:
        return None


def iter_journal_files(*, errors: list[OSError] | None = None) -> Iterator[Found]:
    def want(dirpath: str, name: str, st: os.stat_result) -> bool:
        return name.endswith((".journal", ".journal~"))

    for root in JOURNAL_ROOTS:
        yield from walk_files(root, want, errors=errors)


def iter_expired_journals(
    *, keep_days: int, now: float | None = None, errors: list[OSError] | None = None,
) -> Iterator[Found]:
    """Archived journal files that `journalctl --vacuum-time` would remove."""
    cutoff = (now or time.time()) - keep_days * DAY

    def want(dirpath: str, name: str, st: os.stat_result) -> bool:
        head = journal_head_time(name)
        if head is None and not ("@" in name and name.endswith((".journal", ".journal~"))):
            return False
        return (head if head is not None else st.st_mtime) < cutoff

    for root in JOURNAL_ROOTS:
        yield from walk_files(root, want, errors=errors)


def open_file_ids(prefixes: tuple[str, ...] = ("/tmp/", "/var/tmp/")) -> frozenset[tuple[int, int]]:
    """(device, inode) of files below *prefixes* that some process has open.

    Only descriptors whose target path matches are stat()ed, so a hung
    network filesystem held open elsewhere cannot stall the scan.
    """
    ids = set()
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return frozenset()
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            descriptors = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in descriptors:
            link = f"{fd_dir}/{fd}"
            try:
                if not os.readlink(link).startswith(prefixes):
                    continue
                st = os.stat(link)
            except OSError:
                continue
            ids.add((st.st_dev, st.st_ino))
    return frozenset(ids)


# ───────────────────────────── Package queries ─────────────────────────────

def _query(argv: list[str], timeout: int = 300) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            env=command_env(), timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HelperError(f"Could not run {os.path.basename(argv[0])}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or [f"exit status {result.returncode}"])[-1]
        raise HelperError(f"{os.path.basename(argv[0])} failed: {detail}")
    return result.stdout


def parse_autoremove(text: str) -> list[str]:
    """Package names from `apt-get --simulate autoremove` output."""
    packages = []
    for line in text.splitlines():
        if line.startswith(("Remv ", "Purg ")):
            fields = line.split()
            if len(fields) >= 2:
                packages.append(fields[1])
    return packages


def autoremove_candidates() -> list[str]:
    return parse_autoremove(_query([APT_GET, "--simulate", "autoremove"]))


def parse_installed_sizes(text: str, native_arch: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        package, arch, size = fields
        try:
            value = int(size) * 1024  # dpkg reports KiB
        except ValueError:
            continue
        sizes[f"{package}:{arch}"] = value
        if arch in (native_arch, "all"):
            sizes[package] = value
        else:
            sizes.setdefault(package, value)
    return sizes


def installed_sizes() -> dict[str, int]:
    """Installed size in bytes of every package, keyed by name and name:arch."""
    native = _query([DPKG, "--print-architecture"], timeout=30).strip()
    listing = _query([DPKG_QUERY, "--show", "--showformat=${Package}\t${Architecture}\t${Installed-Size}\n"])
    return parse_installed_sizes(listing, native)


def parse_snap_list(text: str) -> list[tuple[str, str]]:
    """(name, revision) of disabled revisions in `snap list --all` output."""
    revisions = []
    for line in text.splitlines():
        columns = line.split()
        if len(columns) < 4 or columns[0] == "Name":
            continue
        name, revision, notes = columns[0], columns[2], columns[-1]
        if "disabled" in notes.split(",") and SNAP_NAME.fullmatch(name) and SNAP_REVISION.fullmatch(revision):
            revisions.append((name, revision))
    return revisions


def disabled_snaps() -> list[tuple[str, str]]:
    if not os.path.exists(SNAP):
        return []
    return parse_snap_list(_query([SNAP, "list", "--all", "--unicode=never", "--color=never"], timeout=120))


def snap_file(name: str, revision: str) -> str:
    return os.path.join(SNAP_DIR, f"{name}_{revision}.snap")


# ─────────────────────────────── Cleanup tasks ──────────────────────────────

class Reporter:
    """Writes progress events as JSON lines, tolerating a vanished reader."""

    def __init__(self, stream):
        self.stream = stream
        self.broken = False

    def emit(self, event: str, **data) -> None:
        if self.broken:
            return
        try:
            self.stream.write(json.dumps({"event": event, **data}) + "\n")
            self.stream.flush()
        except OSError:
            # The app went away. Finish the current work quietly rather than
            # abandoning apt or dpkg halfway through.
            self.broken = True
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, self.stream.fileno())
            os.close(devnull)


def run_command(argv: list[str], report: Reporter, task: str) -> None:
    report.emit("output", task=task, text="$ " + " ".join(argv))
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=command_env(), text=True, errors="replace",
            start_new_session=True,  # keep Ctrl+C in a terminal away from apt/dpkg
        )
    except OSError as error:
        raise TaskError(f"Could not run {os.path.basename(argv[0])}: {error.strerror}") from error
    assert process.stdout is not None
    for line in process.stdout:
        if line := line.rstrip():
            report.emit("output", task=task, text=line)
    status = process.wait()
    if status != 0:
        raise TaskError(f"{os.path.basename(argv[0])} exited with status {status}")


def delete_files(found: Iterator[Found], report: Reporter, task: str) -> int:
    freed = removed = failed = 0
    for dirfd, name, path, st in found:
        try:
            os.unlink(name, dir_fd=dirfd)
        except FileNotFoundError:
            continue
        except OSError as error:
            failed += 1
            report.emit("output", task=task, text=f"Could not remove {path}: {error.strerror}")
            continue
        removed += 1
        freed += allocated(st)
        report.emit("output", task=task, text=f"Removed {path}")
    report.emit("output", task=task, text=f"Removed {removed} file(s)")
    if failed:
        raise TaskError(f"{failed} file(s) could not be removed", freed=freed)
    return freed


def clean_apt_cache(options: argparse.Namespace, report: Reporter) -> int:
    before = tree_size(APT_ARCHIVES)
    run_command([APT_GET, "-o", APT_LOCK_TIMEOUT, "clean"], report, "apt_cache")
    return max(0, before - tree_size(APT_ARCHIVES))


def clean_orphans(options: argparse.Namespace, report: Reporter) -> int:
    try:
        packages = autoremove_candidates()
        sizes = installed_sizes() if packages else {}
    except HelperError as error:
        raise TaskError(str(error)) from error
    if not packages:
        report.emit("output", task="orphans", text="No unused packages to remove.")
        return 0
    run_command([APT_GET, "--yes", "-o", APT_LOCK_TIMEOUT, "autoremove", "--purge"], report, "orphans")
    return sum(sizes.get(package, 0) for package in packages)


def clean_journal(options: argparse.Namespace, report: Reporter) -> int:
    def journal_size() -> int:
        return sum(allocated(found[3]) for found in iter_journal_files())

    before = journal_size()
    run_command([JOURNALCTL, f"--vacuum-time={options.journal_days}d"], report, "journal")
    return max(0, before - journal_size())


def clean_old_logs(options: argparse.Namespace, report: Reporter) -> int:
    return delete_files(iter_rotated_logs(min_age_days=options.log_age_days), report, "old_logs")


def clean_tmp(options: argparse.Namespace, report: Reporter) -> int:
    found = iter_stale_tmp(age_days=options.tmp_age_days, open_files=open_file_ids())
    return delete_files(found, report, "tmp")


def clean_snaps(options: argparse.Namespace, report: Reporter) -> int:
    try:
        revisions = disabled_snaps()
    except HelperError as error:
        raise TaskError(str(error)) from error
    if not revisions:
        report.emit("output", task="snaps", text="No disabled snap revisions.")
        return 0
    freed = 0
    failures = []
    for name, revision in revisions:
        try:
            size = allocated(os.stat(snap_file(name, revision)))
        except OSError:
            size = 0
        try:
            run_command([SNAP, "remove", name, f"--revision={revision}"], report, "snaps")
        except TaskError:
            failures.append(f"{name} (revision {revision})")
            continue
        freed += size
    if failures:
        raise TaskError("Could not remove " + ", ".join(failures), freed=freed)
    return freed


def clean_crash(options: argparse.Namespace, report: Reporter) -> int:
    return delete_files(iter_crash_reports(), report, "crash")


TASK_FUNCTIONS: dict[str, Callable[[argparse.Namespace, Reporter], int]] = {
    "apt_cache": clean_apt_cache,
    "orphans": clean_orphans,
    "snaps": clean_snaps,
    "journal": clean_journal,
    "old_logs": clean_old_logs,
    "tmp": clean_tmp,
    "crash": clean_crash,
}


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        if not re.fullmatch(r"[0-9]{1,4}", text) or not low <= int(text) <= high:
            raise argparse.ArgumentTypeError(f"must be a whole number from {low} to {high}")
        return int(text)

    return parse


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sparkle-clean-helper",
        description="Privileged helper for Sparkle Clean. Run `sparkle-clean` instead.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    clean = commands.add_parser("clean", help="run system cleanup tasks")
    clean.add_argument("--tmp-age-days", type=_bounded_int(1, 365), default=7)
    clean.add_argument("--journal-days", type=_bounded_int(1, 365), default=7)
    clean.add_argument("--log-age-days", type=_bounded_int(0, 365), default=0)
    clean.add_argument("tasks", nargs="+", choices=TASKS, metavar="TASK", help=", ".join(TASKS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print("sparkle-clean-helper must run as root; start `sparkle-clean` instead.", file=sys.stderr)
        return 2
    # Once started, finish: an interrupted apt or dpkg run is worse than a slow one.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.umask(0o022)
    os.chdir("/")

    report = Reporter(sys.stdout)
    requested = set(options.tasks)
    failed = False
    for task in (name for name in TASKS if name in requested):
        report.emit("start", task=task)
        try:
            freed = TASK_FUNCTIONS[task](options, report)
        except TaskError as error:
            failed = True
            report.emit("done", task=task, ok=False, freed=error.freed, message=str(error))
        except Exception as error:  # report it and carry on with the other tasks
            failed = True
            report.emit("done", task=task, ok=False, freed=None, message=f"Unexpected error: {error}")
        else:
            report.emit("done", task=task, ok=True, freed=freed, message="")
    report.emit("finished")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
