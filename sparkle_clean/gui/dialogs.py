"""About, preferences, history and keyboard shortcut dialogs."""

from __future__ import annotations

import os
import platform
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from sparkle_clean import APP_ID, APP_NAME, HOMEPAGE, __version__, history, paths
from sparkle_clean.categories import BY_ID
from sparkle_clean.gui.widgets import format_timestamp, uses_twelve_hour_clock
from sparkle_clean.settings import Settings
from sparkle_clean.util import format_size, plural

RELEASE_NOTES = """\
<p>Sparkle Clean is now a desktop app.</p>
<ul>
<li>See how much space each category uses before anything is deleted, and pick what to clean</li>
<li>System files are cleaned by a small, restricted helper authorized through polkit</li>
<li>Crash reports and web browser caches can be cleaned too</li>
<li>Every cleanup is logged and kept in a history</li>
<li>A command-line interface for scripts and remote machines</li>
</ul>
"""

SHORTCUTS = (
    ("Scan Again", "<Control>r"),
    ("Clean Up", "<Control>Return"),
    ("Cleanup History", "<Control>h"),
    ("Preferences", "<Control>comma"),
    ("Keyboard Shortcuts", "<Control>question"),
    ("Close Window", "<Control>w"),
    ("Quit", "<Control>q"),
)


def open_path(parent: Gtk.Window, path: str | os.PathLike) -> None:
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(os.fspath(path)))

    def done(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                print(f"sparkle-clean: could not open {path}: {error.message}")

    launcher.launch(parent, None, done)


def _debug_info() -> str:
    try:
        system = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        system = "Linux"
    return "\n".join((
        f"{APP_NAME} {__version__}",
        f"Installed: {'yes' if paths.IS_INSTALLED else f'no, running from {paths.SOURCE_ROOT}'}",
        f"System: {system}",
        f"Python {platform.python_version()}",
        f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        f"Helper: {' '.join(paths.helper_command())}",
        f"Logs: {paths.log_dir()}",
    ))


def show_about(parent: Gtk.Widget) -> None:
    about = Adw.AboutDialog(
        application_name=APP_NAME,
        application_icon=APP_ID,
        version=__version__,
        developer_name="Bryan",
        copyright="© 2026 wakedog",
        website=HOMEPAGE,
        issue_url=HOMEPAGE + "/issues",
        license_type=Gtk.License.MIT_X11,
        comments="Free up disk space safely: see what can be removed, choose what to clean, "
                 "and keep a record of every cleanup.",
        release_notes_version=__version__,
        release_notes=RELEASE_NOTES,
        debug_info=_debug_info(),
        debug_info_filename="sparkle-clean-debug-info.txt",
    )
    about.present(parent)


def show_shortcuts(parent: Gtk.Widget) -> None:
    dialog = Adw.ShortcutsDialog()
    section = Adw.ShortcutsSection.new("General")
    for title, accelerator in SHORTCUTS:
        section.add(Adw.ShortcutsItem.new(title, accelerator))
    dialog.add(section)
    dialog.present(parent)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings: Settings, on_closed: Callable[[bool], None]):
        super().__init__(title="Preferences", search_enabled=False)
        self.settings = settings
        self.changed = False

        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        group = Adw.PreferencesGroup(
            title="What to Keep",
            description="Items newer than these limits are never removed.",
        )
        group.add(self._days_row("tmp_age_days", "Temporary Files",
                                 "Keep files used within this many days", 1, 365))
        group.add(self._days_row("journal_keep_days", "System Journal",
                                 "Keep this many days of journal history", 1, 365))
        group.add(self._days_row("log_age_days", "Rotated Logs",
                                 "Keep rotated logs newer than this many days; 0 removes all of them", 0, 365))
        page.add(group)

        selection = Adw.PreferencesGroup(
            title="Selection",
            description="Your choices in the main window are remembered for the next scan.",
        )
        reset = Adw.ActionRow(title="Restore Default Selection", activatable=True,
                              subtitle="Select the recommended categories again")
        reset.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        reset.connect("activated", self._on_reset)
        selection.add(reset)
        page.add(selection)
        self.add(page)
        self.connect("closed", lambda *_: on_closed(self.changed))

    def _days_row(self, name: str, title: str, subtitle: str, low: int, high: int) -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(low, high, 1)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_value(getattr(self.settings, name))
        row.connect("notify::value", self._on_value_changed, name)
        return row

    def _on_value_changed(self, row: Adw.SpinRow, _pspec, name: str) -> None:
        value = int(row.get_value())
        if value != getattr(self.settings, name):
            setattr(self.settings, name, value)
            self.changed = True
            self._save()

    def _on_reset(self, _row) -> None:
        if self.settings.selection:
            self.settings.selection.clear()
            self.changed = True
            self._save()
        self.add_toast(Adw.Toast.new("Default selection restored"))

    def _save(self) -> None:
        try:
            self.settings.save()
        except OSError as error:
            self.add_toast(Adw.Toast.new(f"Could not save preferences: {error.strerror}"))


class HistoryDialog(Adw.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Cleanup History", content_width=560, content_height=620)
        self.parent_window = parent
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        entries = history.load()
        if entries:
            view.set_content(self._build_list(entries))
        else:
            view.set_content(Adw.StatusPage(
                icon_name="document-open-recent-symbolic",
                title="No Cleanups Yet",
                description="Every cleanup you run is listed here, with a log of what was removed.",
            ))
        self.set_child(view)

    def _build_list(self, entries: list[history.HistoryEntry]) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        totals = Adw.PreferencesGroup()
        total_row = Adw.ActionRow(title="Total Space Freed",
                                  subtitle=f"Across {plural(len(entries), 'cleanup')}")
        total_label = Gtk.Label(label=format_size(sum(entry.freed for entry in entries)), valign=Gtk.Align.CENTER)
        total_label.add_css_class("title-3")
        total_label.add_css_class("numeric")
        total_row.add_suffix(total_label)
        totals.add(total_row)
        page.add(totals)

        group = Adw.PreferencesGroup(title="Cleanups")
        twelve_hour = uses_twelve_hour_clock()
        for entry in entries[:200]:
            names = [BY_ID[c].title for c in entry.categories if c in BY_ID]
            subtitle = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            if entry.failed:
                subtitle += f" · {plural(len(entry.failed), 'problem')}"
            row = Adw.ActionRow(title=format_timestamp(entry.time, twelve_hour), subtitle=subtitle,
                                use_markup=False)
            size = Gtk.Label(label=format_size(entry.freed), valign=Gtk.Align.CENTER)
            size.add_css_class("numeric")
            row.add_suffix(size)
            if entry.log and os.path.exists(entry.log):
                button = Gtk.Button(icon_name="text-x-generic-symbolic", tooltip_text="Open Log",
                                    valign=Gtk.Align.CENTER)
                button.add_css_class("flat")
                button.connect("clicked", lambda _b, path=entry.log: open_path(self.parent_window, path))
                row.add_suffix(button)
            group.add(row)
        page.add(group)
        return page
