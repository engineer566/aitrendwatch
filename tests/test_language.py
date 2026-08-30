"""无网络语言路由回归测试。

测试在导入 Flask app 前禁用后台刷新线程，因此不会触发 RSS/HF/LLM 请求。
"""

import os
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
        ssr.assert_called_once_with(lang="en")

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
            'return `/term/${escapeTerm(term)}?lang=${encodeURIComponent(LANG)}`;',
            source,
        )
        self.assertIn('p.set("lang", LANG);', source)
        self.assertIn("if (!langFromURL &&", source)

    def test_no_llm_keys_are_used_by_language_tests(self):
        self.assertFalse(os.environ.get("DEEPSEEK_API_KEY"))
        self.assertFalse(os.environ.get("GLM_API_KEY"))


if __name__ == "__main__":
    unittest.main()
