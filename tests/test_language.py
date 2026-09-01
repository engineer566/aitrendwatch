"""无网络语言路由回归测试。

测试在导入 Flask app 前禁用后台刷新线程，因此不会触发 RSS/HF/LLM 请求。
"""

import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# 工作区测试严禁使用真实 LLM key；删除环境变量只影响本测试进程。
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GLM_API_KEY", None)

# 生产部署在 Linux；Windows 本地测试没有 fcntl，锁在这些路由测试中不会执行。
if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: None
    )

import tracker  # noqa: E402
import dims  # noqa: E402

tracker.start_background_refresher = lambda: None
dims.start_background_dims_refresher = lambda: None

import app as app_module  # noqa: E402


class LanguageRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config.update(TESTING=True)

    def setUp(self):
        self.client = app_module.app.test_client()
        self.patches = [
            patch.object(app_module.store, "list_slots", return_value=[]),
            patch.object(app_module.store, "record_pageview"),
            patch.object(app_module.store, "record_visit"),
            patch.object(app_module.store, "record_impression"),
            patch.object(app_module.store, "record_search_query"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_homepage_uses_explicit_language_for_ssr_and_links(self):
        ssr_card = {
            "kind": "word",
            "id": "agent",
            "term": "Agent",
            "term_display": "Agent",
            "display_zh": "智能体",
            "dimension": "产品与应用",
            "news_cnt": 1,
            "hot": 10,
            "rise": 1,
            "top_news": [{
                "title": "Agent startup funding",
                "title_zh": "智能体创业公司融资",
                "title_en": "Agent startup funding",
                "official_url": "https://example.test/news",
                "hot": 5,
            }],
        }
        with patch.object(app_module, "_seo_enabled", return_value=True), \
                patch.object(app_module, "_initial_terms_for_ssr",
                             return_value=[ssr_card]) as ssr:
            response = self.client.get("/?lang=en")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<html lang="en">', body)
        self.assertIn("Search trends / authors", body)
        self.assertIn("AI Trend Aggregator", body)
        self.assertIn("Agent startup funding", body)
        self.assertIn("/term/agent?lang=en", body)
        visible = body.split('<script id="sponsor-data"', 1)[0]
        self.assertNotIn("智能体创业公司融资", visible)
        ssr.assert_called_once_with(sort="rise", lang="en")

    def test_term_detail_keeps_requested_language(self):
        detail = {
            "ok": True,
            "term": {
                "term": "Agent",
                "display_zh": "智能体",
                "origin": "news",
                "news_cnt": 1,
                "hot": 10,
                "rise": 1.0,
                "first_seen_at": "2026-08-29",
            },
            "news": [{
                "title": "Agent startup funding",
                "title_zh": "智能体创业公司融资",
                "title_en": "Agent startup funding",
                "summary": "English summary",
                "official_url": "https://example.test/news",
                "source": "Example",
                "hot": 5,
            }],
            "hf": None,
            "hf_detail": None,
            "legacy_hf": False,
        }
        with patch.object(app_module, "_detail_cached", return_value=None), \
                patch.object(app_module, "_detail_set_cache"), \
                patch.object(app_module, "_word_detail", return_value=detail) as build:
            response = self.client.get(
                "/term/agent?lang=en", headers={"Accept-Language": "zh-CN"}
            )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<html lang="en">', body)
        self.assertIn("Related reports", body)
        self.assertIn("Agent startup funding", body)
        self.assertIn("English summary", body)
        self.assertIn("/?lang=en", body)
        self.assertIn("/term/agent?lang=zh", body)
        self.assertNotIn("相关报道", body)
        self.assertNotIn("智能体创业公司融资", body)
        build.assert_called_once_with("agent", lang="en")

    def test_term_detail_home_url_echoes_rank_state(self):
        """返回首页链接必须回显进入时的 view/sort/cat，否则返回后榜单重置为
        默认 Trending，保存的 scrollY 像素落在不同排序的列表上（位置错乱）。

        覆盖 20260901 #7 的边界：Trending 默认排序恰好等于回显缺省，因此只有
        Trending 正常；Hottest/Newest（以及任意 cat 筛选）此前丢失状态。
        """
        detail = {
            "ok": True,
            "term": {"term": "Agent", "display_zh": "智能体", "origin": "news",
                     "news_cnt": 1, "hot": 10, "rise": 1.0,
                     "first_seen_at": "2026-08-29"},
            "news": [], "hf": None, "hf_detail": None, "legacy_hf": False,
        }
        with patch.object(app_module, "_detail_cached", return_value=None), \
                patch.object(app_module, "_detail_set_cache"), \
                patch.object(app_module, "_word_detail", return_value=detail):
            # 模板 href 经 Jinja 转义：& → &amp;
            for query, expect in [
                ("?lang=zh&sort=hot", "/?lang=zh&amp;sort=hot&amp;scroll_back=1"),
                ("?lang=zh&sort=new", "/?lang=zh&amp;sort=new&amp;scroll_back=1"),
                ("?lang=en&sort=hot&view=news&cat=%E6%A8%A1%E5%9E%8B%E4%B8%8E%E6%8A%80%E6%9C%AF",
                 "/?lang=en&amp;view=news&amp;sort=hot&amp;cat=%E6%A8%A1%E5%9E%8B%E4%B8%8E%E6%8A%80%E6%9C%AF&amp;scroll_back=1"),
                ("?lang=zh", "/?lang=zh&amp;scroll_back=1"),
            ]:
                body = self.client.get(f"/term/agent{query}").get_data(as_text=True)
                self.assertIn(f'href="{expect}"', body, f"query={query}")

    def test_homepage_ssr_term_links_carry_rank_state(self):
        """SSR 首屏词链接必须带 requested view/sort/cat（默认项省略），
        与前端 termHref 同口径，保证爬虫/首屏路径返回也恢复正确榜单。"""
        ssr_card = {
            "kind": "word", "id": "agent", "term": "Agent",
            "term_display": "Agent", "display_zh": "智能体",
            "dimension": "产品与应用", "news_cnt": 1, "hot": 10, "rise": 1,
            "top_news": [{"title": "Agent startup funding", "title_zh": "智能体创业公司融资",
                          "title_en": "Agent startup funding",
                          "official_url": "https://example.test/news", "hot": 5}],
        }
        with patch.object(app_module, "_seo_enabled", return_value=True), \
                patch.object(app_module, "_initial_terms_for_ssr",
                             return_value=[ssr_card]) as ssr:
            body = self.client.get("/?lang=zh&sort=hot").get_data(as_text=True)
        self.assertIn('href="/term/agent?lang=zh&amp;sort=hot"', body)
        ssr.assert_called_once_with(sort="hot", lang="zh")

        with patch.object(app_module, "_seo_enabled", return_value=True), \
                patch.object(app_module, "_initial_terms_for_ssr",
                             return_value=[ssr_card]) as ssr:
            body = self.client.get(
                "/?lang=en&sort=new&cat=%E6%A8%A1%E5%9E%8B%E4%B8%8E%E6%8A%80%E6%9C%AF"
            ).get_data(as_text=True)
        self.assertIn('href="/term/agent?lang=en&amp;sort=new&amp;cat=%E6%A8%A1%E5%9E%8B%E4%B8%8E%E6%8A%80%E6%9C%AF"', body)
        ssr.assert_called_once_with(sort="new", lang="en")

        # 默认 Trending（rise/all/words）保持简洁链接
        with patch.object(app_module, "_seo_enabled", return_value=True), \
                patch.object(app_module, "_initial_terms_for_ssr",
                             return_value=[ssr_card]):
            body = self.client.get("/?lang=zh").get_data(as_text=True)
        self.assertIn('href="/term/agent?lang=zh"', body)

    def test_word_api_accepts_explicit_language(self):
        detail = {"ok": True, "term": {}, "news": []}
        with patch.object(app_module, "_word_detail", return_value=detail) as build:
            response = self.client.get("/api/word/agent?lang=en")

        self.assertEqual(response.status_code, 200)
        build.assert_called_once_with("agent", lang="en")

    def test_search_word_hit_keeps_language_in_detail_link(self):
        hit = {
            "id": "agent",
            "term": "Agent",
            "term_display": "Agent",
            "origin": "news",
            "news_cnt": 1,
            "hot": 10,
            "_score": 2.0,
        }
        with patch.object(app_module, "_do_search", return_value=([], [hit], 0)):
            response = self.client.get("/search?q=agent&lang=en")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('<html lang="en">', body)
        self.assertIn("Hot words", body)
        self.assertIn('href="/term/agent?lang=en"', body)
        self.assertIn('href="/?lang=en"', body)

    def test_client_contract_passes_language_to_api_and_detail(self):
        source = Path(app_module.__file__).with_name("templates").joinpath("index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'api/word/${encodeURIComponent(term)}?lang=${encodeURIComponent(LANG)}',
            source,
        )
        self.assertIn(
            'return `/term/${escapeTerm(term)}?${p.toString()}`;',
            source,
        )
        self.assertIn('p.set("lang", LANG);', source)
        self.assertIn('if (currentView !== "words") p.set("view", currentView);', source)
        self.assertIn('if (currentSort !== "rise") p.set("sort", currentSort);', source)
        self.assertIn('if (currentCat !== "all") p.set("cat", currentCat);', source)
        self.assertIn("if (!langFromURL &&", source)

    def test_ssr_cards_all_stay_inside_list_container(self):
        """Regression: every SSR term-card must be nested inside #list.

        The SSR loop previously emitted a stray ``</div>`` after the first
        card (it sat outside the ``{% if t.kind == 'word' %}/{% else %}``
        branches), so ``#list`` closed early and cards 2..N were rendered as
        siblings of ``#list``.  JS ``render()`` then replaced only ``#list``
        content, leaving the leftover SSR cards visible below the 60-card
        stream — duplicate cards numbered 2..20 in production.
        """
        def make_card(kind, i):
            base = {
                "kind": kind,
                "id": f"card-{kind}-{i}",
                "dimension": "模型与技术",
                "hot": 100 - i,
                "rise": 1.0,
            }
            if kind == "word":
                base.update({
                    "term": f"Term{i}",
                    "term_display": f"Term{i}",
                    "display_zh": f"词{i}",
                    "news_cnt": 1,
                    "top_news": [{
                        "title": f"News {i}",
                        "title_zh": f"报道{i}",
                        "official_url": f"https://example.test/{i}",
                        "hot": 5,
                    }],
                })
            elif kind == "model":
                base.update({
                    "term": f"Model{i}",
                    "full_id": f"org/Model{i}",
                    "official_url": f"https://huggingface.co/org/Model{i}",
                    "trending_score": 5,
                    "likes": 3,
                    "downloads": 9,
                    "tags": [],
                    "community": [],
                    "papers": [],
                })
            else:
                base.update({
                    "title": f"News headline {i}",
                    "official_url": f"https://example.test/news/{i}",
                    "source": "Example",
                    "published": "2026-08-30",
                    "summary": "summary",
                })
            return base

        ssr_cards = ([make_card("word", i) for i in range(1, 8)]
                     + [make_card("model", 1), make_card("news", 2)])
        with patch.object(app_module, "_seo_enabled", return_value=True), \
                patch.object(app_module, "_initial_terms_for_ssr",
                             return_value=ssr_cards):
            response = self.client.get("/?lang=zh")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)

        main = body.split("<main>", 1)[1].split("</main>", 1)[0]
        list_open = main.find('<div class="list" id="list">')
        self.assertGreaterEqual(list_open, 0)

        # Walk the <main> block and find where #list's matching </div> is.
        depth = 0
        list_close = None
        for match in re.finditer(r"<div\b|</div>", main):
            if match.group(0).startswith("</"):
                depth -= 1
                if list_close is None and match.start() > list_open and depth == 0:
                    list_close = match.start()
                    break
            elif match.start() >= list_open:
                depth += 1
        self.assertIsNotNone(list_close)

        # Every term-card must open before #list closes; nothing may follow it.
        card_opens = [m.start() for m in re.finditer(r'class="term-card"', main)]
        self.assertEqual(len(card_opens), len(ssr_cards))
        self.assertTrue(all(pos < list_close for pos in card_opens),
                        "all SSR cards must be nested inside #list")

    def test_no_llm_keys_are_used_by_language_tests(self):
        self.assertFalse(os.environ.get("DEEPSEEK_API_KEY"))
        self.assertFalse(os.environ.get("GLM_API_KEY"))


if __name__ == "__main__":
    unittest.main()
