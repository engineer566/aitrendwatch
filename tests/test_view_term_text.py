"""Offline contract tests for the term-detail link wording.

The link jumps to the word detail page (/term/<term>), so the copy should
read 「查看热词」 / "View term" instead of the old 「查看聚合页」 / "View page".

These tests intentionally read the templates only.  They do not import the
Flask app, start refresh threads, or need any upstream/LLM credentials.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ViewTermTextContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index_source = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.search_source = (ROOT / "templates" / "search.html").read_text(encoding="utf-8")

    # ---- index.html (I18N detail_link) ----

    def test_index_zh_uses_view_term(self):
        self.assertIn('detail_link: "查看热词 →"', self.index_source)

    def test_index_en_uses_view_term(self):
        self.assertIn('detail_link: "View term →"', self.index_source)

    def test_index_drops_old_zh_copy(self):
        self.assertNotIn("查看聚合页", self.index_source)

    def test_index_drops_old_en_copy(self):
        self.assertNotIn("View page", self.index_source)

    # ---- search.html (go-detail span) ----

    def test_search_zh_uses_view_term(self):
        self.assertIn("'查看热词'", self.search_source)

    def test_search_en_uses_view_term(self):
        self.assertIn("'View term'", self.search_source)

    def test_search_drops_old_zh_copy(self):
        self.assertNotIn("查看聚合页", self.search_source)

    def test_search_drops_old_en_copy(self):
        self.assertNotIn("'View page'", self.search_source)


if __name__ == "__main__":
    unittest.main()
