"""Command-line interface: `sparkle-clean scan`, `sparkle-clean clean`, …"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import replace

from sparkle_clean import APP_NAME, __version__, engine, history
from sparkle_clean.categories import BY_ID, CATEGORIES, PERSONAL, SYSTEM, ScanResult
from sparkle_clean.settings import LIMITS, Settings
from sparkle_clean.util import Cancelled, DiskSpace, disk_spaces, format_size, plural

COMMANDS = ("scan", "clean", "categories", "history")


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._paint("1", text)

    def dim(self, text: str) -> str:
        return self._paint("2", text)

    def red(self, text: str) -> str:
        return self._paint("31", text)

    def green(self, text: str) -> str:
        return self._paint("32", text)

    def yellow(self, text: str) -> str:
        return self._paint("33", text)

    def cyan(self, text: str) -> str:
        return self._paint("36", text)


def colors_wanted(stream, disabled: bool) -> bool:
    return (not disabled and stream.isatty() and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb")


class Spinner:
    """One animated status line on stderr; println() writes above it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, style: Style, enabled: bool):
        self.stream = sys.stderr
        self.style = style
        self.enabled = enabled
        self._text = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def show(self, text: str) -> None:
        with self._lock:
            self._text = text
        if self.enabled and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def _animate(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            with self._lock:
                self.stream.write(f"\r\033[K  {self.style.cyan(frame)} {self._text}")
                self.stream.flush()
            if self._stop.wait(0.08):
                return

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
            with self._lock:
                self.stream.write("\r\033[K")
                self.stream.flush()

    def println(self, text: str, stream=None) -> None:
        with self._lock:
            if self._thread is not None:
                self.stream.write("\r\033[K")
                self.stream.flush()
            print(text, file=stream or sys.stdout, flush=True)


# ─────────────────────────────── Arguments ─────────────────────────────

def category_list(text: str) -> list[str]:
    ids = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [part for part in ids if part not in BY_ID]
    if unknown or not ids:
        names = ", ".join(unknown) if unknown else repr(text)
        raise argparse.ArgumentTypeError(f"unknown category {names}; choose from: {', '.join(BY_ID)}")
    return ids


def bounded(name: str):
    low, high = LIMITS[name]

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = low - 1
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be a whole number from {low} to {high}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparkle-clean",
        description="Free up disk space safely. Run without arguments to open the desktop app.",
        epilog="Category ids: " + ", ".join(BY_ID)
        + ".\nSee sparkle-clean(1) for details.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="never use colors")
    common.add_argument("--json", action="store_true", help="print machine-readable JSON")

    selection = argparse.ArgumentParser(add_help=False)
    group = selection.add_argument_group("choosing what to clean")
    group.add_argument("--only", type=category_list, metavar="IDS",
                       help="only these categories (comma-separated ids)")
    group.add_argument("--include", type=category_list, metavar="IDS",
                       help="add categories that are off by default, such as browsers")
    group.add_argument("--exclude", type=category_list, metavar="IDS", help="skip these categories")
    group.add_argument("--tmp-age-days", type=bounded("tmp_age_days"), metavar="DAYS",
                       help="only temporary files unused for this many days (default: from preferences, 7)")
    group.add_argument("--journal-days", type=bounded("journal_keep_days"), metavar="DAYS",
                       help="keep this many days of journal history (default: from preferences, 7)")
    group.add_argument("--log-age-days", type=bounded("log_age_days"), metavar="DAYS",
                       help="only rotated logs older than this; 0 means any age (default: from preferences, 0)")

    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("scan", parents=[common, selection], help="show what can be cleaned; changes nothing",
                        description="Measure every category and show what can be cleaned. Nothing is deleted.")
    clean = commands.add_parser("clean", parents=[common, selection], help="scan, confirm and clean",
                                description="Scan, show what will be deleted, ask for confirmation and clean.")
    clean.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    clean.add_argument("-n", "--dry-run", action="store_true", help="only show what would be cleaned")
    clean.add_argument("-v", "--verbose", action="store_true", help="show the output of system commands")
    commands.add_parser("categories", parents=[common], help="list cleanup categories and their ids")
    commands.add_parser("history", parents=[common], help="list past cleanups")
    return parser


def effective_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.load()
    overrides = {
        "tmp_age_days": getattr(args, "tmp_age_days", None),
        "journal_keep_days": getattr(args, "journal_days", None),
        "log_age_days": getattr(args, "log_age_days", None),
    }
    return replace(settings, **{key: value for key, value in overrides.items() if value is not None})


def chosen_ids(args: argparse.Namespace, settings: Settings) -> set[str]:
    if args.only:
        chosen = set(args.only)
    else:
        chosen = {category.id for category in CATEGORIES if engine.is_selected(settings, category)}
        chosen |= set(args.include or [])
    return chosen - set(args.exclude or [])


# ──────────────────────────────── Output ───────────────────────────────

def size_text(result: ScanResult) -> str:
    if result.error:
        return "error"
    if result.size is None:
        return "unknown"
    if result.size == 0:
        return "—"
    return ("≥ " if result.partial else "") + format_size(result.size)


def disk_lines(spaces: list[DiskSpace], style: Style) -> list[str]:
    lines = []
    for space in spaces:
        filled = round(space.used_fraction * 20)
        paint = style.green if space.used_fraction < 0.8 else style.yellow if space.used_fraction < 0.92 else style.red
        bar = paint("█" * filled) + style.dim("░" * (20 - filled))
        lines.append(f"  Disk {space.mount_point:<8} {bar} {space.used_fraction:4.0%} used · "
                     f"{format_size(space.free)} free of {format_size(space.total)}")
    return lines


def report_lines(results: dict[str, ScanResult], selected: list[str], style: Style, width: int) -> list[str]:
    show_ids = width >= 78
    lines = []
    for group, heading, subtitle in ((SYSTEM, "SYSTEM", "asks for your password"),
                                     (PERSONAL, "PERSONAL", "your home folder")):
        rows = [c for c in CATEGORIES if c.group == group and c.id in results and results[c.id].available]
        if not rows:
            continue
        lines += ["", f"  {style.bold(heading)}  {style.dim(subtitle)}"]
        for category in rows:
            result = results[category.id]
            if result.error:
                mark, detail = style.red("✖"), "could not be scanned"
            elif not result.cleanable:
                mark, detail = style.dim("·"), "nothing to clean"
            else:
                mark = style.green("●") if category.id in selected else style.dim("○")
                detail = result.count_label if result.count else ""
                if category.id not in selected:
                    detail = f"{detail} (not selected)".strip()
            body = f"{category.title:<26} {detail:<24} {size_text(result):>10}"
            line = f"  {mark} {body if result.cleanable else style.dim(body)}"
            if show_ids:
                line += "  " + style.dim(category.id)
            lines.append(line)
            for note in (result.error, result.note):
                if note:
                    lines.append("      " + style.dim(note))
    total = sum(results[i].size or 0 for i in selected)
    size = format_size(total) + ("+" if any(results[i].size is None for i in selected) else "")
    label = f"Selected: {plural(len(selected), 'category', 'categories')}"
    lines += ["", f"  {style.bold(label.ljust(53))} {style.bold(f'{size:>10}')}"]
    return lines


def item_lines(results: dict[str, ScanResult], selected: list[str], style: Style, limit: int = 12) -> list[str]:
    lines = []
    for category_id in selected:
        result = results[category_id]
        if not result.items or category_id not in ("orphans", "snaps", "crash", "browsers"):
            continue
        lines += ["", f"  {BY_ID[category_id].title} ({len(result.items)}):"]
        for item in result.items[:limit]:
            size = format_size(item.size) if item.size is not None else ""
            note = f" {style.dim(item.note)}" if item.note else ""
            lines.append(f"    · {item.label}{note} {style.dim(size)}".rstrip())
        if len(result.items) > limit:
            lines.append(style.dim(f"    … and {len(result.items) - limit} more (listed in the cleanup log)"))
    return lines


def scan_json(results: dict[str, ScanResult], selected: list[str], spaces: list[DiskSpace], settings: Settings) -> dict:
    return {
        "version": __version__,
        "disks": [{"mount_point": s.mount_point, "total": s.total, "free": s.free} for s in spaces],
        "categories": [
            {
                "id": category.id, "title": category.title, "group": category.group,
                "description": category.describe(settings), "selected": category.id in selected,
                "cleanable": result.cleanable, "size": result.size, "count": result.count,
                "partial": result.partial, "note": result.note, "error": result.error or None,
                "items": [{"label": i.label, "size": i.size, "note": i.note} for i in result.items],
            }
            for category in CATEGORIES
            if (result := results.get(category.id)) is not None and result.available
        ],
        "selected": selected,
        "selected_size": sum(results[i].size or 0 for i in selected),
    }


# ─────────────────────────────── Commands ──────────────────────────────

def run_scan(args, settings, spinner: Spinner) -> dict[str, ScanResult]:
    ids = set(args.only) if args.only else None

    def progress(category, result):
        if result is None:
            spinner.show(f"{category.scanning_label}…")

    try:
        return engine.scan(settings, ids, on_progress=progress)
    finally:
        spinner.stop()


def cmd_scan(args, style: Style, spinner: Spinner) -> int:
    settings = effective_settings(args)
    results = run_scan(args, settings, spinner)
    spaces = disk_spaces()
    chosen = chosen_ids(args, settings)
    selected = [i for i in results if i in chosen and results[i].cleanable]
    if args.json:
        print(json.dumps(scan_json(results, selected, spaces, settings), indent=2))
        return 0
    width = shutil.get_terminal_size((80, 24)).columns
    print("\n".join(disk_lines(spaces, style) + report_lines(results, selected, style, width)
                    + item_lines(results, selected, style)))
    print()
    return 0


def outcome_line(outcome: engine.TaskOutcome, style: Style) -> str:
    title = BY_ID[outcome.category].title
    freed = f"{format_size(outcome.freed)} freed" if outcome.freed is not None else "done"
    if outcome.state == engine.DONE:
        return f"  {style.green('✔')} {title:<30} {freed}"
    if outcome.state == engine.WARNING:
        return f"  {style.yellow('⚠')} {title:<30} {freed} · {outcome.message}"
    if outcome.state == engine.SKIPPED:
        return f"  {style.dim('–')} {title:<30} {style.dim('skipped: ' + outcome.message)}"
    return f"  {style.red('✖')} {title:<30} {style.red(outcome.message or 'failed')}"


def cmd_clean(args, style: Style, spinner: Spinner) -> int:
    settings = effective_settings(args)
    if args.json and not (args.yes or args.dry_run):
        print("sparkle-clean: --json needs --yes (or --dry-run) because it cannot ask for confirmation",
              file=sys.stderr)
        return 2
    results = run_scan(args, settings, spinner)
    spaces = disk_spaces()
    chosen = chosen_ids(args, settings)
    selected = [i for i in results if i in chosen and results[i].cleanable]

    if not args.json:
        width = shutil.get_terminal_size((80, 24)).columns
        print("\n".join(disk_lines(spaces, style) + report_lines(results, selected, style, width)
                        + item_lines(results, selected, style)))
        print()
    if not selected or args.dry_run:
        if args.json:
            print(json.dumps({"scan": scan_json(results, selected, spaces, settings), "report": None}, indent=2))
        elif not selected:
            print(style.green("  ✨ Nothing to clean. Your system is already sparkling.\n"))
        else:
            print(style.cyan("  Dry run: nothing was deleted. Run without --dry-run to clean.\n"))
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("sparkle-clean: refusing to delete without confirmation; add --yes to clean non-interactively",
                  file=sys.stderr)
            return 2
        print(style.bold(style.red("  The items marked ● will be permanently deleted.")))
        if any(BY_ID[i].privileged for i in selected):
            print(style.dim("  You will be asked for your password to clean system files."))
        try:
            answer = input(f"  Type {style.bold('yes')} to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print(style.yellow("\n  Cancelled. Nothing was deleted.\n"))
            return 0
        print()

    def on_event(event: engine.CleanEvent) -> None:
        if args.json:
            return
        if event.kind == "authenticating":
            # No spinner here: a terminal password prompt may be sharing the screen.
            print(style.dim("  Waiting for administrator authentication…"), file=sys.stderr, flush=True)
        elif event.kind == "started":
            spinner.show(f"{BY_ID[event.category].title}…")
        elif event.kind == "output" and args.verbose:
            spinner.println(style.dim(f"      {event.text}"))
        elif event.kind == "finished-task":
            spinner.println(outcome_line(event.outcome, style))

    try:
        report = engine.clean(selected, settings, scan_results=results, on_event=on_event)
    finally:
        spinner.stop()

    if args.json:
        print(json.dumps({"scan": scan_json(results, selected, spaces, settings), "report": report.to_json()},
                         indent=2))
    else:
        print(style.dim("\n  " + "─" * 60))
        print(f"  {style.bold('Freed')} {style.bold(style.green(format_size(report.freed)))}")
        for before in report.disk_before:
            after = next((a for a in report.disk_after if a.mount_point == before.mount_point), None)
            if after:
                print(f"  Free space on {before.mount_point}: {format_size(before.free)} → {format_size(after.free)}")
        if report.log_path:
            print(style.dim(f"  Log: {report.log_path}"))
        print()
    return 1 if any(o.state in (engine.FAILED, engine.SKIPPED) for o in report.problems) else 0


def cmd_categories(args, style: Style) -> int:
    settings = Settings.load()
    if args.json:
        print(json.dumps([
            {"id": c.id, "title": c.title, "group": c.group, "default": c.default,
             "selected": engine.is_selected(settings, c), "description": c.describe(settings)}
            for c in CATEGORIES
        ], indent=2))
        return 0
    for group, heading in ((SYSTEM, "SYSTEM"), (PERSONAL, "PERSONAL")):
        print(f"\n  {style.bold(heading)}")
        for category in (c for c in CATEGORIES if c.group == group):
            mark = style.green("●") if engine.is_selected(settings, category) else style.dim("○")
            print(f"  {mark} {category.id:<11} {category.title:<26} {style.dim(category.describe(settings))}")
    print(style.dim("\n  ● selected by default. Change the selection in the app or with --only/--include/--exclude.\n"))
    return 0


def cmd_history(args, style: Style) -> int:
    entries = history.load()
    if args.json:
        print(json.dumps([entry.to_json() for entry in entries], indent=2))
        return 0
    if not entries:
        print("\n  No cleanups yet.\n")
        return 0
    print()
    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.time))
        names = [BY_ID[c].title for c in entry.categories if c in BY_ID]
        summary = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        failed = style.red(f"  ({plural(len(entry.failed), 'problem')})") if entry.failed else ""
        print(f"  {when}  {format_size(entry.freed):>10}  {style.dim(summary)}{failed}")
    total = sum(entry.freed for entry in entries)
    print(f"\n  {style.bold('Total')} {format_size(total)} freed in {plural(len(entries), 'cleanup')}\n")
    return 0


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    style = Style(colors_wanted(sys.stdout, args.no_color))
    spinner = Spinner(Style(colors_wanted(sys.stderr, args.no_color)),
                      enabled=sys.stderr.isatty() and not args.json)
    if not args.json and args.command in ("scan", "clean"):
        print(f"\n  {style.bold(APP_NAME)} {style.dim(__version__)}\n")
        if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
            print(style.yellow("  Running as root: personal items refer to root's home folder, not "
                               f"{os.environ['SUDO_USER']}'s. Run without sudo to clean your own files.\n"))
    try:
        if args.command == "scan":
            return cmd_scan(args, style, spinner)
        if args.command == "clean":
            return cmd_clean(args, style, spinner)
        if args.command == "categories":
            return cmd_categories(args, style)
        return cmd_history(args, style)
    except (KeyboardInterrupt, Cancelled):
        spinner.stop()
        print("\n  Interrupted.", file=sys.stderr)
        return 130
