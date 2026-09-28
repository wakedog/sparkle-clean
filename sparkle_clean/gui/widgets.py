"""Widgets used by the main window."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from sparkle_clean import engine
from sparkle_clean.categories import Category, ScanResult
from sparkle_clean.settings import Settings
from sparkle_clean.util import DiskSpace, format_size, plural

MAX_ITEMS = 50


def spinner(size: int = 16) -> Gtk.Widget:
    widget = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
    widget.set_size_request(size, size)
    return widget


def size_text(result: ScanResult) -> str:
    if result.error:
        return "Error"
    if result.size is None:
        return "Unknown"
    if result.size == 0:
        return "—"
    return ("≥ " if result.partial else "") + format_size(result.size)


def relative_day(timestamp: float) -> str:
    then = GLib.DateTime.new_from_unix_local(int(timestamp))
    now = GLib.DateTime.new_now_local()
    days = (GLib.DateTime.new_local(now.get_year(), now.get_month(), now.get_day_of_month(), 0, 0, 0)
            .difference(GLib.DateTime.new_local(then.get_year(), then.get_month(), then.get_day_of_month(), 0, 0, 0))
            // GLib.TIME_SPAN_DAY)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return then.format("on %B %-e") if then.get_year() == now.get_year() else then.format("on %B %-e, %Y")


class CategoryRow:
    """A selectable row for one category, expandable when it has details."""

    def __init__(self, category: Category, result: ScanResult, settings: Settings,
                 selected: bool, on_toggled: Callable[["CategoryRow"], None]):
        self.category = category
        self.result = result
        self.group: Adw.PreferencesGroup | None = None
        expandable = bool(result.items)
        self.widget = row = Adw.ExpanderRow() if expandable else Adw.ActionRow()
        row.set_use_markup(False)
        row.set_title(category.title)
        row.set_subtitle(self._subtitle(settings))

        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=selected and result.cleanable,
                                     sensitive=result.cleanable)
        self.check.update_property([Gtk.AccessibleProperty.LABEL], [f"Clean {category.title}"])
        self.check.connect("toggled", lambda *_: on_toggled(self))
        icon = Gtk.Image(icon_name=category.icon, valign=Gtk.Align.CENTER)
        icon.add_css_class("category-icon")
        icon.add_css_class(category.group)
        # One prefix widget: action and expander rows order multiple prefixes differently.
        prefix = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
        prefix.append(self.check)
        prefix.append(icon)
        row.add_prefix(prefix)

        size = Gtk.Label(label=size_text(result), valign=Gtk.Align.CENTER)
        size.add_css_class("numeric")
        size.add_css_class("size-label" if result.cleanable else "dim-label")
        if result.partial:
            size.set_tooltip_text("Some folders can only be measured by an administrator, "
                                  "so there may be more to clean")
        elif result.size == 0:
            size.set_tooltip_text("Nothing to clean")
        row.add_suffix(size)

        if expandable:
            for item in result.items[:MAX_ITEMS]:
                child = Adw.ActionRow(title=item.label, subtitle=item.note, use_markup=False)
                if item.size is not None:
                    label = Gtk.Label(label=format_size(item.size), valign=Gtk.Align.CENTER)
                    label.add_css_class("dim-label")
                    label.add_css_class("numeric")
                    child.add_suffix(label)
                row.add_row(child)
            if len(result.items) > MAX_ITEMS:
                row.add_row(Adw.ActionRow(title=f"And {len(result.items) - MAX_ITEMS} more", use_markup=False))
        elif result.cleanable:
            row.set_activatable_widget(self.check)

    def _subtitle(self, settings: Settings) -> str:
        parts = [self.category.describe(settings)]
        if self.result.error:
            parts.append(f"Could not be checked: {self.result.error}")
        else:
            if self.result.count:
                parts.append(self.result.count_label)
            if self.result.note:
                parts.append(self.result.note)
        return " · ".join(parts)

    @property
    def selected(self) -> bool:
        return self.check.get_active() and self.check.get_sensitive()


class TaskRow(Adw.ActionRow):
    """Progress of one category during cleanup."""

    def __init__(self, category: Category):
        super().__init__(title=category.title, use_markup=False, subtitle_lines=2)
        icon = Gtk.Image(icon_name=category.icon, valign=Gtk.Align.CENTER)
        icon.add_css_class("category-icon")
        icon.add_css_class(category.group)
        self.add_prefix(icon)

        self.freed = Gtk.Label(valign=Gtk.Align.CENTER)
        self.freed.add_css_class("dim-label")
        self.freed.add_css_class("numeric")
        self.add_suffix(self.freed)

        self.status = Gtk.Stack(valign=Gtk.Align.CENTER, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status.set_size_request(20, 20)
        self.status.add_named(Gtk.Box(), "pending")
        self.status.add_named(spinner(16), "running")
        for state, icon_name, label in (
            (engine.DONE, "object-select-symbolic", "Done"),
            (engine.WARNING, "dialog-warning-symbolic", "Done with warnings"),
            (engine.FAILED, "dialog-error-symbolic", "Failed"),
            (engine.SKIPPED, "action-unavailable-symbolic", "Skipped"),
        ):
            image = Gtk.Image(icon_name=icon_name, tooltip_text=label)
            image.add_css_class("task-state")
            image.add_css_class(state)
            self.status.add_named(image, state)
        self.add_suffix(self.status)
        self.add_css_class("pending")

    def set_running(self) -> None:
        self.remove_css_class("pending")
        self.status.set_visible_child_name("running")

    def set_activity(self, text: str) -> None:
        self.set_subtitle(text)

    def set_outcome(self, outcome: engine.TaskOutcome) -> None:
        self.remove_css_class("pending")
        self.status.set_visible_child_name(outcome.state)
        self.freed.set_label(format_size(outcome.freed) if outcome.freed else "")
        self.set_subtitle(outcome.message)


class SummaryCard(Gtk.Box):
    """How much the current selection frees, and how full the disks are."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("card")
        self.add_css_class("summary-card")
        self.amount = Gtk.Label(xalign=0, wrap=True)
        self.amount.add_css_class("summary-amount")
        self.amount.add_css_class("numeric")
        self.caption = Gtk.Label(xalign=0, wrap=True)
        self.caption.add_css_class("dim-label")
        self.disks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18)
        self.append(self.amount)
        self.append(self.caption)
        self.append(self.disks)

    def update(self, selected_bytes: int, selected_count: int, unknown: bool,
               anything_to_clean: bool, spaces: list[DiskSpace]) -> None:
        if not anything_to_clean:
            self.amount.set_label("All Clean")
            self.caption.set_label("There is nothing to clean up right now.")
        elif selected_count == 0:
            self.amount.set_label(format_size(0))
            self.caption.set_label("Select what you want to clean below.")
        else:
            self.amount.set_label(format_size(selected_bytes) + ("+" if unknown else ""))
            text = f"can be freed from {plural(selected_count, 'selected item')}"
            if unknown:
                text += ", plus items whose size is known only after cleaning"
            self.caption.set_label(text)

        while child := self.disks.get_first_child():
            self.disks.remove(child)
        for space in spaces:
            self.disks.append(disk_bar(space))


def disk_bar(space: DiskSpace) -> Gtk.Widget:
    if space.mount_point == "/":
        name = "System Disk"
    elif Path.home().is_relative_to(space.mount_point):
        name = "Home Disk"
    else:
        name = space.mount_point
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    header = Gtk.Box(spacing=8)
    header.append(Gtk.Image(icon_name="drive-harddisk-symbolic"))
    title = Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
    title.add_css_class("heading")
    header.append(title)
    usage = Gtk.Label(label=f"{format_size(space.free)} free of {format_size(space.total)}")
    usage.add_css_class("dim-label")
    usage.add_css_class("numeric")
    header.append(usage)

    bar = Gtk.LevelBar(min_value=0, max_value=1, value=space.used_fraction)
    for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
        bar.remove_offset_value(offset)
    bar.add_offset_value("disk-ok", 0.80)
    bar.add_offset_value("disk-warn", 0.92)
    bar.add_offset_value("disk-critical", 1.0)
    bar.add_css_class("disk-bar")
    bar.update_property([Gtk.AccessibleProperty.LABEL], [f"{name} usage"])
    bar.set_tooltip_text(f"{space.used_fraction:.0%} used")
    box.append(header)
    box.append(bar)
    return box


def format_timestamp(timestamp: float, twelve_hour: bool) -> str:
    moment = GLib.DateTime.new_from_unix_local(int(timestamp))
    clock = moment.format("%-l:%M %p") if twelve_hour else moment.format("%H:%M")
    return f"{moment.format('%A, %B %-e, %Y')} at {clock}"


def uses_twelve_hour_clock() -> bool:
    from gi.repository import Gio

    source = Gio.SettingsSchemaSource.get_default()
    if source and source.lookup("org.gnome.desktop.interface", True):
        return Gio.Settings(schema_id="org.gnome.desktop.interface").get_string("clock-format") == "12h"
    return False
