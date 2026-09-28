# Developing Sparkle Clean

Everything needed to rebuild, change and release Sparkle Clean. For what the
app does and how to use it, see the [README](../README.md).

## Set up a development machine

Sparkle Clean is plain Python 3 with GTK 4 and libadwaita through PyGObject.
There is no compile step, no virtualenv and nothing from PyPI; it uses the
system Python and the libraries that ship with Ubuntu.

```bash
sudo apt install git make python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    desktop-file-utils appstream
git clone https://github.com/wakedog/sparkle-clean.git
cd sparkle-clean
make run      # start the app straight from the source tree
make check    # run the tests and validate the metadata; do this before every commit
```

Use the system `python3` (`/usr/bin/python3`), not a pyenv or conda Python,
because only the system Python can see `python3-gi`.

Supported targets: Ubuntu 24.04 (Python 3.12, GTK 4.14, libadwaita 1.5) and
newer. It is developed on Ubuntu 26.04. Continuous integration runs on 24.04,
so it catches anything that only works on newer libraries.

## Make targets

| Command | What it does |
| --- | --- |
| `make run` | Start the desktop app from this folder |
| `make test` | Unit tests (`python3 -m unittest discover -s tests -t .`) |
| `make check` | Tests, then validates the `.desktop` file, the AppStream metadata, the polkit policy and the bash completion |
| `make deb` | Build `dist/sparkle-clean_<version>_all.deb` |
| `make install` | Build and install the package with `sudo apt install` |
| `make uninstall` | Remove the installed package |
| `make clean` | Delete `build/`, `dist/` and `__pycache__` folders |

The command-line interface also runs from the source tree:

```bash
python3 -m sparkle_clean scan
python3 -m sparkle_clean clean --dry-run
```

## How it fits together

```text
             ┌──────────── gui/ (GTK 4 + libadwaita) ────────────┐
             │ application.py  actions, shortcuts, CSS            │
             │ window.py       idle → scanning → results →        │
             │                 cleaning → done                    │
             │ widgets.py, dialogs.py, style.css                  │
             └───────────────┬────────────────────────────────────┘
                             │            cli.py (same engine)
                             ▼                   │
                 engine.py  scan() / clean()  ◄──┘
                 │    runs scanners in threads, writes logs and history
                 │
                 ├── categories.py   the list of categories: how to measure
                 │                   each one, and in-process cleaners for
                 │                   files the user owns
                 │
                 └── pkexec ──► helper.py (as root, standard library only)
                                cleans system categories, streams JSON lines
```

- **`categories.py`** defines every category as a `Category` with an id, a
  title, an icon, a group (`SYSTEM` or `PERSONAL`), a `describe` function, a
  `scan` function returning a `ScanResult`, and optionally a `clean` function.
  **A category with `clean=None` is privileged** and is cleaned by the helper.
- **`engine.py`** runs scans in parallel. When cleaning, it starts the helper
  once for all selected privileged categories, then runs the in-process
  cleaners. It writes a log file for every run and appends a line to the
  history.
- **`helper.py`** is the only code that runs as root. It is started through
  `pkexec`, accepts only task names from `TASKS` and three range-checked
  numbers, and never takes paths or commands from the caller. It prints one
  JSON object per line (`start`, `output`, `done`, `finished`) and the engine
  turns those into progress in the app. It must import nothing outside the
  Python standard library, because it runs with `python3 -I` as root.
- **`paths.py`** decides whether we are installed (code under
  `/usr/share/sparkle-clean`) or running from source, and where settings
  (`~/.config/sparkle-clean`), history and logs (`~/.local/state/sparkle-clean`)
  live.
- **`util.py`** holds size formatting, disk usage and `remove_contents`, the
  deletion routine. It walks with directory handles, never follows symbolic
  links and never crosses into another filesystem. Use it (or the helper's
  `delete_files`) for any new deletion code rather than `shutil.rmtree`.

### Privileges and polkit

`data/io.github.wakedog.SparkleClean.policy` authorizes exactly one program,
`/usr/libexec/sparkle-clean/sparkle-clean-helper`, with `auth_admin_keep`, so
the password is asked once and remembered briefly.

When running from the source tree the helper is `sparkle_clean/helper.py`,
which the policy does not cover. pkexec then shows its generic "run a program
as administrator" prompt. That is expected. Install the `.deb` to test the
real prompt.

pkexec exit code 126 means the user cancelled the password dialog and 127
means not authorized; `engine.AUTH_MESSAGES` turns these into readable
messages.

## Adding a new category

For example, a "Docker build cache" category. Every item below is enforced by
a test or by `make check`, except where marked.

1. **Scanner.** In `categories.py`, write `scan_docker(ctx: ScanContext) -> ScanResult`.
   Call `ctx.check()` inside long loops so Cancel works. Return
   `ScanResult(id, available=False)` when the category doesn't apply, for
   example when the tool isn't installed. Categories that aren't available
   are hidden.
2. **Cleaner.**
   - *Files the user owns:* write `clean_docker(settings) -> CleanResult` in
     `categories.py` and pass it as `clean=`.
   - *Needs root:* leave `clean=None`. Then in `helper.py` add the id to
     `TASKS` (in the same order as `CATEGORIES`), write
     `clean_docker(options, report) -> int` that returns the bytes freed, and
     register it in `TASK_FUNCTIONS`. Run commands with `run_command` using
     fixed arguments only. Remember that the helper runs as root: keep it
     small and never let input from the caller become a path or an argument.
3. **Register it** in `CATEGORIES`, in the SYSTEM or PERSONAL block. Set
   `default=False` if the category might remove something people want to
   keep (like browser caches).
4. **Bash completion:** add the id to `local ids="..."` in
   `data/sparkle-clean.bash-completion`, in the same order.
5. **Docs** (not tested): add a line to the category list in the man page
   (`data/sparkle-clean.1`), the table in `README.md`, and the list in the
   AppStream `<description>`.
6. **Tests:** add parser or selection tests in `tests/test_helper.py` for
   helper code, or in `tests/test_engine.py` for in-process code. Tests run
   with a temporary `HOME` (see `tests/support.py`); never touch real system
   files in tests.
7. `make check`, then try it: `python3 -m sparkle_clean scan --only docker`.

## Settings

New preferences go in the `Settings` dataclass and `LIMITS` in `settings.py`.
Values are clamped to `LIMITS` on load. If the helper needs a new value, add
an argument in `helper.parse_args` with a strict `_bounded_int` range and pass
it from `engine.helper_argv`. Add a matching `SpinRow` in
`gui/dialogs.PreferencesDialog` and an option in `cli.py`.

## GUI notes

- Keep compatibility with libadwaita 1.5. Newer widgets are used only after a
  feature check, for example `Adw.Spinner`, and `Adw.ShortcutsDialog` behind
  `hasattr`. Follow the same pattern for anything newer.
- Work in background threads must hand results to GTK with
  `GLib.idle_add`. Never touch widgets from a thread.
- Colors and custom styles are in `gui/style.css`. Check both light and dark
  styles.

## Screenshots

`tools/screenshots.py` renders the app on a hidden GTK Broadway display with
demo data, and writes PNGs to `data/screenshots/`. It needs `gtk4-broadwayd`
(package `libgtk-4-bin`). Re-run it after visible UI changes and commit the
PNGs that the README uses (`results.png`, `results-dark.png`, `cleaning.png`,
`done.png`).

## Releasing a new version

1. Bump `__version__` in `sparkle_clean/__init__.py`.
2. Add a `<release version="X.Y.Z" date="YYYY-MM-DD">` entry at the top of
   `<releases>` in the AppStream metadata, and update `RELEASE_NOTES` in
   `gui/dialogs.py` to match.
3. Update the version and month in the first line of `data/sparkle-clean.1`,
   and the `.deb` file name in the README install section.
4. Add an entry at the top of the changelog written by `packaging/build-deb.sh`.
5. `make check` (tests fail if the man page or AppStream version is behind),
   then `make deb`, install it and try a real cleanup.
6. Commit, then tag and push:

   ```bash
   git tag v1.0.1
   git push origin main v1.0.1
   ```

   The **Release** workflow checks that the tag matches `__version__`, runs
   the checks, builds the `.deb` and attaches it to a new GitHub Release.

## Changing the app ID or GitHub account

The app ID `io.github.wakedog.SparkleClean` is the reverse domain of
`wakedog.github.io` and appears in file names under `data/`, in
`sparkle_clean/__init__.py`, the `Makefile`, `packaging/build-deb.sh` and the
polkit action id. The GitHub URL is `HOMEPAGE` in `sparkle_clean/__init__.py`,
the `<url>` entries in the metainfo and `Homepage:` in `build-deb.sh`. To
move the project, replace both everywhere:

```bash
git grep -l 'wakedog' | xargs sed -i 's/wakedog/NEWNAME/g'
for f in $(git ls-files 'data/*wakedog*'); do git mv "$f" "${f//wakedog/NEWNAME}"; done
make check
```

## Known gaps

- The privileged cleanup through pkexec has been exercised with a fake helper
  in tests and from the source tree, but should be tried once on a real
  install after any change to `helper.py` or the polkit policy.
- Ubuntu 24.04 is covered by CI for tests and imports, but the GUI has only
  been used on 26.04.
- There are no translations yet. User-visible strings are plain Python
  strings, not wrapped in gettext.
