"""P5 meta keywords 清理 + 报道来源可见化契约测试（history/20260905 SEO P1~P5 需求）。

- 所有公开模板不得再输出 <meta name="keywords">（Google 不参考 keywords，
  且对「堆砌/门页」观感负分）；term_detail.html 的 page_keywords 变量一并删除。
- 聚合报道在用户可见处要有「来源」标注：index.html SSR 词卡 top 报道、
  JS 渲染路径（词卡 top / 展开列表）与 term_detail.html 报道列表都渲染 n.source；
  展开列表的 .src 类修正为真正显示来源（此前误显发布日期，日期改走 .pm）。

本测试只读模板文件，不 import app、零 token。
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MetaKeywordsRemovedTest(unittest.TestCase):
    """所有模板不再输出 meta keywords。"""

    @classmethod
    def setUpClass(cls):
        cls.templates = {
            p.name: p.read_text(encoding="utf-8")
            for p in (ROOT / "templates").glob("*.html")
        }

    def test_no_template_has_meta_keywords(self):
        for name, src in self.templates.items():
            self.assertNotIn('<meta name="keywords"', src,
                             f"{name} 仍含 <meta name=\"keywords\">")

    def test_term_detail_has_no_page_keywords(self):
        self.assertNotIn("page_keywords", self.templates["term_detail.html"])

    def test_index_has_no_meta_keywords(self):
        self.assertNotIn("<meta name=\"keywords\"", self.templates["index.html"])

    def test_search_has_no_dynamic_keywords(self):
        self.assertNotIn("meta name=\"keywords\"", self.templates["search.html"])

    def test_hf_has_no_meta_keywords(self):
        self.assertNotIn("meta name=\"keywords\"", self.templates["hf.html"])


class SourceAttributionVisibleTest(unittest.TestCase):
    """聚合报道的来源标注在用户可见处渲染。"""

    @classmethod
    def setUpClass(cls):
        cls.index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.term_detail = (ROOT / "templates" / "term_detail.html").read_text(encoding="utf-8")

    def test_index_ssr_word_top_shows_source(self):
        # SSR 词卡 top 报道：标题链接原文后跟来源标签（hd span 可能带 P3 口径 title 属性）
        self.assertRegex(
            self.index,
            r'<div class="word-top-item"><a href="\{\{ n\.official_url \}\}"[^>]*>.*?</a><span class="hd"[^>]*>🔥 \{\{ n\.hot \}\}</span>\{% if n\.source %\}<span class="src">\{\{ n\.source \}\}</span>\{% endif %\}</div>')

    def test_index_js_word_card_top_shows_source(self):
        self.assertIn('${n.source ? `<span class="src">${escapeHtml(n.source)}</span>` : ""}',
                      self.index)

    def test_index_js_expand_list_shows_source_and_date(self):
        # 展开列表：.src 显示来源、.pm 显示日期（原缺陷：.src 误显日期）
        self.assertIn('${n.source ? `<span class="src">${escapeHtml(n.source)}</span>` : ""}',
                      self.index)
        self.assertIn('${n.published ? `<span class="pm">${escapeHtml(n.published)}</span>` : ""}',
                      self.index)

    def test_term_detail_report_shows_source(self):
        self.assertIn('{% if n.source %}<span class="src">{{ n.source }}</span>{% endif %}',
                      self.term_detail)

    def test_news_title_links_original(self):
        # 标题外链原文（official_url）是既有契约，固化防回归
        self.assertIn('href="{{ n.official_url }}" target="_blank" rel="noopener"',
                      self.term_detail)


if __name__ == "__main__":
    unittest.main()
