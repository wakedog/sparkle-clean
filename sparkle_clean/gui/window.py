"""The main window: scan, review, confirm, clean."""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from sparkle_clean import APP_ID, APP_NAME, engine, history, paths
from sparkle_clean.categories import BY_ID, CATEGORIES, SYSTEM, ScanResult
from sparkle_clean.gui import dialogs
from sparkle_clean.gui.widgets import CategoryRow, SummaryCard, TaskRow, relative_day, spinner
from sparkle_clean.settings import Settings
from sparkle_clean.util import Cancelled, DiskSpace, disk_spaces, format_size, plural

log = logging.getLogger(__name__)

IDLE, SCANNING, RESULTS, CLEANING, DONE = "idle", "scanning", "results", "cleaning", "done"


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application, title=APP_NAME, default_width=680, default_height=820)
        self.set_size_request(360, 500)
        self.settings = Settings.load()
        self.results: dict[str, ScanResult] = {}
        self.spaces: list[DiskSpace] = []
        self.rows: dict[str, CategoryRow] = {}
        self.task_rows: dict[str, TaskRow] = {}
        self.state = IDLE
        self._scan_cancel: threading.Event | None = None
        self._tasks_done = 0
        self._report: engine.CleanReport | None = None
        self._activity: tuple[str | None, str] | None = None
        self._activity_scheduled = False

        self._build()
        self._add_actions()
        self.connect("close-request", self._on_close_request)
        self._refresh_subtitle()
        GLib.idle_add(self._initial_scan)

    # ───────────────────────────── Layout ─────────────────────────────

    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.view = Adw.ToolbarView()
        self.toasts.set_child(self.view)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME)
        header.set_title_widget(self.window_title)
        header.pack_start(Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan Again",
                                     action_name="win.scan"))
        header.pack_end(self._build_menu_button())
        self.view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_idle_page(), IDLE)
        self.stack.add_named(self._build_scanning_page(), SCANNING)
        self.stack.add_named(self._build_results_page(), RESULTS)
        self.stack.add_named(self._build_cleanup_page(), CLEANING)
        self.view.set_content(self.stack)

        bar = Gtk.ActionBar()
        self.selection_label = Gtk.Label(xalign=0, margin_start=6, ellipsize=Pango.EllipsizeMode.END)
        bar.pack_start(self.selection_label)
        clean_button = Gtk.Button(label="_Clean Up…", use_underline=True, action_name="win.clean")
        clean_button.add_css_class("suggested-action")
        bar.pack_end(clean_button)
        self.view.add_bottom_bar(bar)
        self.view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED)
        self.view.set_reveal_bottom_bars(False)

    def _build_menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        main = Gio.Menu()
        main.append("_Preferences", "app.preferences")
        main.append("Cleanup _History", "win.history")
        main.append("Open _Log Folder", "app.open-logs")
        menu.append_section(None, main)
        about = Gio.Menu()
        if hasattr(Adw, "ShortcutsDialog"):
            about.append("_Keyboard Shortcuts", "app.shortcuts")
        about.append(f"_About {APP_NAME}", "app.about")
        menu.append_section(None, about)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                              tooltip_text="Main Menu")

    def _build_idle_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name=APP_ID,
            title="Free Up Disk Space",
            description="Find caches, old logs, unused packages and other files that are safe to remove. "
                        "Nothing is deleted until you confirm.",
        )
        button = Gtk.Button(label="_Scan", use_underline=True, halign=Gtk.Align.CENTER, action_name="win.scan")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        page.set_child(button)
        return page

    def _build_scanning_page(self) -> Gtk.Widget:
        self.scan_page = Adw.StatusPage(title="Scanning…")
        if hasattr(Adw, "SpinnerPaintable"):
            self.scan_page.set_paintable(Adw.SpinnerPaintable.new(self.scan_page))
        else:
            self.scan_page.set_icon_name(APP_ID)
        cancel = Gtk.Button(label="_Cancel", use_underline=True, halign=Gtk.Align.CENTER)
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_: self.cancel_scan())
        self.scan_page.set_child(cancel)
        return self.scan_page

    def _build_results_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        self.summary = SummaryCard()
        content.append(self.summary)
        self.system_group = Adw.PreferencesGroup(
            title="System", description="Shared system files. Cleaning them asks for your password.")
        self.personal_group = Adw.PreferencesGroup(
            title="Personal", description="Caches and files in your home folder")
        content.append(self.system_group)
        content.append(self.personal_group)
        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=520, child=content)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)

    def _build_cleanup_page(self) -> Gtk.Widget:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_top=36, margin_bottom=24, margin_start=12, margin_end=12)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.cleanup_icon = Gtk.Stack(halign=Gtk.Align.CENTER, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.cleanup_icon.add_named(spinner(64), "busy")
        for name, icon_name in (("success", "object-select-symbolic"), ("warning", "dialog-warning-symbolic")):
            image = Gtk.Image(icon_name=icon_name, pixel_size=48)
            image.add_css_class("status-badge")
            image.add_css_class(name)
            self.cleanup_icon.add_named(image, name)
        self.cleanup_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.cleanup_title.add_css_class("title-1")
        self.cleanup_description = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.cleanup_description.add_css_class("dim-label")
        self.progress = Gtk.ProgressBar(margin_top=6, margin_start=24, margin_end=24)
        for widget in (self.cleanup_icon, self.cleanup_title, self.cleanup_description, self.progress):
            header.append(widget)
        content.append(header)

        self.task_group = Adw.PreferencesGroup()
        content.append(self.task_group)

        self.finish_buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, visible=False)
        self.log_button = Gtk.Button(label="View _Log", use_underline=True)
        self.log_button.add_css_class("pill")
        self.log_button.connect("clicked", lambda *_: self._open_report_log())
        again = Gtk.Button(label="_Scan Again", use_underline=True, action_name="win.scan")
        again.add_css_class("pill")
        again.add_css_class("suggested-action")
        self.finish_buttons.append(self.log_button)
        self.finish_buttons.append(again)
        content.append(self.finish_buttons)

        clamp = Adw.Clamp(maximum_size=560, child=content)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)

    def _add_actions(self) -> None:
        for name, callback in (("scan", self.start_scan), ("clean", self.confirm_clean),
                               ("history", self.show_history)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, callback=callback: callback())
            self.add_action(action)
        self._sync_actions()

    def _sync_actions(self) -> None:
        busy = self.state in (SCANNING, CLEANING)
        self.lookup_action("scan").set_enabled(not busy)
        self.lookup_action("clean").set_enabled(self.state == RESULTS and bool(self.selected_ids()))

    def _show(self, state: str) -> None:
        self.state = state
        self.stack.set_visible_child_name(CLEANING if state == DONE else state)
        self.view.set_reveal_bottom_bars(state == RESULTS)
        self._sync_actions()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(text))

    def _refresh_subtitle(self) -> None:
        entries = history.load()
        self.window_title.set_subtitle(f"Last cleanup {relative_day(entries[0].time)}" if entries else "")

    # ──────────────────────────── Scanning ────────────────────────────

    def _initial_scan(self) -> bool:
        self.start_scan()
        return GLib.SOURCE_REMOVE

    def start_scan(self) -> None:
        if self.state in (SCANNING, CLEANING):
            return
        cancel = threading.Event()
        self._scan_cancel = cancel
        self.scan_page.set_description("Looking for files that are safe to remove")
        self._show(SCANNING)
        settings = replace(self.settings, selection=dict(self.settings.selection))
        threading.Thread(target=self._scan_worker, args=(settings, cancel), daemon=True).start()

    def cancel_scan(self) -> None:
        if self._scan_cancel:
            self._scan_cancel.set()

    def _scan_worker(self, settings: Settings, cancel: threading.Event) -> None:
        def progress(category, result):
            if result is None:
                GLib.idle_add(self._on_scan_progress, cancel, category.scanning_label)

        try:
            results = engine.scan(settings, cancel=cancel, on_progress=progress)
            spaces = disk_spaces()
        except Cancelled:
            results, spaces = None, []
        except Exception:
            log.exception("Scan failed")
            results, spaces = None, []
        GLib.idle_add(self._on_scan_finished, cancel, results, spaces)

    def _on_scan_progress(self, cancel: threading.Event, label: str) -> bool:
        if cancel is self._scan_cancel and not cancel.is_set():
            self.scan_page.set_description(f"{label}…")
        return GLib.SOURCE_REMOVE

    def _on_scan_finished(self, cancel, results, spaces) -> bool:
        if cancel is not self._scan_cancel:
            return GLib.SOURCE_REMOVE
        self._scan_cancel = None
        if results is None:
            if not cancel.is_set():
                self.toast("The scan failed. See the terminal output for details.")
            self._show(RESULTS if self.results else IDLE)
            return GLib.SOURCE_REMOVE
        self.results, self.spaces = results, spaces
        self._show(RESULTS)
        self._populate()
        return GLib.SOURCE_REMOVE

    def _populate(self) -> None:
        for row in self.rows.values():
            row.group.remove(row.widget)
        self.rows = {}
        for category in CATEGORIES:
            result = self.results.get(category.id)
            if result is None or not result.available:
                continue
            row = CategoryRow(category, result, self.settings, engine.is_selected(self.settings, category),
                              self._on_row_toggled)
            row.group = self.system_group if category.group == SYSTEM else self.personal_group
            row.group.add(row.widget)
            self.rows[category.id] = row
        self.system_group.set_visible(any(r.group is self.system_group for r in self.rows.values()))
        self.personal_group.set_visible(any(r.group is self.personal_group for r in self.rows.values()))
        self._update_selection()

    def _on_row_toggled(self, row: CategoryRow) -> None:
        self.settings.selection[row.category.id] = row.check.get_active()
        try:
            self.settings.save()
        except OSError as error:
            self.toast(f"Could not save your selection: {error.strerror}")
        self._update_selection()

    def selected_ids(self) -> list[str]:
        return [category_id for category_id, row in self.rows.items() if row.selected]

    def _update_selection(self) -> None:
        ids = self.selected_ids()
        total = sum(self.results[i].size or 0 for i in ids)
        unknown = any(self.results[i].size is None for i in ids)
        anything = any(result.cleanable for result in self.results.values())
        self.summary.update(total, len(ids), unknown, anything, self.spaces)
        self.selection_label.set_label(
            f"{plural(len(ids), 'item')} selected" if ids else "Nothing selected")
        self._sync_actions()

    # ──────────────────────────── Cleaning ────────────────────────────

    def confirm_clean(self) -> None:
        ids = self.selected_ids()
        if self.state != RESULTS or not ids:
            return
        total = sum(self.results[i].size or 0 for i in ids)
        exact = not any(self.results[i].size is None or self.results[i].partial for i in ids)
        heading = f"Clean Up {format_size(total)}?" if exact else "Clean Up the Selected Items?"
        body = f"{plural(len(ids), 'item')} will be permanently deleted. This cannot be undone."
        if "orphans" in ids:
            body += f" {plural(self.results['orphans'].count, 'unused package')} will be uninstalled."
        if any(BY_ID[i].privileged for i in ids):
            body += "\n\nYou will be asked for your password to clean system files."
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("clean", "_Clean Up")
        dialog.set_response_appearance("clean", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: response == "clean" and self.start_clean(ids))
        dialog.present(self)

    def start_clean(self, ids: list[str]) -> None:
        if self.state != RESULTS:
            return
        self._prepare_cleanup_page(ids)
        settings = replace(self.settings, selection=dict(self.settings.selection))
        threading.Thread(target=self._clean_worker, args=(ids, settings, dict(self.results)),
                         name="cleanup").start()

    def _prepare_cleanup_page(self, ids: list[str]) -> None:
        for row in self.task_rows.values():
            self.task_group.remove(row)
        self.task_rows = {}
        for category_id in ids:
            row = TaskRow(BY_ID[category_id])
            self.task_group.add(row)
            self.task_rows[category_id] = row
        self._tasks_done = 0
        self._report = None
        self.progress.set_fraction(0)
        self.progress.set_visible(True)
        self.cleanup_icon.set_visible_child_name("busy")
        self.cleanup_title.set_label("Cleaning Up…")
        self.cleanup_description.set_label("Starting")
        self.finish_buttons.set_visible(False)
        self._show(CLEANING)

    def _clean_worker(self, ids: list[str], settings: Settings, results: dict[str, ScanResult]) -> None:
        def on_event(event: engine.CleanEvent) -> None:
            if event.kind == "output":
                # Commands can print thousands of lines; show the latest a few times a second.
                self._activity = (event.category, event.text)
                if not self._activity_scheduled:
                    self._activity_scheduled = True
                    GLib.timeout_add(150, self._show_activity)
            else:
                GLib.idle_add(self._on_clean_event, event)

        try:
            engine.clean(ids, settings, scan_results=results, on_event=on_event)
        except Exception as error:
            log.exception("Cleanup failed")
            GLib.idle_add(self._on_clean_crashed, str(error))

    def _show_activity(self) -> bool:
        self._activity_scheduled = False
        if self.state == CLEANING and self._activity:
            category_id, text = self._activity
            row = self.task_rows.get(category_id) if category_id else None
            if row and row.status.get_visible_child_name() == "running":
                row.set_activity(text)
        return GLib.SOURCE_REMOVE

    def _on_clean_event(self, event: engine.CleanEvent) -> bool:
        if event.kind == "authenticating":
            self.cleanup_description.set_label("Waiting for authentication…")
        elif event.kind == "started":
            self.task_rows[event.category].set_running()
            self.cleanup_description.set_label(BY_ID[event.category].title)
        elif event.kind == "finished-task":
            self.task_rows[event.category].set_outcome(event.outcome)
            self._tasks_done += 1
            self.progress.set_fraction(self._tasks_done / max(1, len(self.task_rows)))
        elif event.kind == "finished":
            self._on_clean_finished(event.report)
        return GLib.SOURCE_REMOVE

    def _on_clean_finished(self, report: engine.CleanReport) -> None:
        self._report = report
        self.results = {}
        cleaned = [o for o in report.outcomes.values() if o.state in (engine.DONE, engine.WARNING)]
        problems = [o for o in report.outcomes.values() if o.state in (engine.FAILED, engine.SKIPPED)]
        warnings = [o for o in report.outcomes.values() if o.state == engine.WARNING]
        if not cleaned:
            icon, title = "warning", "Nothing Was Cleaned"
            description = problems[0].message if problems else ""
        else:
            icon, title = ("warning", "Partly Cleaned") if problems else ("success", "All Clean")
            description = f"{format_size(report.freed)} freed"
            if problems:
                description += f". {plural(len(problems), 'item')} could not be cleaned."
            elif warnings:
                description += ". Some files could not be removed."
        self.cleanup_icon.set_visible_child_name(icon)
        self.cleanup_title.set_label(title)
        self.cleanup_description.set_label(description)
        self.progress.set_visible(False)
        self.log_button.set_visible(report.log_path is not None)
        self.finish_buttons.set_visible(True)
        self._show(DONE)
        self._refresh_subtitle()
        if not self.is_active():
            notification = Gio.Notification.new(title)
            notification.set_body(description)
            self.get_application().send_notification("cleanup-finished", notification)

    def _on_clean_crashed(self, message: str) -> bool:
        self.cleanup_icon.set_visible_child_name("warning")
        self.cleanup_title.set_label("Cleanup Failed")
        self.cleanup_description.set_label(message)
        self.progress.set_visible(False)
        self.log_button.set_visible(False)
        self.finish_buttons.set_visible(True)
        self.results = {}
        self._show(DONE)
        return GLib.SOURCE_REMOVE

    def _open_report_log(self) -> None:
        if self._report and self._report.log_path:
            dialogs.open_path(self, self._report.log_path)

    def _on_close_request(self, _window) -> bool:
        if self.state == CLEANING:
            self.toast("Please wait until the cleanup finishes")
            return True
        self.cancel_scan()
        return False

    # ──────────────────────────── Dialogs ─────────────────────────────

    def show_history(self) -> None:
        dialogs.HistoryDialog(self).present(self)

    def show_preferences(self) -> None:
        def closed(changed: bool) -> None:
            if changed and self.state in (IDLE, RESULTS, DONE):
                self.start_scan()

        dialogs.PreferencesDialog(self.settings, closed).present(self)

    def open_log_folder(self) -> None:
        directory = paths.log_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.toast(f"Could not open the log folder: {error.strerror}")
            return
        dialogs.open_path(self, directory)
