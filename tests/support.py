import os
import tempfile
import unittest
from pathlib import Path


class TempHomeTestCase(unittest.TestCase):
    """Runs each test with HOME pointing at an empty temporary folder."""

    XDG_VARIABLES = ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")

    def setUp(self):
        super().setUp()
        self._temp = tempfile.TemporaryDirectory(prefix="sparkle-clean-test-")
        self.home = Path(self._temp.name)
        self._saved = {name: os.environ.get(name) for name in ("HOME", *self.XDG_VARIABLES)}
        os.environ["HOME"] = str(self.home)
        for name in self.XDG_VARIABLES:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for directory, subdirs, _ in os.walk(self.home):
            for name in subdirs:
                os.chmod(os.path.join(directory, name), 0o700)
        self._temp.cleanup()
        super().tearDown()

    def make_file(self, relative: str, size: int = 4096, age_days: float | None = None) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        if age_days is not None:
            then = path.stat().st_mtime - age_days * 86400
            os.utime(path, (then, then))
        return path
