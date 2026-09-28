"""User preferences, shared by the desktop app and the command line."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

from sparkle_clean import paths

LIMITS = {
    "tmp_age_days": (1, 365),
    "journal_keep_days": (1, 365),
    "log_age_days": (0, 365),
}


@dataclass
class Settings:
    # Temporary files are only removed once unused for this many days.
    tmp_age_days: int = 7
    # Journal entries newer than this are always kept.
    journal_keep_days: int = 7
    # Rotated logs newer than this are kept; 0 removes them regardless of age.
    log_age_days: int = 0
    # Category id -> whether it is selected. Missing ids use the category default.
    selection: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        try:
            with open(paths.config_dir() / "settings.json", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        settings = cls()
        for name, (low, high) in LIMITS.items():
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(settings, name, min(max(value, low), high))
        selection = data.get("selection")
        if isinstance(selection, dict):
            settings.selection = {
                str(key): value for key, value in selection.items() if isinstance(value, bool)
            }
        return settings

    def save(self) -> None:
        directory = paths.config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        known = {f.name for f in fields(self)}
        data = {key: value for key, value in asdict(self).items() if key in known}
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, directory / "settings.json")
        except BaseException:
            os.unlink(temp_path)
            raise
