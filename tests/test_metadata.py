import re
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from sparkle_clean import APP_ID, HOMEPAGE, __version__

ROOT = Path(__file__).resolve().parent.parent


class VersionTests(unittest.TestCase):
    def test_newest_appstream_release_is_current_version(self):
        tree = ElementTree.parse(ROOT / "data" / f"{APP_ID}.metainfo.xml")
        newest = tree.find("releases/release")
        self.assertEqual(newest.get("version"), __version__)

    def test_man_page_shows_current_version(self):
        header = (ROOT / "data" / "sparkle-clean.1").read_text().splitlines()[0]
        self.assertIn(f'"sparkle-clean {__version__}"', header)

    def test_appstream_homepage_matches_about_dialog(self):
        tree = ElementTree.parse(ROOT / "data" / f"{APP_ID}.metainfo.xml")
        urls = {url.get("type"): url.text for url in tree.findall("url")}
        self.assertEqual(urls["homepage"], HOMEPAGE)
        self.assertTrue(re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", HOMEPAGE))


if __name__ == "__main__":
    unittest.main()
