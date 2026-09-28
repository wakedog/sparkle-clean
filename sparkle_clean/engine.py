"""Scanning and cleaning, shared by the command line and the desktop app."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sparkle_clean import __version__, history, paths
from sparkle_clean.categories import CATEGORIES, Category, CleanResult, ScanContext, ScanResult
from sparkle_clean.settings import Settings
from sparkle_clean.util import Cancelled, DiskSpace, disk_spaces, format_size

log = logging.getLogger(__name__)

LOGS_TO_KEEP = 30


# ──────────────────────────────── Scanning ─────────────────────────────

def scan(
    settings: Settings,
    ids: set[str] | None = None,
    *,
    cancel: threading.Event | None = None,
    on_progress: Callable[[Category, ScanResult | None], None] | None = None,
) -> dict[str, ScanResult]:
    """Measure every category (or just *ids*). Nothing is modified.

    on_progress is called with (category, None) before each category is
    scanned and with (category, result) afterwards. Raises Cancelled.
    """
    context = ScanContext(settings, cancel or threading.Event())
    results: dict[str, ScanResult] = {}
    for category in CATEGORIES:
        if ids is not None and category.id not in ids:
            continue
        context.check()
        if on_progress:
            on_progress(category, None)
        try:
            result = category.scan(context)
        except Cancelled:
            raise
        except Exception as error:
            log.warning("Could not scan %s: %s", category.id, error, exc_info=log.isEnabledFor(logging.DEBUG))
            result = ScanResult(category.id, size=None, error=str(error) or type(error).__name__)
        results[category.id] = result
        if on_progress:
            on_progress(category, result)
    return results


def is_selected(settings: Settings, category: Category) -> bool:
    return settings.selection.get(category.id, category.default)


def default_selection(settings: Settings, results: dict[str, ScanResult]) -> list[str]:
    return [
        category.id for category in CATEGORIES
        if category.id in results and results[category.id].cleanable and is_selected(settings, category)
    ]


# ──────────────────────────────── Cleaning ─────────────────────────────

DONE, WARNING, FAILED, SKIPPED = "done", "warning", "failed", "skipped"


@dataclass
class TaskOutcome:
    category: str
    state: str  # DONE, WARNING, FAILED or SKIPPED
    freed: int | None = None
    message: str = ""


@dataclass
class CleanReport:
    started: float
    disk_before: list[DiskSpace]
    finished: float = 0.0
    disk_after: list[DiskSpace] = field(default_factory=list)
    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)
    log_path: Path | None = None

    @property
    def freed(self) -> int:
        return sum(outcome.freed or 0 for outcome in self.outcomes.values())

    @property
    def problems(self) -> list[TaskOutcome]:
        return [outcome for outcome in self.outcomes.values() if outcome.state != DONE]

    def to_json(self) -> dict:
        return {
            "started": self.started,
            "finished": self.finished,
            "freed": self.freed,
            "log": str(self.log_path) if self.log_path else None,
            "disks": [
                {"mount_point": before.mount_point, "total": before.total, "free_before": before.free,
                 "free_after": next((a.free for a in self.disk_after if a.mount_point == before.mount_point), None)}
                for before in self.disk_before
            ],
            "tasks": [
                {"id": outcome.category, "state": outcome.state, "freed": outcome.freed, "message": outcome.message}
                for outcome in self.outcomes.values()
            ],
        }


@dataclass
class CleanEvent:
    kind: str  # "authenticating", "started", "output", "finished-task" or "finished"
    category: str | None = None
    text: str = ""
    outcome: TaskOutcome | None = None
    report: CleanReport | None = None


class RunLog:
    """Plain-text record of one cleanup, kept in ~/.local/state/sparkle-clean/logs."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._file = None
        try:
            directory = paths.log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            self._prune(directory)
            path = directory / time.strftime("cleanup-%Y%m%d-%H%M%S.log")
            suffix = 1
            while path.exists():
                suffix += 1
                path = directory / time.strftime(f"cleanup-%Y%m%d-%H%M%S-{suffix}.log")
            self._file = open(path, "x", encoding="utf-8")
            self.path = path
        except OSError as error:
            log.warning("Could not create a cleanup log: %s", error)

    @staticmethod
    def _prune(directory: Path) -> None:
        logs = sorted(directory.glob("cleanup-*.log"))
        for old in logs[: max(0, len(logs) - (LOGS_TO_KEEP - 1))]:
            try:
                old.unlink()
            except OSError:
                pass

    def write(self, text: str) -> None:
        if self._file:
            self._file.write(f"{time.strftime('%H:%M:%S')}  {text}\n")
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None


def clean(
    ids: list[str],
    settings: Settings,
    *,
    scan_results: dict[str, ScanResult] | None = None,
    on_event: Callable[[CleanEvent], None] | None = None,
) -> CleanReport:
    """Clean the given categories, system ones first (one authentication prompt)."""
    emit = on_event or (lambda event: None)
    chosen = [category for category in CATEGORIES if category.id in set(ids)]
    run_log = RunLog()
    report = CleanReport(started=time.time(), disk_before=disk_spaces(), log_path=run_log.path)

    run_log.write(f"Sparkle Clean {__version__}: cleaning {len(chosen)} categories")
    run_log.write(f"Settings: temporary files unused for {settings.tmp_age_days} days, journal kept "
                  f"{settings.journal_keep_days} days, rotated logs older than {settings.log_age_days} days")
    for category in chosen:
        result = (scan_results or {}).get(category.id)
        if result:
            run_log.write(f"Selected: {category.title}: {format_size(result.size)} {result.count_label}".rstrip())
            for item in result.items:
                run_log.write(f"    {item.label} {item.note} {format_size(item.size) if item.size is not None else ''}".rstrip())
    for space in report.disk_before:
        run_log.write(f"Free on {space.mount_point}: {format_size(space.free)} of {format_size(space.total)}")

    def finish(category: Category, outcome: TaskOutcome) -> None:
        report.outcomes[category.id] = outcome
        detail = f" ({outcome.message})" if outcome.message else ""
        freed = f", freed {format_size(outcome.freed)}" if outcome.freed is not None else ""
        run_log.write(f"<== {category.title}: {outcome.state}{freed}{detail}")
        emit(CleanEvent("finished-task", category.id, outcome=outcome))

    try:
        privileged = [category for category in chosen if category.privileged]
        if privileged:
            _run_helper(privileged, settings, emit, run_log, finish)
        for category in chosen:
            if category.privileged:
                continue
            run_log.write(f"==> {category.title}")
            emit(CleanEvent("started", category.id))
            try:
                result = category.clean(settings)
            except Exception as error:
                log.exception("Cleaning %s failed", category.id)
                result = CleanResult(freed=None, ok=False, message=str(error))
            for line in result.details:
                run_log.write(f"    {line}")
            state = FAILED if not result.ok else (WARNING if result.message else DONE)
            finish(category, TaskOutcome(category.id, state, result.freed, result.message))
    finally:
        report.finished = time.time()
        report.disk_after = disk_spaces()
        for space in report.disk_after:
            run_log.write(f"Free on {space.mount_point}: {format_size(space.free)}")
        run_log.write(f"Finished: freed {format_size(report.freed)}")
        run_log.close()

    if report.outcomes:
        try:
            history.append(history.HistoryEntry(
                time=report.started,
                freed=report.freed,
                categories={o.category: o.freed for o in report.outcomes.values() if o.state in (DONE, WARNING)},
                failed=[o.category for o in report.problems if o.state in (FAILED, SKIPPED)],
                log=str(report.log_path or ""),
            ))
        except OSError as error:
            log.warning("Could not update the cleanup history: %s", error)
    emit(CleanEvent("finished", report=report))
    return report


AUTH_MESSAGES = {
    126: "Authentication was cancelled",
    127: "Not authorized to clean system files",
}


def helper_argv(categories: list[Category], settings: Settings) -> list[str]:
    argv = paths.helper_command() + [
        "clean",
        f"--tmp-age-days={settings.tmp_age_days}",
        f"--journal-days={settings.journal_keep_days}",
        f"--log-age-days={settings.log_age_days}",
        *(category.id for category in categories),
    ]
    return argv if os.geteuid() == 0 else [paths.PKEXEC, *argv]


def _run_helper(categories, settings, emit, run_log, finish) -> None:
    pending = {category.id: category for category in categories}

    def skip_pending(reason: str) -> None:
        for category in list(pending.values()):
            pending.pop(category.id)
            finish(category, TaskOutcome(category.id, SKIPPED, None, reason))

    argv = helper_argv(categories, settings)
    if argv[0] == paths.PKEXEC and not os.path.exists(paths.PKEXEC):
        skip_pending("pkexec is not installed, so system files cannot be cleaned")
        return
    run_log.write("$ " + shlex.join(argv))
    if argv[0] == paths.PKEXEC:
        emit(CleanEvent("authenticating"))
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
    except OSError as error:
        skip_pending(f"Could not start the system helper: {error.strerror}")
        return

    with process:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if not isinstance(message, dict) or "event" not in message:
                if line.strip():
                    run_log.write(f"    {line}")
                    emit(CleanEvent("output", text=line))
                continue
            task = message.get("task")
            category = pending.get(task) if isinstance(task, str) else None
            kind = message["event"]
            if kind == "output":
                text = str(message.get("text", ""))
                run_log.write(f"    {text}")
                emit(CleanEvent("output", task if category else None, text=text))
            elif category is None:
                continue
            elif kind == "start":
                run_log.write(f"==> {category.title}")
                emit(CleanEvent("started", category.id))
            elif kind == "done":
                pending.pop(category.id)
                freed = message.get("freed")
                freed = freed if isinstance(freed, int) and not isinstance(freed, bool) else None
                state = DONE if message.get("ok") is True else FAILED
                finish(category, TaskOutcome(category.id, state, freed, str(message.get("message") or "")))

    status = process.returncode
    if pending:
        skip_pending(AUTH_MESSAGES.get(status, f"The system helper stopped unexpectedly (exit status {status})"))
