import io
import json
import os
import time
import unittest
from unittest import mock

from sparkle_clean import helper
from sparkle_clean.categories import CATEGORIES
from tests.support import TempHomeTestCase

DAY = 86400

AUTOREMOVE_OUTPUT = """\
NOTE: This is only a simulation!
Reading package lists...
The following packages will be REMOVED:
  libfoo1 linux-headers-6.8.0-31
0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded.
Remv linux-headers-6.8.0-31 [6.8.0-31.31]
Remv libfoo1:i386 [1.0-2]
"""

SNAP_LIST_OUTPUT = """\
Name      Version     Rev    Tracking         Publisher    Notes
core22    20240111    1122   latest/stable    canonical**  base,disabled
core22    20240408    1380   latest/stable    canonical**  base
firefox   125.0.2-1   4173   latest/stable/…  mozilla**    disabled
firefox   125.0.3-1   4209   latest/stable/…  mozilla**    -
evil;rm   1.0         12     latest/stable    someone      disabled
local     0.1         x3     -                -            disabled,classic
"""


class ParserTests(unittest.TestCase):
    def test_autoremove(self):
        self.assertEqual(helper.parse_autoremove(AUTOREMOVE_OUTPUT), ["linux-headers-6.8.0-31", "libfoo1:i386"])

    def test_snap_list_only_returns_valid_disabled_revisions(self):
        self.assertEqual(helper.parse_snap_list(SNAP_LIST_OUTPUT),
                         [("core22", "1122"), ("firefox", "4173"), ("local", "x3")])

    def test_installed_sizes(self):
        sizes = helper.parse_installed_sizes("libfoo1\tamd64\t100\nlibfoo1\ti386\t90\nbar\tall\t5\nbad\n", "amd64")
        self.assertEqual(sizes["libfoo1"], 100 * 1024)
        self.assertEqual(sizes["libfoo1:i386"], 90 * 1024)
        self.assertEqual(sizes["bar"], 5 * 1024)

    def test_journal_head_time(self):
        when = 1_790_000_000.5
        archived = f"system@{'ab' * 16}-{0x1ab03e:016x}-{int(when * 1e6):016x}.journal"
        self.assertAlmostEqual(helper.journal_head_time(archived), when, places=3)
        disposed = f"user-1000@{int(when * 1e6):016x}-{0xdeadbeef:016x}.journal~"
        self.assertAlmostEqual(helper.journal_head_time(disposed), when, places=3)
        self.assertIsNone(helper.journal_head_time("system.journal"))
        self.assertIsNone(helper.journal_head_time("system@garbage.journal"))


class ArgumentTests(unittest.TestCase):
    def test_accepts_known_tasks(self):
        options = helper.parse_args(["clean", "--tmp-age-days=3", "tmp", "apt_cache"])
        self.assertEqual(options.tasks, ["tmp", "apt_cache"])
        self.assertEqual(options.tmp_age_days, 3)

    def test_rejects_anything_else(self):
        for argv in (
            ["clean", "bogus"],
            ["clean", "--tmp-age-days=0", "tmp"],
            ["clean", "--journal-days", "7; rm -rf /", "journal"],
            ["clean", "--log-age-days=-1", "old_logs"],
            ["clean", "--tmp-age-days=+5", "tmp"],
            ["clean"],
            ["run", "/bin/sh"],
        ):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    helper.parse_args(argv)

    def test_refuses_to_run_unprivileged(self):
        with mock.patch("os.geteuid", return_value=1000), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(helper.main(["clean", "apt_cache"]), 2)

    def test_tasks_match_privileged_categories_in_display_order(self):
        self.assertEqual([c.id for c in CATEGORIES if c.privileged], list(helper.TASKS))
        self.assertEqual(set(helper.TASK_FUNCTIONS), set(helper.TASKS))


class SelectionTests(TempHomeTestCase):
    def names(self, found):
        return sorted(os.path.relpath(path, self.home) for _, _, path, _ in found)

    def test_rotated_logs(self):
        for name in ("syslog", "syslog.1", "kern.log.2.gz", "Xorg.0.log.old", "Xorg.0.log", "dpkg.log",
                     "apt/history.log.1.gz", "journal/abc/system@1.journal.gz"):
            self.make_file(f"log/{name}")
        found = helper.iter_rotated_logs(root=str(self.home / "log"))
        self.assertEqual(self.names(found), ["log/Xorg.0.log.old", "log/apt/history.log.1.gz",
                                             "log/kern.log.2.gz", "log/syslog.1"])

    def test_rotated_logs_minimum_age(self):
        self.make_file("log/new.1", age_days=1)
        self.make_file("log/old.1", age_days=30)
        found = helper.iter_rotated_logs(root=str(self.home / "log"), min_age_days=7)
        self.assertEqual(self.names(found), ["log/old.1"])

    def test_stale_tmp_rules(self):
        root = self.home / "tmp"
        stale = self.make_file("tmp/stale")
        self.make_file("tmp/nested/stale")
        self.make_file("tmp/.X0-lock")
        self.make_file("tmp/nested/.X0-lock")
        self.make_file("tmp/.X11-unix/something")
        self.make_file("tmp/systemd-private-abc-colord.service-xyz/tmp/file")
        self.make_file("tmp/snap-private-tmp/snap.firefox/tmp/file")
        in_use = self.make_file("tmp/in-use")
        recent = self.make_file("tmp/recent")
        outside = self.make_file("elsewhere/secret")
        os.symlink(outside, root / "file-link")
        os.symlink(outside.parent, root / "dir-link")

        now = time.time() + 30 * DAY  # ctime cannot be backdated, so move "now" instead
        os.utime(recent, (now - DAY, now - DAY))
        st = in_use.stat()
        found = helper.iter_stale_tmp(age_days=7, now=now, roots=(str(root),),
                                      open_files=frozenset({(st.st_dev, st.st_ino)}))
        self.assertEqual(self.names(found), ["tmp/nested/.X0-lock", "tmp/nested/stale", "tmp/stale"])
        self.assertTrue(stale.exists())

    def test_crash_reports(self):
        for name in ("a.crash", "b.upload", "c.uploaded", "notes.txt", "sub/d.crash"):
            self.make_file(f"crash/{name}")
        found = helper.iter_crash_reports(root=str(self.home / "crash"))
        self.assertEqual(self.names(found), ["crash/a.crash", "crash/b.upload", "crash/c.uploaded"])

    def test_expired_journals(self):
        directory = self.home / "journal" / "machine"
        old = time.time() - 30 * DAY
        new = time.time() - DAY
        names = {
            "system.journal": time.time(),
            f"system@{'0' * 32}-{1:016x}-{int(old * 1e6):016x}.journal": old,
            f"system@{'0' * 32}-{2:016x}-{int(new * 1e6):016x}.journal": new,
            f"user-1000@{int(old * 1e6):016x}-{3:016x}.journal~": old,
        }
        for name in names:
            self.make_file(f"journal/machine/{name}")
        with mock.patch.object(helper, "JOURNAL_ROOTS", (str(self.home / "journal"),)):
            expired = self.names(helper.iter_expired_journals(keep_days=7))
            everything = self.names(helper.iter_journal_files())
        self.assertEqual(len(everything), 4)
        self.assertEqual(expired, sorted(os.path.relpath(directory / n, self.home)
                                         for n, t in names.items() if t == old))

    def test_delete_files_reports_progress(self):
        self.make_file("log/a.1")
        self.make_file("log/b.old")
        stream = io.StringIO()
        freed = helper.delete_files(helper.iter_rotated_logs(root=str(self.home / "log")),
                                    helper.Reporter(stream), "old_logs")
        self.assertGreater(freed, 0)
        self.assertEqual(os.listdir(self.home / "log"), [])
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(events[-1]["text"], "Removed 2 file(s)")


if __name__ == "__main__":
    unittest.main()
