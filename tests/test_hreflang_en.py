"""P4 双语去重 + 主语言=英文：hreflang zh↔en + x-default→en 渲染与 sitemap en 显式变体。

背景：zh/en 两语言此前各自 self-canonical，页面 head 无 hreflang 互指，
Google 把两语言当无关联的独立页（重复内容）。本分支目标：

- 可索引页（首页 / /term 词条页 / /hf）head 紧跟 canonical 输出三行
  ``link rel=alternate hreflang=zh/en/x-default``（x-default → en 主语言）；
- sitemap 只提交英文显式变体（?lang=en）——首页 `/?lang=en`、/hf 页
  `/hf?lang=en`、词条 `/term/<display>?lang=en`；/terms 单页双语保持裸 URL。
- BASE_URL 未设 → hreflang 不输出、sitemap 空 urlset（行为不变）。

测试为渲染级：临时 DB + Flask test client（仿 tests/test_jsonld.py），
env save/restore、fcntl/requests 打桩、patch 后台刷新线程、空 key 降级。
sitemap 测试词插得「扎实」（origin=news + total_mentions>=3 + 关联卡），
避免将来 P1 质量门槛合并后薄词被过滤导致本用例失效。
"""

import json
import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import quote

BASE = "https://example.test"


def _hf_href(path, lang):
    """与路由 _abs(_lang_url(...)) 同口径的期望绝对 URL。"""
    return f"{BASE}{path}?lang={lang}"


class HreflangEnRenderTest(unittest.TestCase):
    """临时 DB + Flask test client 的真实渲染验证。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "BASE_URL", "SEO_ENABLED",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-hreflang-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        # 空库：import 时 news_store/terms 的 init_db 会建全表（含 keywords 列）
        sqlite3.connect(cls.db_path).close()
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # hreflang 与 sitemap 绝对 URL 需要 BASE_URL；SEO 开（默认值，显式钉住）
        os.environ["BASE_URL"] = BASE
        os.environ["SEO_ENABLED"] = "1"
        # 零 token 降级路径：与 test_term_news.py 一致，key 必须为空
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        cls.news_store = news_store
        cls.terms = terms

        # 与 test_jsonld.py 相同的 import 打桩：fcntl / requests + 禁用后台刷新
        if "fcntl" not in sys.modules:
            fcntl_stub = types.ModuleType("fcntl")
            fcntl_stub.LOCK_EX = 2
            fcntl_stub.LOCK_NB = 4
            fcntl_stub.LOCK_UN = 8
            fcntl_stub.flock = lambda *args: None
            sys.modules["fcntl"] = fcntl_stub
        if "requests" not in sys.modules:
            try:
                import requests  # noqa: F401
            except ModuleNotFoundError:
                requests_stub = types.ModuleType("requests")
                requests_stub.get = lambda *args, **kwargs: None
                requests_stub.post = lambda *args, **kwargs: None
                requests_stub.utils = types.SimpleNamespace(quote=quote)
                requests_stub.exceptions = types.SimpleNamespace(
                    ChunkedEncodingError=Exception,
                    ConnectionError=Exception,
                    ReadTimeout=Exception,
                    JSONDecodeError=Exception,
                    HTTPError=Exception,
                )
                sys.modules["requests"] = requests_stub
        import dims
        import tracker
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            import app as app_module
            importlib.reload(app_module)
        cls.app = app_module

        # 详情页 HF live 慢路径打桩：不联网，返回未命中 → hf_detail=None
        cls._hf_detail_patch = patch.object(
            app_module.tracker, "get_term_detail", return_value={"ok": False})
        cls._hf_detail_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._hf_detail_patch.stop()
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def setUp(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM news_cards")
        conn.execute("DELETE FROM terms")
        conn.execute("DELETE FROM term_snapshots")
        conn.commit()
        conn.close()
        self.app._detail_cache.clear()
        self.client = self.app.app.test_client()

    # ---------- helpers ----------

    def _insert_term(self, canonical="glm-5.3-flash", display="GLM-5.3-Flash",
                     origin="news", total=12, hf_json=None, hot=85.0, rise=2.5):
        """插一个「扎实」的词（news 源、总提及>=3、热度/环比非空），
        与 test_jsonld.py 同列集合；origin=news 不带 hf_json。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "first_seen_at, last_seen_at, total_mentions, hf_json, "
            "cur_hot, cur_rise, cur_novelty) "
            "VALUES (?, ?, '', '', ?, '2026-08-31', '2026-08-31', ?, ?, ?, ?, 0)",
            (canonical, display, origin, total, hf_json or "", hot, rise),
        )
        conn.commit()
        conn.close()

    def _insert_cards(self, canonical="glm-5.3-flash", display="GLM-5.3-Flash",
                      count=3):
        """插入 count 张关键词关联卡，模拟该词的支撑报道。"""
        conn = sqlite3.connect(self.db_path)
        for i in range(count):
            conn.execute(
                "INSERT INTO news_cards (url, title, title_zh, title_en, "
                "published, score, keywords) "
                "VALUES (?, ?, ?, ?, '2026-08-29', 100, ?)",
                (f"https://example.test/news/{i}",
                 f"{display} report {i}", f"{display} 报道{i}",
                 f"{display} report {i}",
                 json.dumps([canonical])),
            )
        conn.commit()
        conn.close()

    _HREFLANG_TPL = ('<link rel="alternate" hreflang="{lang}" '
                     'href="{href}">')

    def _assert_hreflang(self, body, page_path):
        """三行 hreflang（zh / en / x-default→en）各恰好出现一次。"""
        for lang, href in (("zh", _hf_href(page_path, "zh")),
                           ("en", _hf_href(page_path, "en")),
                           ("x-default", _hf_href(page_path, "en"))):
            line = self._HREFLANG_TPL.format(lang=lang, href=href)
            self.assertIn(line, body)
            self.assertEqual(body.count(line), 1, f"{line} 应只出现一次")
        # x-default 必须是 en（主语言英文）
        self.assertIn('hreflang="x-default" href="'
                      + _hf_href(page_path, "en") + '"', body)

    # ---------- tests ----------

    def test_sitemap_lists_only_primary_language_en_urls(self):
        """sitemap 只交英文显式变体：首页 /?lang=en、词条 /term/<display>?lang=en；
        无裸词条 URL、无 lang=zh。"""
        self._insert_term()
        self._insert_cards()
        resp = self.client.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn(f"<loc>{BASE}/?lang=en</loc>", body)  # 首页主语言变体
        self.assertNotIn(f"<loc>{BASE}/</loc>", body)        # 无裸首页
        self.assertIn(f"<loc>{BASE}/terms</loc>", body)      # /terms 单页双语裸 URL
        self.assertIn(f"<loc>{BASE}/hf?lang=en</loc>", body)
        self.assertIn(f"<loc>{BASE}/term/GLM-5.3-Flash?lang=en</loc>", body)
        self.assertNotIn(f"<loc>{BASE}/term/GLM-5.3-Flash</loc>", body)  # 无裸词条
        self.assertNotIn("lang=zh", body)                    # 不交 zh 变体

    def test_term_detail_head_has_hreflang_zh_en_xdefault(self):
        """/term/<slug>?lang=zh 与 ?lang=en 均输出 hreflang 三行（互指相同）。"""
        self._insert_term()
        self._insert_cards()
        for lang in ("zh", "en"):
            resp = self.client.get(f"/term/glm-5.3-flash?lang={lang}")
            self.assertEqual(resp.status_code, 200, lang)
            body = resp.get_data(as_text=True)
            self._assert_hreflang(body, "/term/GLM-5.3-Flash")
            # canonical 与该语言变体一致（self-canonical 保留）
            self.assertIn(f'<link rel="canonical" href="{_hf_href("/term/GLM-5.3-Flash", lang)}">',
                          body, lang)

    def test_homepage_head_has_hreflang_zh_en_xdefault(self):
        """首页 ?lang=zh 与 ?lang=en 均输出 hreflang 三行（/ 变体）。"""
        for lang in ("zh", "en"):
            resp = self.client.get(f"/?lang={lang}")
            self.assertEqual(resp.status_code, 200, lang)
            body = resp.get_data(as_text=True)
            self._assert_hreflang(body, "/")
            self.assertIn(f'<link rel="canonical" href="{_hf_href("/", lang)}">',
                          body, lang)

    def test_hf_page_head_has_hreflang_zh_en_xdefault(self):
        """/hf?lang=zh 与 ?lang=en 均输出 hreflang 三行（/hf 变体）。"""
        for lang in ("zh", "en"):
            with patch.object(self.app, "_hf_models_for", return_value=([], 0)):
                resp = self.client.get(f"/hf?lang={lang}")
            self.assertEqual(resp.status_code, 200, lang)
            body = resp.get_data(as_text=True)
            self._assert_hreflang(body, "/hf")
            self.assertIn(f'<link rel="canonical" href="{_hf_href("/hf", lang)}">',
                          body, lang)

    def test_no_hreflang_and_empty_sitemap_without_base_url(self):
        """BASE_URL 未设（降级环境）→ 页面无 hreflang、sitemap 空 urlset。"""
        self._insert_term()
        with patch.object(self.app.config, "BASE_URL", ""):
            resp = self.client.get("/term/glm-5.3-flash?lang=zh")
            body = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn('rel="alternate" hreflang=', body)
            self.assertNotIn('rel="canonical"', body)

            resp = self.client.get("/sitemap.xml")
            body = resp.get_data(as_text=True)
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("<loc>", body)  # 空 urlset（与历史行为一致）


if __name__ == "__main__":
    unittest.main()
