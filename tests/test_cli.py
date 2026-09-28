import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from sparkle_clean import cli
from sparkle_clean.categories import BY_ID
from tests.support import TempHomeTestCase

ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = cli.main(list(argv))
        except SystemExit as exit:
            status = exit.code
    return status, out.getvalue(), err.getvalue()


class CommandLineTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.make_file(".cache/thumbnails/normal/a.png", 20_000)

    def test_rejects_unknown_categories(self):
        status, _, err = run("scan", "--only", "thumbnails,bogus")
        self.assertEqual(status, 2)
        self.assertIn("unknown category bogus", err)

    def test_categories_json(self):
        status, out, _ = run("categories", "--json")
        self.assertEqual(status, 0)
        self.assertEqual([entry["id"] for entry in json.loads(out)], list(BY_ID))

    def test_scan_json(self):
        status, out, _ = run("scan", "--json", "--only", "thumbnails")
        data = json.loads(out)
        self.assertEqual(status, 0)
        self.assertEqual(data["selected"], ["thumbnails"])
        self.assertGreaterEqual(data["selected_size"], 20_000)

    def test_scan_text(self):
        status, out, _ = run("scan", "--no-color", "--only", "thumbnails,trash")
        self.assertEqual(status, 0)
        self.assertIn("Thumbnail Cache", out)
        self.assertIn("Selected: 1 category", out)
        self.assertNotIn("\033[", out)

    def test_clean_needs_confirmation(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            status, _, err = run("clean", "--only", "thumbnails")
        self.assertEqual(status, 2)
        self.assertIn("--yes", err)
        self.assertTrue((self.home / ".cache/thumbnails/normal/a.png").exists())

    def test_dry_run_changes_nothing(self):
        status, out, _ = run("clean", "--no-color", "--dry-run", "--only", "thumbnails")
        self.assertEqual(status, 0)
        self.assertIn("Dry run", out)
        self.assertTrue((self.home / ".cache/thumbnails/normal/a.png").exists())

    def test_clean_yes_json_then_history(self):
        status, out, _ = run("clean", "--yes", "--json", "--only", "thumbnails")
        self.assertEqual(status, 0)
        report = json.loads(out)["report"]
        self.assertEqual([task["state"] for task in report["tasks"]], ["done"])
        self.assertFalse((self.home / ".cache/thumbnails/normal/a.png").exists())
        status, out, _ = run("history", "--json")
        self.assertEqual(json.loads(out)[0]["categories"], {"thumbnails": report["freed"]})

    def test_bash_completion_lists_every_category(self):
        text = (ROOT / "data" / "sparkle-clean.bash-completion").read_text()
        ids = re.search(r'local ids="([^"]+)"', text).group(1).split()
        self.assertEqual(ids, list(BY_ID))


if __name__ == "__main__":
    unittest.main()
