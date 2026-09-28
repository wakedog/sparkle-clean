"""Filesystem locations used by Sparkle Clean.

Everything is resolved at call time so tests (and ``sudo -H``) can point
``HOME`` or the XDG variables somewhere else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
INSTALL_ROOT = Path("/usr/share/sparkle-clean")
IS_INSTALLED = PACKAGE_DIR.parent == INSTALL_ROOT
SOURCE_ROOT = None if IS_INSTALLED else PACKAGE_DIR.parent

SYSTEM_HELPER = Path("/usr/libexec/sparkle-clean/sparkle-clean-helper")
PKEXEC = "/usr/bin/pkexec"


def helper_command() -> list[str]:
    """Return the command that runs the privileged helper (without pkexec).

    Installed copies use the root-owned helper that the polkit policy refers
    to. A source checkout runs its own copy so code and helper always match;
    polkit then shows its generic "run a program as administrator" prompt.
    """
    if IS_INSTALLED and SYSTEM_HELPER.exists():
        return [str(SYSTEM_HELPER)]
    return [sys.executable or "/usr/bin/python3", "-I", str(PACKAGE_DIR / "helper.py")]


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / fallback


def home() -> Path:
    return Path.home()


def cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "sparkle-clean"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "sparkle-clean"


def log_dir() -> Path:
    return state_dir() / "logs"


def dev_icon_dir() -> Path | None:
    """Icon theme directory of a source checkout, or None when installed."""
    return SOURCE_ROOT / "data" / "icons" if SOURCE_ROOT else None
