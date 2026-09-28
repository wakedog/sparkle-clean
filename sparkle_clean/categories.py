"""What Sparkle Clean can clean, and how each category is measured.

System categories are cleaned by the privileged helper (see helper.py);
the rest are cleaned in-process as the current user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from sparkle_clean import helper, paths
from sparkle_clean.settings import Settings
from sparkle_clean.util import (
    Cancelled,
    Removal,
    allocated_bytes,
    disk_usage,
    format_size,
    plural,
    remove_contents,
    running_process_names,
)

SYSTEM = "system"
PERSONAL = "personal"


@dataclass
class Item:
    """One line of detail below a category, such as a package name."""

    label: str
    size: int | None = None
    note: str = ""


@dataclass
class ScanResult:
    category: str
    size: int | None = 0  # bytes cleaning would free; None when unknown in advance
    count: int = 0
    count_label: str = ""  # "12 packages"
    items: list[Item] = field(default_factory=list)
    partial: bool = False  # some locations could not be read without root
    note: str = ""
    available: bool = True  # False when the category does not apply to this system
    error: str = ""

    @property
    def cleanable(self) -> bool:
        return self.available and not self.error and (self.size is None or self.size > 0)


@dataclass
class ScanContext:
    settings: Settings
    cancel: threading.Event

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


@dataclass
class CleanResult:
    freed: int | None = 0
    ok: bool = True
    message: str = ""
    details: list[str] = field(default_factory=list)  # written to the cleanup log


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    icon: str
    group: str
    describe: Callable[[Settings], str]
    scan: Callable[[ScanContext], ScanResult]
    # In-process cleaner; None means the privileged helper handles it.
    clean: Callable[[Settings], CleanResult] | None = None
    default: bool = True
    scanning_label: str = "Scanning"

    @property
    def privileged(self) -> bool:
        return self.clean is None


def days(count: int) -> str:
    return plural(count, "day")


def _sum_found(category: str, found: Iterator[helper.Found], ctx: ScanContext,
               errors: list[OSError], noun: str = "file") -> ScanResult:
    size = count = 0
    for *_, st in found:
        ctx.check()
        size += allocated_bytes(st)
        count += 1
    return ScanResult(category, size=size, count=count, count_label=plural(count, noun), partial=bool(errors))


def _usage_result(category: str, path: Path, ctx: ScanContext, noun: str = "file") -> ScanResult:
    usage = disk_usage(path, cancel=ctx.cancel)
    return ScanResult(category, size=usage.bytes, count=usage.files,
                      count_label=plural(usage.files, noun), partial=usage.partial)


def _from_removal(removal: Removal, extra_message: str = "") -> CleanResult:
    errors = removal.errors or []
    message = f"{plural(len(errors), 'item')} could not be removed" if errors else ""
    if extra_message:
        message = f"{message}. {extra_message}" if message else extra_message
    details = [f"Removed {plural(removal.removed, 'file')} ({format_size(removal.freed)})"]
    details += [f"Could not remove {error}" for error in errors]
    return CleanResult(freed=removal.freed, ok=removal.removed > 0 or not errors,
                       message=message, details=details)


def _clear(*directories: Path) -> CleanResult:
    removal = Removal()
    for directory in directories:
        remove_contents(directory, removal)
    return _from_removal(removal)


# ─────────────────────────────── System ────────────────────────────────

def scan_apt_cache(ctx: ScanContext) -> ScanResult:
    if not os.path.isdir(helper.APT_ARCHIVES):
        return ScanResult("apt_cache", available=False)
    result = _usage_result("apt_cache", Path(helper.APT_ARCHIVES), ctx)
    result.partial = False  # only the tiny partial/ download folder is unreadable
    return result


def scan_orphans(ctx: ScanContext) -> ScanResult:
    if not os.path.exists(helper.APT_GET):
        return ScanResult("orphans", available=False)
    packages = helper.autoremove_candidates()
    if not packages:
        return ScanResult("orphans", size=0)
    ctx.check()
    sizes = helper.installed_sizes()
    items = sorted((Item(name, sizes.get(name)) for name in packages),
                   key=lambda item: (-(item.size or 0), item.label))
    return ScanResult("orphans", size=sum(item.size or 0 for item in items), count=len(items),
                      count_label=plural(len(items), "package"), items=items)


def scan_snaps(ctx: ScanContext) -> ScanResult:
    if not os.path.exists(helper.SNAP):
        return ScanResult("snaps", available=False)
    items = []
    for name, revision in helper.disabled_snaps():
        try:
            size = allocated_bytes(os.stat(helper.snap_file(name, revision)))
        except OSError:
            size = None
        items.append(Item(name, size, note=f"Revision {revision}"))
    known = [item.size for item in items if item.size is not None]
    size = sum(known) if known or not items else None
    return ScanResult("snaps", size=size, count=len(items),
                      count_label=plural(len(items), "revision"), items=items)


def scan_journal(ctx: ScanContext) -> ScanResult:
    if not any(os.path.isdir(root) for root in helper.JOURNAL_ROOTS):
        return ScanResult("journal", available=False)
    errors: list[OSError] = []
    found = helper.iter_expired_journals(keep_days=ctx.settings.journal_keep_days, errors=errors)
    result = _sum_found("journal", found, ctx, errors, noun="archived file")
    in_use = sum(allocated_bytes(found[3]) for found in helper.iter_journal_files())
    if in_use:
        result.note = f"The journal uses {format_size(in_use)} in total"
    return result


def scan_old_logs(ctx: ScanContext) -> ScanResult:
    errors: list[OSError] = []
    found = helper.iter_rotated_logs(min_age_days=ctx.settings.log_age_days, errors=errors)
    return _sum_found("old_logs", found, ctx, errors)


def scan_tmp(ctx: ScanContext) -> ScanResult:
    errors: list[OSError] = []
    found = helper.iter_stale_tmp(age_days=ctx.settings.tmp_age_days,
                                  open_files=helper.open_file_ids(), errors=errors)
    return _sum_found("tmp", found, ctx, errors)


def scan_crash(ctx: ScanContext) -> ScanResult:
    if not os.path.isdir(helper.CRASH_ROOT):
        return ScanResult("crash", available=False)
    errors: list[OSError] = []
    items = [Item(name, allocated_bytes(st)) for _, name, _, st in helper.iter_crash_reports(errors=errors)]
    return ScanResult("crash", size=sum(item.size or 0 for item in items), count=len(items),
                      count_label=plural(len(items), "report"), items=items, partial=bool(errors))


FLATPAK_LOCATIONS = ("/var/lib/flatpak",)


def scan_flatpak(ctx: ScanContext) -> ScanResult:
    if not shutil.which("flatpak"):
        return ScanResult("flatpak", available=False)
    return ScanResult("flatpak", size=None, note="The amount freed is known after cleaning")


def _free_bytes(locations: list[str]) -> int:
    seen, total = set(), 0
    for location in locations:
        try:
            device = os.stat(location).st_dev
            info = os.statvfs(location)
        except OSError:
            continue
        if device not in seen:
            seen.add(device)
            total += info.f_bavail * info.f_frsize
    return total


def clean_flatpak(settings: Settings) -> CleanResult:
    locations = [*FLATPAK_LOCATIONS, str(paths.data_home() / "flatpak")]
    before = _free_bytes(locations)
    try:
        process = subprocess.run(
            ["flatpak", "uninstall", "--unused", "--assumeyes", "--noninteractive"],
            capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CleanResult(freed=None, ok=False, message=f"Could not run flatpak: {error}")
    freed = max(0, _free_bytes(locations) - before)
    details = [line for line in (process.stdout + process.stderr).splitlines() if line.strip()]
    if process.returncode != 0:
        reason = details[-1] if details else f"flatpak exited with status {process.returncode}"
        return CleanResult(freed=freed, ok=False, message=reason, details=details)
    return CleanResult(freed=freed, details=details)


# ────────────────────────────── Personal ───────────────────────────────

def thumbnails_dir() -> Path:
    return paths.cache_home() / "thumbnails"


def trash_dir() -> Path:
    return paths.data_home() / "Trash"


def pip_cache_dir() -> Path:
    return paths.cache_home() / "pip"


def npm_cache_dir() -> Path:
    return paths.home() / ".npm" / "_cacache"


def scan_thumbnails(ctx: ScanContext) -> ScanResult:
    return _usage_result("thumbnails", thumbnails_dir(), ctx)


def clean_thumbnails(settings: Settings) -> CleanResult:
    return _clear(thumbnails_dir())


def scan_trash(ctx: ScanContext) -> ScanResult:
    result = _usage_result("trash", trash_dir(), ctx)
    try:
        items = sum(1 for entry in os.scandir(trash_dir() / "info") if entry.name.endswith(".trashinfo"))
    except OSError:
        items = 0
    result.count = items
    result.count_label = plural(items, "item")
    return result


def clean_trash(settings: Settings) -> CleanResult:
    removal = Removal()
    for part in ("files", "info", "expunged"):
        remove_contents(trash_dir() / part, removal)
    sizes_cache = trash_dir() / "directorysizes"
    try:
        st = os.stat(sizes_cache, follow_symlinks=False)
        os.unlink(sizes_cache)
        removal.freed += allocated_bytes(st)
    except FileNotFoundError:
        pass
    except OSError as error:
        removal.add_error(f"{sizes_cache}: {error.strerror}")
    return _from_removal(removal)


def scan_pip(ctx: ScanContext) -> ScanResult:
    if not (pip_cache_dir().exists() or shutil.which("pip3") or shutil.which("pip")):
        return ScanResult("pip", available=False)
    return _usage_result("pip", pip_cache_dir(), ctx)


def clean_pip(settings: Settings) -> CleanResult:
    return _clear(pip_cache_dir())


def scan_npm(ctx: ScanContext) -> ScanResult:
    if not (npm_cache_dir().parent.exists() or shutil.which("npm")):
        return ScanResult("npm", available=False)
    return _usage_result("npm", npm_cache_dir(), ctx)


def clean_npm(settings: Settings) -> CleanResult:
    return _clear(npm_cache_dir())


@dataclass(frozen=True)
class Browser:
    name: str
    processes: tuple[str, ...]  # names as they appear in /proc/PID/comm
    cache_dirs: tuple[str, ...]  # "cache:" is XDG_CACHE_HOME, anything else is below $HOME

    def existing_cache_dirs(self) -> list[Path]:
        found = []
        for entry in self.cache_dirs:
            path = paths.cache_home() / entry[6:] if entry.startswith("cache:") else paths.home() / entry
            if path.is_dir() and not path.is_symlink():
                found.append(path)
        return found

    def is_running(self, running: set[str]) -> bool:
        return not running.isdisjoint(self.processes)


BROWSERS = (
    Browser("Firefox", ("firefox", "firefox-bin", "firefox-esr"),
            ("cache:mozilla/firefox", "snap/firefox/common/.cache/mozilla/firefox",
             ".var/app/org.mozilla.firefox/cache")),
    Browser("Google Chrome", ("chrome",),
            ("cache:google-chrome", ".var/app/com.google.Chrome/cache")),
    Browser("Chromium", ("chromium", "chromium-browse", "chrome"),
            ("cache:chromium", "snap/chromium/common/.cache/chromium",
             ".var/app/org.chromium.Chromium/cache")),
    Browser("Brave", ("brave",), ("cache:BraveSoftware", ".var/app/com.brave.Browser/cache")),
    Browser("Microsoft Edge", ("msedge",), ("cache:microsoft-edge",)),
    Browser("Vivaldi", ("vivaldi-bin",), ("cache:vivaldi",)),
    Browser("Opera", ("opera",), ("cache:opera",)),
)


def scan_browsers(ctx: ScanContext) -> ScanResult:
    running = running_process_names()
    items, total, busy = [], 0, []
    for browser in BROWSERS:
        directories = browser.existing_cache_dirs()
        if not directories:
            continue
        size = sum(disk_usage(directory, cancel=ctx.cancel).bytes for directory in directories)
        if browser.is_running(running):
            busy.append(browser.name)
            items.append(Item(browser.name, size, note="Running; close it to include its cache"))
        else:
            total += size
            items.append(Item(browser.name, size))
    if not items:
        return ScanResult("browsers", available=False)
    note = f"{' and '.join(busy)} will be skipped while running" if busy else ""
    return ScanResult("browsers", size=total, count=len(items),
                      count_label=plural(len(items), "browser"), items=items, note=note)


def clean_browsers(settings: Settings) -> CleanResult:
    running = running_process_names()
    removal, skipped = Removal(), []
    for browser in BROWSERS:
        directories = browser.existing_cache_dirs()
        if not directories:
            continue
        if browser.is_running(running):
            skipped.append(browser.name)
            continue
        for directory in directories:
            remove_contents(directory, removal)
    note = f"Skipped {' and '.join(skipped)} because it was running" if skipped else ""
    return _from_removal(removal, note)


# ──────────────────────────────── Registry ─────────────────────────────

CATEGORIES: tuple[Category, ...] = (
    Category("apt_cache", "Package Cache", "package-x-generic-symbolic", SYSTEM,
             lambda s: "Downloaded installer files in /var/cache/apt/archives",
             scan_apt_cache, scanning_label="Checking the package cache"),
    Category("orphans", "Unused Packages", "application-x-addon-symbolic", SYSTEM,
             lambda s: "Automatically installed packages that nothing needs anymore",
             scan_orphans, scanning_label="Looking for unused packages"),
    Category("snaps", "Old Snap Revisions", "application-x-executable-symbolic", SYSTEM,
             lambda s: "Previous versions snapd keeps around after updates",
             scan_snaps, scanning_label="Checking snap revisions"),
    Category("journal", "Old Journal Entries", "x-office-document-symbolic", SYSTEM,
             lambda s: f"System journal files older than {days(s.journal_keep_days)}",
             scan_journal, scanning_label="Measuring the system journal"),
    Category("old_logs", "Rotated Logs", "text-x-generic-symbolic", SYSTEM,
             lambda s: "Compressed and rotated logs in /var/log"
             + (f" older than {days(s.log_age_days)}" if s.log_age_days else ""),
             scan_old_logs, scanning_label="Finding rotated logs"),
    Category("tmp", "Old Temporary Files", "document-open-recent-symbolic", SYSTEM,
             lambda s: f"Files in /tmp and /var/tmp unused for {days(s.tmp_age_days)}",
             scan_tmp, scanning_label="Finding stale temporary files"),
    Category("crash", "Crash Reports", "dialog-warning-symbolic", SYSTEM,
             lambda s: "Reports about programs that crashed, in /var/crash",
             scan_crash, scanning_label="Looking for crash reports"),
    Category("flatpak", "Unused Flatpak Runtimes", "system-software-install-symbolic", SYSTEM,
             lambda s: "Runtimes and extensions that no installed app uses",
             scan_flatpak, clean_flatpak, scanning_label="Checking Flatpak"),
    Category("trash", "Trash", "user-trash-symbolic", PERSONAL,
             lambda s: "Files you moved to the Trash",
             scan_trash, clean_trash, scanning_label="Weighing the Trash"),
    Category("thumbnails", "Thumbnail Cache", "image-x-generic-symbolic", PERSONAL,
             lambda s: "Previews of your files, recreated when needed",
             scan_thumbnails, clean_thumbnails, scanning_label="Checking the thumbnail cache"),
    Category("browsers", "Web Browser Caches", "web-browser-symbolic", PERSONAL,
             lambda s: "Cached web content; sites load a little slower the next time",
             scan_browsers, clean_browsers, default=False, scanning_label="Measuring browser caches"),
    Category("pip", "Python Package Cache", "folder-download-symbolic", PERSONAL,
             lambda s: "Downloads cached by pip in ~/.cache/pip",
             scan_pip, clean_pip, scanning_label="Checking the pip cache"),
    Category("npm", "npm Cache", "folder-download-symbolic", PERSONAL,
             lambda s: "Downloads cached by npm in ~/.npm",
             scan_npm, clean_npm, scanning_label="Checking the npm cache"),
)

BY_ID = {category.id: category for category in CATEGORIES}
