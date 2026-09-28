"""Entry point: no arguments opens the desktop app, anything else is the CLI."""

from __future__ import annotations

import logging
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.WARNING, format="sparkle-clean: %(message)s")
    if argv:
        from sparkle_clean import cli

        return cli.main(argv)

    from sparkle_clean import gui

    return gui.run()
