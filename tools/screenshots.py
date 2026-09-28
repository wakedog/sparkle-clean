#!/usr/bin/env python3
"""Render the app's screens to PNG files on a headless Broadway display.

    tools/screenshots.py [OUTPUT_DIR]      (default: data/screenshots)

No window appears on screen and nothing is cleaned: the window is fed sample
scan results and sample cleanup events.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPLAY = ":42"


def launch(output: Path) -> int:
    """Start a private Broadway server and rerun this script as its client."""
    if not shutil.which("gtk4-broadwayd"):
        print("gtk4-broadwayd is missing (package libgtk-4-bin)", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="sparkle-shots-") as temp:
        runtime, home = Path(temp, "runtime"), Path(temp, "home")
        runtime.mkdir(mode=0o700)
        home.mkdir()
        env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), HOME=str(home), GSETTINGS_BACKEND="memory",
                   DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent", GDK_BACKEND="broadway",
                   BROADWAY_DISPLAY=DISPLAY, SPARKLE_SCREENSHOT_CHILD="1", NO_AT_BRIDGE="1")
        for name in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "WAYLAND_DISPLAY", "DISPLAY"):
            env.pop(name, None)
        server = subprocess.Popen(["gtk4-broadwayd", DISPLAY], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            return subprocess.run([sys.executable, __file__, str(output)], env=env, timeout=120).returncode
        finally:
            server.terminate()
            server.wait(timeout=10)


def render(output: Path) -> int:
    sys.path.insert(0, str(ROOT))
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

    from sparkle_clean import engine, history
    from sparkle_clean.categories import Item, ScanResult
    from sparkle_clean.gui import dialogs
    from sparkle_clean.gui.application import Application
    from sparkle_clean.util import DiskSpace

    mb, gb = 1_000_000, 1_000_000_000
    now = time.time()
    for days_ago, freed in ((31, 2_400 * mb), (12, 860 * mb), (3, 1_210 * mb)):
        history.append(history.HistoryEntry(time=now - days_ago * 86400, freed=freed,
                                            categories={"apt_cache": freed // 2, "trash": freed // 2}))

    results = {
        "apt_cache": ScanResult("apt_cache", size=412 * mb, count=238, count_label="238 files"),
        "orphans": ScanResult("orphans", size=1_130 * mb, count=4, count_label="4 packages", items=[
            Item("linux-modules-6.17.0-8-generic", 612 * mb), Item("linux-image-6.17.0-8-generic", 356 * mb),
            Item("linux-headers-6.17.0-8-generic", 98 * mb), Item("libllvm19", 64 * mb)]),
        "snaps": ScanResult("snaps", size=621 * mb, count=3, count_label="3 revisions", items=[
            Item("firefox", 287 * mb, "Revision 6565"), Item("gnome-46-2404", 244 * mb, "Revision 90"),
            Item("core24", 90 * mb, "Revision 1006")]),
        "journal": ScanResult("journal", size=184 * mb, count=22, count_label="22 archived files",
                              note="The journal uses 512.0 MB in total"),
        "old_logs": ScanResult("old_logs", size=5_200_000, count=18, count_label="18 files", partial=True),
        "tmp": ScanResult("tmp", size=0),
        "crash": ScanResult("crash", size=38 * mb, count=1, count_label="1 report",
                            items=[Item("_usr_bin_gnome-shell.1000.crash", 38 * mb)]),
        "flatpak": ScanResult("flatpak", available=False),
        "trash": ScanResult("trash", size=1_240 * mb, count=12, count_label="12 items"),
        "thumbnails": ScanResult("thumbnails", size=96 * mb, count=1204, count_label="1,204 files"),
        "browsers": ScanResult("browsers", size=880 * mb, count=2, count_label="2 browsers",
                               note="Brave will be skipped while running",
                               items=[Item("Firefox", 880 * mb),
                                      Item("Brave", 310 * mb, "Running; close it to include its cache")]),
        "pip": ScanResult("pip", size=220 * mb, count=312, count_label="312 files"),
        "npm": ScanResult("npm", available=False),
    }
    spaces = [DiskSpace("/", 512 * gb, 61 * gb)]

    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
    app = Application()
    steps = []
    failures = []

    def shot(name, attempts=5):
        def take(window):
            width, height = window.get_width(), window.get_height()
            paintable = Gtk.WidgetPaintable.new(window)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            if node is None:  # nothing painted yet; try again after the next frames
                if attempts > 1:
                    steps.insert(0, shot(name, attempts - 1))
                else:
                    failures.append(name)
                return
            renderer = Gsk.CairoRenderer()
            renderer.realize_for_display(window.get_display())
            renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height)).save_to_png(
                str(output / f"{name}.png"))
            renderer.unrealize()
            print(f"saved {output / name}.png ({width}×{height})")
        return take

    def show_results(window):
        window.cancel_scan()
        window._scan_cancel = None
        window.settings.selection = {}
        window.results, window.spaces = dict(results), spaces
        window._show("results")
        window._populate()

    def expand(window):
        window.rows["orphans"].widget.set_expanded(True)

    def collapse(window):
        window.rows["orphans"].widget.set_expanded(False)

    def confirm(window):
        window.confirm_clean()

    def close_dialog(window):
        if dialog := window.get_visible_dialog():
            dialog.force_close()

    ids = ["apt_cache", "orphans", "snaps", "journal", "old_logs", "trash", "thumbnails", "pip"]
    freed = {"apt_cache": 412 * mb, "orphans": 1_130 * mb, "snaps": 621 * mb, "journal": 179 * mb,
             "old_logs": 7_100_000, "trash": 1_240 * mb, "thumbnails": 96 * mb, "pip": 220 * mb}

    def cleaning(window):
        window.is_active = lambda: True
        window._prepare_cleanup_page(ids)
        for category in ids[:3]:
            window._on_clean_event(engine.CleanEvent("started", category))
            window._on_clean_event(engine.CleanEvent("finished-task", category, outcome=engine.TaskOutcome(
                category, engine.DONE, freed[category])))
        window._on_clean_event(engine.CleanEvent("started", "journal"))
        window.task_rows["journal"].set_activity("Vacuuming done, freed 179.0M of archived journals")

    def finished(window):
        report = engine.CleanReport(started=now, disk_before=spaces, disk_after=spaces)
        for category in ids[3:]:
            if category != "journal":
                window._on_clean_event(engine.CleanEvent("started", category))
            window._on_clean_event(engine.CleanEvent("finished-task", category, outcome=engine.TaskOutcome(
                category, engine.DONE, freed[category])))
        report.outcomes = {c: engine.TaskOutcome(c, engine.DONE, freed[c]) for c in ids}
        report.log_path = Path("/tmp/cleanup.log")
        window._on_clean_event(engine.CleanEvent("finished", report=report))

    def back_to_results(window):
        show_results(window)

    def dark(window):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def scanning(window):
        window._show("scanning")
        window.scan_page.set_description("Looking for unused packages…")

    def scroll_down(window):
        scroller = window.stack.get_child_by_name("results")
        adjustment = scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())
        window.rows["browsers"].widget.set_expanded(True)

    steps += [
        scanning, shot("scanning"),
        lambda w: w._show("idle"), shot("idle"),
        show_results, shot("results"),
        scroll_down, shot("results-personal"),
        expand, shot("results-expanded"), collapse,
        confirm, shot("confirm"), close_dialog,
        cleaning, shot("cleaning"),
        finished, shot("done"),
        back_to_results, lambda w: w.show_preferences(), shot("preferences"), close_dialog,
        lambda w: w.show_history(), shot("history"), close_dialog,
        lambda w: dialogs.show_about(w), shot("about"), close_dialog,
        dark, show_results, shot("results-dark"),
        scroll_down, shot("results-personal-dark"),
        lambda w: w.show_history(), shot("history-dark"), close_dialog,
    ]

    def run_steps(window):
        # Broadway paints irregularly without a browser attached, so move on only
        # after the previous change has actually been laid out and painted.
        painted = {"frames": 0}
        window.get_frame_clock().connect("after-paint", lambda _clock: painted.update(frames=painted["frames"] + 1))

        def advance():
            if painted["frames"] < 2:
                window.queue_draw()
                return GLib.SOURCE_CONTINUE
            if not steps:
                app.quit()
                return GLib.SOURCE_REMOVE
            painted["frames"] = 0
            steps.pop(0)(window)
            window.queue_resize()
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(250, advance)

    def on_activate(application):
        window = application.get_active_window()
        window.set_default_size(660, 900)
        run_steps(window)

    app.connect_after("activate", on_activate)
    output.mkdir(parents=True, exist_ok=True)
    app.run([sys.argv[0]])
    if failures:
        print("could not render: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "screenshots").resolve()
    sys.exit(render(target) if os.environ.get("SPARKLE_SCREENSHOT_CHILD") else launch(target))
