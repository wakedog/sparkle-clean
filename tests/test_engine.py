import json
import os
import sys
import unittest
from unittest import mock

from sparkle_clean import engine, history, paths
from sparkle_clean.categories import BY_ID
from sparkle_clean.settings import Settings
from tests.support import TempHomeTestCase

FAKE_HELPER = """\
import json, sys
def emit(**event):
    print(json.dumps(event), flush=True)
emit(event="start", task="apt_cache")
emit(event="output", task="apt_cache", text="$ apt-get clean")
print("some stray line", flush=True)
emit(event="done", task="apt_cache", ok=True, freed=4096, message="")
emit(event="start", task="journal")
sys.exit({status})
"""


class SettingsTests(TempHomeTestCase):
    def test_round_trip_and_clamping(self):
        settings = Settings(tmp_age_days=3, selection={"trash": False})
        settings.save()
        self.assertEqual(Settings.load(), settings)

        (paths.config_dir() / "settings.json").write_text(json.dumps(
            {"tmp_age_days": 9999, "journal_keep_days": "x", "log_age_days": True, "selection": {"a": 1, "b": True}}))
        loaded = Settings.load()
        self.assertEqual(loaded.tmp_age_days, 365)
        self.assertEqual(loaded.journal_keep_days, 7)
        self.assertEqual(loaded.log_age_days, 0)
        self.assertEqual(loaded.selection, {"b": True})

    def test_damaged_file_gives_defaults(self):
        paths.config_dir().mkdir(parents=True)
        (paths.config_dir() / "settings.json").write_text("{not json")
        self.assertEqual(Settings.load(), Settings())


class HistoryTests(TempHomeTestCase):
    def test_newest_first_and_skips_damaged_lines(self):
        history.append(history.HistoryEntry(time=1.0, freed=10, categories={"trash": 10}))
        with open(paths.state_dir() / "history.jsonl", "a") as handle:
            handle.write("garbage\n{\"time\": \"x\"}\n")
        history.append(history.HistoryEntry(time=2.0, freed=20, failed=["orphans"]))
        entries = history.load()
        self.assertEqual([entry.freed for entry in entries], [20, 10])
        self.assertEqual(entries[0].failed, ["orphans"])


class PersonalCleanupTests(TempHomeTestCase):
    def setUp(self):
        super().setUp()
        self.make_file(".cache/thumbnails/normal/a.png", 20_000)
        self.make_file(".cache/thumbnails/large/b.png", 20_000)
        self.make_file(".local/share/Trash/files/report.pdf", 50_000)
        self.make_file(".local/share/Trash/info/report.pdf.trashinfo", 100)
        self.make_file(".cache/pip/http/abc", 30_000)

    def test_scan_measures_without_deleting(self):
        results = engine.scan(Settings(), {"thumbnails", "trash", "pip"})
        self.assertGreaterEqual(results["thumbnails"].size, 40_000)
        self.assertEqual(results["thumbnails"].count, 2)
        self.assertEqual(results["trash"].count, 1)
        self.assertGreaterEqual(results["pip"].size, 30_000)
        self.assertTrue((self.home / ".local/share/Trash/files/report.pdf").exists())

    def test_default_selection_follows_settings(self):
        results = engine.scan(Settings(), {"thumbnails", "trash", "pip"})
        self.assertEqual(engine.default_selection(Settings(), results), ["trash", "thumbnails", "pip"])
        settings = Settings(selection={"trash": False})
        self.assertEqual(engine.default_selection(settings, results), ["thumbnails", "pip"])

    def test_clean_removes_files_and_records_history(self):
        events = []
        report = engine.clean(["thumbnails", "trash"], Settings(), on_event=events.append)
        self.assertEqual(list((self.home / ".cache/thumbnails").iterdir()), [])
        self.assertEqual(list((self.home / ".local/share/Trash/files").iterdir()), [])
        self.assertTrue((self.home / ".cache/pip/http/abc").exists())
        self.assertEqual({o.state for o in report.outcomes.values()}, {engine.DONE})
        self.assertGreaterEqual(report.freed, 90_000)
        self.assertEqual([e.kind for e in events], ["started", "finished-task", "started", "finished-task", "finished"])
        self.assertTrue(report.log_path and report.log_path.exists())
        self.assertIn("Trash: done", report.log_path.read_text())
        self.assertEqual(history.load()[0].freed, report.freed)
        json.dumps(report.to_json())


class HelperProtocolTests(TempHomeTestCase):
    def run_fake_helper(self, status):
        script = self.home / "fake_helper.py"
        script.write_text(FAKE_HELPER.format(status=status))
        with mock.patch.object(paths, "helper_command", return_value=[sys.executable, str(script)]), \
                mock.patch.object(paths, "PKEXEC", "/nonexistent/pkexec"), \
                mock.patch("os.geteuid", return_value=0):
            return engine.clean(["apt_cache", "journal", "trash"], Settings())

    def test_events_are_mapped_to_outcomes(self):
        report = self.run_fake_helper(status=1)
        self.assertEqual(report.outcomes["apt_cache"].state, engine.DONE)
        self.assertEqual(report.outcomes["apt_cache"].freed, 4096)
        self.assertEqual(report.outcomes["journal"].state, engine.SKIPPED)
        self.assertIn("exit status 1", report.outcomes["journal"].message)
        self.assertEqual(report.outcomes["trash"].state, engine.DONE)
        self.assertIn("some stray line", report.log_path.read_text())

    def test_cancelled_authentication(self):
        report = self.run_fake_helper(status=126)
        self.assertEqual(report.outcomes["journal"].message, "Authentication was cancelled")

    def test_helper_argv_uses_pkexec_when_unprivileged(self):
        categories = [BY_ID["apt_cache"], BY_ID["tmp"]]
        with mock.patch("os.geteuid", return_value=1000):
            argv = engine.helper_argv(categories, Settings(tmp_age_days=3))
        self.assertEqual(argv[0], paths.PKEXEC)
        self.assertEqual(argv[-2:], ["apt_cache", "tmp"])
        self.assertIn("--tmp-age-days=3", argv)
        self.assertTrue(os.path.isabs(argv[1]))

    def test_missing_pkexec_skips_system_items(self):
        with mock.patch.object(paths, "PKEXEC", "/nonexistent/pkexec"), mock.patch("os.geteuid", return_value=1000):
            report = engine.clean(["apt_cache"], Settings())
        self.assertEqual(report.outcomes["apt_cache"].state, engine.SKIPPED)
        self.assertIn("pkexec is not installed", report.outcomes["apt_cache"].message)


if __name__ == "__main__":
    unittest.main()
