"""Record of past cleanups, stored as JSON lines in the XDG state directory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sparkle_clean import paths


@dataclass
class HistoryEntry:
    time: float
    freed: int
    categories: dict[str, int | None] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    log: str = ""

    def to_json(self) -> dict:
        return {
            "time": self.time,
            "freed": self.freed,
            "categories": self.categories,
            "failed": self.failed,
            "log": self.log,
        }

    @classmethod
    def from_json(cls, data: dict) -> "HistoryEntry | None":
        try:
            return cls(
                time=float(data["time"]),
                freed=int(data["freed"]),
                categories={str(k): v for k, v in dict(data.get("categories", {})).items()},
                failed=[str(item) for item in data.get("failed", [])],
                log=str(data.get("log", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _history_file():
    return paths.state_dir() / "history.jsonl"


def append(entry: HistoryEntry) -> None:
    path = _history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.to_json(), sort_keys=True) + "\n")


def load() -> list[HistoryEntry]:
    """Every recorded cleanup, newest first. Damaged lines are skipped."""
    entries = []
    try:
        with open(_history_file(), encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and (entry := HistoryEntry.from_json(data)):
                    entries.append(entry)
    except OSError:
        return []
    entries.sort(key=lambda item: item.time, reverse=True)
    return entries
