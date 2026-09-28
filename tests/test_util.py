import os
import threading
import unittest

from sparkle_clean.util import Cancelled, disk_usage, format_size, plural, remove_contents
from tests.support import TempHomeTestCase


class FormatSizeTests(unittest.TestCase):
    def test_matches_glib_style(self):
        self.assertEqual(format_size(None), "Unknown")
        self.assertEqual(format_size(0), "0 bytes")
        self.assertEqual(format_size(1), "1 byte")
        self.assertEqual(format_size(999), "999 bytes")
        self.assertEqual(format_size(1000), "1.0 kB")
        self.assertEqual(format_size(12_345), "12.3 kB")
        self.assertEqual(format_size(412_345_678), "412.3 MB")
        self.assertEqual(format_size(1_600_000_000), "1.6 GB")

    def test_plural(self):
        self.assertEqual(plural(1, "file"), "1 file")
        self.assertEqual(plural(1234, "file"), "1,234 files")
        self.assertEqual(plural(2, "category", "categories"), "2 categories")


class DiskUsageTests(TempHomeTestCase):
    def test_missing_folder_is_empty(self):
        usage = disk_usage(self.home / "missing")
        self.assertEqual((usage.bytes, usage.files, usage.partial), (0, 0, False))

    def test_counts_files_once_and_ignores_symlinks(self):
        first = self.make_file("cache/a", 10_000)
        self.make_file("cache/sub/b", 10_000)
        os.link(first, self.home / "cache" / "hardlink")
        outside = self.make_file("outside/big", 100_000)
        os.symlink(outside, self.home / "cache" / "link")
        os.symlink(outside.parent, self.home / "cache" / "dirlink")
        usage = disk_usage(self.home / "cache")
        self.assertEqual(usage.files, 2)
        self.assertGreaterEqual(usage.bytes, 20_000)
        self.assertLess(usage.bytes, 100_000)

    def test_cancel(self):
        self.make_file("cache/a")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            disk_usage(self.home / "cache", cancel=cancel)


class RemoveContentsTests(TempHomeTestCase):
    def test_empties_folder_but_keeps_it(self):
        self.make_file("cache/a")
        self.make_file("cache/deep/er/b")
        removal = remove_contents(self.home / "cache")
        self.assertEqual(os.listdir(self.home / "cache"), [])
        self.assertEqual(removal.removed, 2)
        self.assertGreater(removal.freed, 0)
        self.assertIsNone(removal.errors)

    def test_removes_symlinks_without_following_them(self):
        outside = self.make_file("outside/keep.txt")
        (self.home / "cache").mkdir()
        os.symlink(outside, self.home / "cache" / "file-link")
        os.symlink(outside.parent, self.home / "cache" / "dir-link")
        remove_contents(self.home / "cache")
        self.assertEqual(os.listdir(self.home / "cache"), [])
        self.assertTrue(outside.exists())

    def test_handles_read_only_folders(self):
        self.make_file("trash/files/archive/inner/file")
        os.chmod(self.home / "trash/files/archive/inner", 0o500)
        os.chmod(self.home / "trash/files/archive", 0o500)
        removal = remove_contents(self.home / "trash/files")
        self.assertEqual(os.listdir(self.home / "trash/files"), [])
        self.assertIsNone(removal.errors)

    def test_missing_folder(self):
        removal = remove_contents(self.home / "missing")
        self.assertEqual((removal.freed, removal.removed, removal.errors), (0, 0, None))


if __name__ == "__main__":
    unittest.main()
