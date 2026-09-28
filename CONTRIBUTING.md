# Contributing

Thanks for helping improve Sparkle Clean.

- **Bugs and ideas:** open an [issue](https://github.com/wakedog/sparkle-clean/issues).
  For bugs, include the output of **About → Troubleshooting → Debug
  Information** in the app, and the cleanup log if a cleanup went wrong
  (**Main menu → Open Log Folder**, or `~/.local/state/sparkle-clean/logs/`).
- **Code changes:** read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup,
  how the code is organized and how to add a category. Run `make check`
  before opening a pull request. CI runs the same checks on Ubuntu 24.04.

Ground rules:

- Nothing is deleted without the user confirming it, and scanning never
  changes anything.
- Code that runs as root lives only in `sparkle_clean/helper.py`. It uses the
  standard library only and never accepts paths or commands from its caller.
- New deletion code uses `util.remove_contents` or the helper's
  `delete_files`, which never follow symbolic links or cross filesystems.
- Stay compatible with Ubuntu 24.04 (Python 3.12, GTK 4.14, libadwaita 1.5).
