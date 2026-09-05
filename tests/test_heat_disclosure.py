"""P3 热度口径标注契约测试（history/20260905 SEO P1~P5 需求）。

热度数字本身口径透明化：只读模板断言——
- term_detail.html / index.html 的 🔥 热度数字带口径说明（title + 可见脚注）；
- word 级热度（词热度 = 近 7 天报道热度分 + HF 点赞）与单篇报道热度分开说明；
- index.html 的 JS 渲染路径（I18N key）与 SSR 静态路径口径文案一致。

本测试只读模板文件，不 import app、不联网、零 token。
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class HeatDisclosureContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.term_detail = (ROOT / "templates" / "term_detail.html").read_text(encoding="utf-8")
        cls.index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    # ---- term_detail.html ----

    def test_term_detail_has_zh_note(self):
        self.assertIn("词热度 = 近 7 天相关报道热度分（时间加权）+ HuggingFace 点赞数",
                      self.term_detail)

    def test_term_detail_has_en_note(self):
        self.assertIn(
            "Word hotness = heat scores of related reports in last 7 days (recency-weighted) + HuggingFace likes",
            self.term_detail)

    def test_term_detail_has_report_level_note(self):
        self.assertIn("报道热度分 = 社区讨论信号按来源加权 + 发布时间衰减", self.term_detail)
        self.assertIn("Report heat score = community signals weighted by source + time decay",
                      self.term_detail)

    def test_term_detail_word_hot_span_has_title(self):
        # word 级 🔥 热度 span 带 title（口径说明）
        self.assertRegex(
            self.term_detail,
            r'<span class="m-hot" title="{{ hot_note_txt }}">🔥')

    def test_term_detail_report_hot_span_has_title(self):
        # 单篇报道 🔥 热度 span 带 title（口径说明）
        self.assertRegex(
            self.term_detail,
            r'<span class="hd" title="{{ hot_news_note_txt }}">🔥')

    def test_term_detail_has_visible_note_line(self):
        # 词卡内可见口径脚注（SSR，无 JS 也可见）
        self.assertRegex(
            self.term_detail,
            r'<div class="term-note">🔥 {{ hot_note_txt }}</div>')
        self.assertIn(".term-note {", self.term_detail)

    # ---- index.html ----

    def test_index_i18n_zh_has_hot_notes(self):
        self.assertIn('hot_note: "词热度 = 近 7 天相关报道热度分（时间加权）+ HuggingFace 点赞数",',
                      self.index)
        self.assertIn('hot_news_note: "报道热度分 = 社区讨论信号按来源加权 + 发布时间衰减",',
                      self.index)

    def test_index_i18n_en_has_hot_notes(self):
        self.assertIn(
            'hot_note: "Word hotness = heat scores of related reports in last 7 days (recency-weighted) + HuggingFace likes",',
            self.index)
        self.assertIn(
            'hot_news_note: "Report heat score = community signals weighted by source + time decay",',
            self.index)

    def test_index_i18n_footer_l4_both_langs(self):
        self.assertIn('footer_l4: "🔥 热度口径：', self.index)
        self.assertIn('footer_l4: "🔥 Word hotness = heat scores', self.index)

    def test_index_js_word_card_hot_has_title(self):
        # JS 渲染词卡 🔥 用 I18N hot_note 作 title
        self.assertIn('title="${t("hot_note")}"', self.index)

    def test_index_js_news_hot_has_title(self):
        # JS 渲染新闻卡/报道列表 🔥 用 hot_news_note 作 title
        self.assertEqual(self.index.count('title="${t("hot_news_note")}"'), 3)  # metrics + 词卡 top + 展开列表

    def test_index_js_footer_includes_note(self):
        self.assertIn('${t("footer_l4")}<br>', self.index)

    def test_index_ssr_footer_includes_note(self):
        self.assertIn('🔥 {{ hot_note_en }}<br>', self.index)
        self.assertIn('🔥 热度口径：{{ hot_note_zh }}<br>', self.index)

    def test_index_ssr_word_hot_has_title(self):
        self.assertRegex(
            self.index,
            r'<span class="m-hot" title="{{ hot_note_en if is_en else hot_note_zh }}">🔥')


if __name__ == "__main__":
    unittest.main()
