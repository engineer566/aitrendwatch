"""CSS contract: long English words must not be split mid-word.

This test only reads the templates; no Flask app, no upstream credentials.
"""
from pathlib import Path
import unittest


class WordBreakContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = {
            name: (Path(__file__).resolve().parents[1] / "templates" / name).read_text(encoding="utf-8")
            for name in ["index.html", "term_detail.html", "search.html"]
        }

    def _assert_no_break_all_in_selectors(self, source, selectors):
        for sel in selectors:
            with self.subTest(selector=sel):
                # locate the selector line
                idx = source.find(sel)
                self.assertGreaterEqual(idx, 0, f"selector {sel!r} not found")
                # read the declaration block
                block_start = source.index("{", idx)
                block_end = source.index("}", block_start)
                block = source[block_start:block_end]
                self.assertNotIn("word-break: break-all", block,
                                  f"{sel} still forces mid-word breaks")

    def test_index_page_word_break(self):
        self._assert_no_break_all_in_selectors(
            self.templates["index.html"],
            [".term-name {", ".word-name {", ".word-top-item a {", ".sponsor-text {"],
        )

    def test_term_detail_page_word_break(self):
        self._assert_no_break_all_in_selectors(
            self.templates["term_detail.html"],
            [".term-name {", ".news-item a {", ".paper a {"],
        )

    def test_search_page_word_break(self):
        self._assert_no_break_all_in_selectors(
            self.templates["search.html"],
            [".result-title {"],
        )


if __name__ == "__main__":
    unittest.main()
