"""Offline contract tests for the word-card Show more control.

These tests intentionally read the template only.  They do not import the
Flask app, start refresh threads, or need any upstream/LLM credentials.
"""

from pathlib import Path
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"


class ShowMoreViewPageContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        start = cls.source.index("  const expandBtn =", cls.source.index("function renderWordCard"))
        end = cls.source.index("  return `<div class=\"term-card\"", start)
        cls.card_controls = cls.source[start:end]

        start = cls.source.index("async function toggleWordExpand")
        end = cls.source.index("function renderWordExpand", start)
        cls.toggle = cls.source[start:end]

    def test_detail_link_is_sibling_of_button(self):
        """The detail link must survive any button loading-state update."""
        button_start = self.card_controls.index('<button class="word-expand-btn"')
        button_end = self.card_controls.index("</button>", button_start)
        button_markup = self.card_controls[button_start:button_end]

        self.assertNotIn("<a ", button_markup)
        self.assertIn('<span class="word-expand-label">', button_markup)
        self.assertIn("</button>", self.card_controls[button_end:])
        self.assertIn(
            '<a class="word-detail-link link-btn official" href="${detailHref}">',
            self.card_controls[button_end:])
        self.assertNotIn('onclick="event.stopPropagation()"', self.card_controls)

    def test_no_expand_branch_uses_same_actions_pattern(self):
        """有/无展开按钮时，「查看热词」都处于同一 .word-actions 容器、同一带框样式。

        The expand button is the only conditional element; without it the
        container holds just the detail link, styled identically to the
        two-control case (link-btn official 带框样式，与展开按钮同框)。
        """
        self.assertIn('<div class="word-actions">', self.card_controls)
        # 统一的详情链接只定义一处（两条分支共用同一 markup，样式必然一致）
        self.assertEqual(
            self.card_controls.count(
                '<a class="word-detail-link link-btn official" href="${detailHref}">'),
            1)
        # 旧的「单链接 + 内联 margin」分支已移除
        self.assertNotIn('style="margin-top:8px"', self.card_controls)
        # 展开按钮是唯一条件元素：无更多报道时容器内只剩详情链接
        self.assertEqual(self.card_controls.count('<button class="word-expand-btn"'), 1)
        self.assertIn(': ""}', self.card_controls)

    def test_loading_state_does_not_replace_button_children(self):
        """A fetch must update only the label, leaving sibling links intact."""
        self.assertNotIn("btn.textContent", self.toggle)
        self.assertIn('label.textContent = "…"', self.toggle)
        self.assertIn("label.textContent = oldText", self.toggle)

    def test_detail_href_uses_encoded_term_helper(self):
        """The refactor must retain the existing encoded /term URL contract."""
        self.assertIn("function escapeTerm(term)", self.source)
        self.assertIn("return encodeURIComponent(String(term || \"\"));", self.source)
        self.assertIn("const detailHref = termHref(term);", self.source)


if __name__ == "__main__":
    unittest.main()
