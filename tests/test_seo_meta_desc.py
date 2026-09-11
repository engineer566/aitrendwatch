"""2026-09-11 SEO：BWT「重复 meta description」修复的回归测试。

背景（Bing Webmaster Tools 报「Meta descriptions are duplicated」）：
裸 URL（`/`、`/hf`、`/term/<词>`、`/search`）此前按 Accept-Language 协商语言，
Bingbot 带 en-US 时拿到与显式 `?lang=en` 变体**逐字节相同**的 HTML（同 title、
同 meta description、同 canonical），于是 Bing 索引里有两个 URL 内容一致 →
报重复描述。同一根因还让 `/terms?lang=en`、`/privacy?lang=zh` 这类"伪语言变体"
与裸 URL 重复。

本分支的契约（测试逐条固化）：

1. 英文是**裸 URL 主语言**：显式 `lang=en` 变体 301 收敛到裸 URL（保留其它
   查询参数）；中文只为显式 `lang=zh`（`_request_lang` 不再看 Accept-Language）。
2. 单页内嵌双语的 `/terms`、`/privacy`：任何 `lang=` 参数 301 收敛到裸 URL。
3. `/api/*` 不受影响（前端 JS 显式传 lang=en）。
4. 页面内链（SSR 与 JS）不带 `lang=en`——否则爬虫会反复发现 301 变体。
5. meta description 逐页唯一、长度达标：英文 130-160 字符，中文 ≥90 字符；
   词条页描述含词名 + 报道数 + 最新报道标题（因此逐词唯一）。
6. sitemap 只交裸 URL 主语言集合，不交 `?lang=en` 变体。
"""

import html
import importlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import quote

BASE = "https://example.test"

# 同一语言下应互不相同的描述集合（页面形态 × 语言）
_HREF_LANG_EN = re.compile(r'href="[^"]*\blang=en\b')


def _meta_desc(body):
    """取 <meta name="description"> 内容。"""
    m = re.search(r'<meta name="description" content="(.*?)">', body, re.S)
    return m.group(1) if m else None


def _title(body):
    """取 <title> 文本并按 HTML 实体还原（&amp; → &，长度按真实字符计）。"""
    m = re.search(r"<title>(.*?)</title>", body, re.S)
    return html.unescape(m.group(1)) if m else None


class SeoMetaDescTest(unittest.TestCase):
    """临时 DB + Flask test client 的渲染级验证。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR", "BASE_URL",
                        "SEO_ENABLED", "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-seo-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        sqlite3.connect(cls.db_path).close()
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        os.environ["BASE_URL"] = BASE
        os.environ["SEO_ENABLED"] = "1"
        # 零 token 降级路径：key 必须为空（项目纪律：worktree 不调 LLM）
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        cls.terms = terms

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
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO terms (term, display, display_zh, display_en, origin, "
            "first_seen_at, last_seen_at, total_mentions, hf_json, "
            "cur_hot, cur_rise, cur_novelty) "
            "VALUES (?, ?, '', ?, ?, '2026-08-31', '2026-08-31', ?, ?, ?, ?, 0)",
            (canonical, display, display, origin, total, hf_json or "", hot, rise),
        )
        conn.commit()
        conn.close()

    def _insert_cards(self, canonical="glm-5.3-flash", titles=None):
        titles = titles or ["GLM-5.3-Flash 发布：开源 MoE 模型上新",
                            "GLM-5.3-Flash benchmarks leaked"]
        conn = sqlite3.connect(self.db_path)
        for i, title in enumerate(titles):
            conn.execute(
                "INSERT INTO news_cards (url, title, title_zh, title_en, "
                "published, score, keywords) "
                "VALUES (?, ?, ?, ?, '2026-08-29', 100, ?)",
                (f"https://example.test/news/{i}", title, title, title,
                 json.dumps([canonical])),
            )
        conn.commit()
        conn.close()

    def _get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def _page(self, url, expect=200, **kwargs):
        resp = self._get(url, **kwargs)
        self.assertEqual(resp.status_code, expect, url)
        return resp.get_data(as_text=True)

    def _hf_models(self):
        return patch.object(self.app, "_hf_models_for", return_value=([], 0))

    # ---------- 1. 裸 URL 收敛（301）----------

    def test_lang_en_variants_redirect_to_bare_urls(self):
        """`?lang=en` 显式变体 301 到裸 URL，其它查询参数保留。"""
        for url, target in [
            ("/?lang=en", "/"),
            ("/?lang=en&view=news&sort=hot", "/?view=news&sort=hot"),
            ("/hf?lang=en", "/hf"),
            ("/hf?sort=likes&lang=en", "/hf?sort=likes"),
            ("/term/glm-5.3-flash?lang=en", "/term/glm-5.3-flash"),
            ("/search?q=agent&lang=en", "/search?q=agent"),
            ("/search?lang=en", "/search"),
        ]:
            resp = self._get(url)
            self.assertEqual(resp.status_code, 301, url)
            self.assertEqual(resp.headers.get("Location"), target, url)

    def test_monolingual_pages_redirect_any_lang_param(self):
        """/terms、/privacy 是单页双语：en/zh 语言参数都 301 到裸 URL。"""
        for url, target in [
            ("/terms?lang=en", "/terms"),
            ("/terms?lang=zh", "/terms"),
            ("/privacy?lang=en", "/privacy"),
            ("/privacy?lang=zh", "/privacy"),
        ]:
            resp = self._get(url)
            self.assertEqual(resp.status_code, 301, url)
            self.assertEqual(resp.headers.get("Location"), target, url)

    def test_apis_and_zh_variants_are_not_redirected(self):
        """API 显式 lang=en 仍 200（前端 JS 依赖）；zh 变体不被收敛。"""
        self.assertEqual(self._get("/api/hf?lang=en").status_code, 200)
        self.assertEqual(self._get("/api/dims?lang=en").status_code, 200)
        self.assertEqual(self._get("/?lang=zh").status_code, 200)
        self.assertEqual(self._get("/hf?lang=zh").status_code, 200)

    def test_bare_and_lang_en_never_both_render_200(self):
        """BWT 重复描述的根因回归：裸 URL 与 ?lang=en 不能同时是 200 页面。"""
        self._insert_term()
        self._insert_cards()
        with self._hf_models():
            pairs = [("/", "/?lang=en"), ("/hf", "/hf?lang=en"),
                     ("/term/glm-5.3-flash", "/term/glm-5.3-flash?lang=en")]
        for bare, dup in pairs:
            with self._hf_models():
                self.assertEqual(self._get(bare).status_code, 200, bare)
            self.assertEqual(self._get(dup).status_code, 301, dup)

    # ---------- 2. canonical / hreflang ----------

    def test_canonical_points_at_the_only_indexable_url(self):
        """/ 的 canonical 是自己（裸 URL），不是会跳转的 ?lang=en。"""
        with self._hf_models():
            body = self._page("/")
        self.assertIn(f'<link rel="canonical" href="{BASE}/">', body)
        self.assertNotIn("?lang=en", body)

        body_zh = self._page("/?lang=zh")
        self.assertIn(f'<link rel="canonical" href="{BASE}/?lang=zh">', body_zh)

    # ---------- 3. 内部链接不带 lang=en ----------

    def test_pages_never_link_with_lang_en(self):
        """SSR 内链不带 lang=en：否则爬虫跟着内链反复发现 301 变体。"""
        self._insert_term()
        self._insert_cards()
        pages = [("/", {}), ("/hf", {}), ("/term/glm-5.3-flash", {}),
                 ("/search?q=agent", {}), ("/terms", {}), ("/privacy", {}),
                 ("/?lang=zh", {}), ("/hf?lang=zh", {})]
        for url, kwargs in pages:
            with self._hf_models():
                body = self._page(url, **kwargs)
            found = _HREF_LANG_EN.search(body)
            self.assertIsNone(
                found, f"{url} 出现 lang=en 内链: {found.group(0) if found else ''}")

    # ---------- 4. meta description 唯一性与长度 ----------

    def _collect_descs(self):
        """采集各页面形态（两侧语言）的 meta description。"""
        self._insert_term()
        self._insert_cards()
        out = {}
        with self._hf_models():
            out[("home", "en")] = self._page("/")
        out[("home", "zh")] = self._page("/?lang=zh")
        with self._hf_models():
            out[("hf", "en")] = self._page("/hf")
        out[("hf", "zh")] = self._page("/hf?lang=zh")
        out[("term", "en")] = self._page("/term/glm-5.3-flash")
        out[("term", "zh")] = self._page("/term/glm-5.3-flash?lang=zh")
        out[("terms", "-")] = self._page("/terms")
        out[("privacy", "-")] = self._page("/privacy")
        out[("search", "en")] = self._page("/search")
        out[("search", "zh")] = self._page("/search?lang=zh")
        return {k: _meta_desc(v) for k, v in out.items()}

    def test_descriptions_are_unique_per_page(self):
        """同一语言下任意两个页面的描述都不相同（BWT 重复告警的直接条件）。"""
        descs = self._collect_descs()
        for key, desc in descs.items():
            self.assertTrue(desc, f"{key} 描述为空")
        for lang in ("en", "zh"):
            items = [(k, d) for k, d in descs.items()
                     if k[1] == lang or k[1] == "-"]
            for i, (ka, da) in enumerate(items):
                for kb, db in items[i + 1:]:
                    self.assertNotEqual(da, db, f"{ka} 与 {kb} 描述重复：{da}")

    def test_description_length_bounds(self):
        """英文 130-160 字符（BWT 建议 150-160，130 为下限容忍）、中文 ≥90 字符。"""
        descs = self._collect_descs()
        for (page, lang), desc in descs.items():
            if lang == "zh":
                self.assertGreaterEqual(len(desc), 90, f"{page} zh 描述过短: {desc}")
            elif lang == "en":
                self.assertGreaterEqual(len(desc), 130, f"{page} en 描述过短: {desc}")
                self.assertLessEqual(len(desc), 160, f"{page} en 描述过长: {desc}")

    def test_titles_are_unique_and_en_within_limit(self):
        """首页与 /hf 标题不重复；英文标题 ≤60 字符（超出会被 SERP 截断）。"""
        with self._hf_models():
            home_en = _title(self._page("/"))
            hf_en = _title(self._page("/hf"))
        home_zh = _title(self._page("/?lang=zh"))
        hf_zh = _title(self._page("/hf?lang=zh"))
        for title in (home_en, hf_en, home_zh, hf_zh):
            self.assertTrue(title)
        self.assertNotEqual(home_en, hf_en)
        self.assertNotEqual(home_zh, hf_zh)
        self.assertLessEqual(len(home_en), 60, home_en)
        self.assertLessEqual(len(hf_en), 60, hf_en)
        # JS 覆盖的标题与 SSR 同源（Google 渲染后取 document.title 为准）：
        # 模板经 Jinja tojson 注入，非 ASCII 与 &<>' 均转义，故比对前同口径转义。
        def _js_literal(text):
            return json.dumps(text).replace("&", "\\u0026").replace("<", "\\u003c") \
                                 .replace(">", "\\u003e").replace("'", "\\u0027")

        self.assertTrue("PAGE_TITLES" in self._page("/"), "首页 JS 标题字典缺失")
        self.assertTrue(_js_literal(home_en) in self._page("/"),
                        "首页 PAGE_TITLES.en 与 SSR <title> 不一致")
        # /hf 的语言切换是普通链接（无 JS 改标题），SSR 标题即最终标题
        self.assertTrue("HuggingFace" in hf_en, hf_en)

    # ---------- 5. 词条页描述内容 ----------

    def test_term_description_carries_term_count_and_headline(self):
        """词条描述含词名 + 报道数 + 最新报道标题，故逐词唯一且信息量足。"""
        self._insert_term(total=12)
        self._insert_cards(titles=["GLM-5.3-Flash 发布：开源 MoE 模型上新",
                                   "GLM-5.3-Flash tops the open-source leaderboard"])
        en = _meta_desc(self._page("/term/glm-5.3-flash"))
        zh = _meta_desc(self._page("/term/glm-5.3-flash?lang=zh"))
        self.assertIn("GLM-5.3-Flash", en)
        self.assertIn("12 related reports", en)
        # 英文页取英文标题（中文标题被跳过，见 CJK 用例），标题按预算裁剪故比前缀
        self.assertIn("GLM-5.3-Flash tops the open-source", en)
        self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in en), en)
        self.assertIn("GLM-5.3-Flash", zh)
        self.assertIn("12 篇相关报道", zh)
        self.assertIn("最新报道：", zh)
        self.assertIn("GLM-5.3-Flash 发布", zh)
        self.assertLessEqual(len(en), 160)

    def test_english_term_description_skips_cjk_headlines(self):
        """英文页不把中文标题塞进 description（中英混排）：跳过含 CJK 的标题，
        全是中文报道时改用加长尾句版，描述仍唯一且长度达标。"""
        self._insert_term(total=7)
        self._insert_cards(titles=["某中文标题：智能体耳机发布", "Another Chinese 报道"])
        desc = _meta_desc(self._page("/term/glm-5.3-flash"))
        self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in desc), desc)
        self.assertIn("GLM-5.3-Flash", desc)
        self.assertIn("7 related reports", desc)
        self.assertGreaterEqual(len(desc), 130, desc)
        self.assertLessEqual(len(desc), 160, desc)


    def test_english_term_description_prefers_english_headline(self):
        """中英标题混排：英文页取英文标题，不取排在前面的中文标题。"""
        self._insert_term(total=7)
        self._insert_cards(titles=["某中文标题：智能体耳机发布",
                                   "GLM-5.3-Flash tops the open-source leaderboard"])
        desc = _meta_desc(self._page("/term/glm-5.3-flash"))
        self.assertIn("GLM-5.3-Flash tops the open-source", desc)
        self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in desc), desc)

    def test_term_description_without_reports_uses_hf_facts(self):
        """无报道支撑（纯 HF 模型词）时用 HF 点赞/下载兜底，描述仍非空且唯一。"""
        self._insert_term(canonical="qwen3.8-27b", display="Qwen3.8-27B",
                          origin="hf", total=0,
                          hf_json=json.dumps({"full_id": "Qwen/Qwen3.8-27B",
                                              "likes": 14677,
                                              "downloads": 7322476}))
        en = _meta_desc(self._page("/term/qwen3.8-27b"))
        zh = _meta_desc(self._page("/term/qwen3.8-27b?lang=zh"))
        self.assertIn("Qwen3.8-27B", en)
        self.assertIn("14677 likes", en)
        self.assertIn("7322476 downloads", en)
        self.assertIn("Qwen3.8-27B", zh)
        self.assertIn("14677 点赞", zh)

    # ---------- 6. sitemap ----------

    def test_sitemap_has_no_lang_en_variants(self):
        self._insert_term()
        self._insert_cards()
        body = self._page("/sitemap.xml")
        self.assertIn(f"<loc>{BASE}/</loc>", body)
        self.assertIn(f"<loc>{BASE}/hf</loc>", body)
        self.assertIn(f"<loc>{BASE}/term/GLM-5.3-Flash</loc>", body)
        self.assertNotIn("?lang=en", body)
        self.assertNotIn("lang=zh", body)

    # ---------- 7. 语言解析不再看 Accept-Language ----------

    def test_request_language_ignores_accept_language(self):
        """裸 URL 恒为英文页（不再按 Accept-Language 协商），zh 只认显式参数。"""
        for header in ("zh-CN,zh;q=0.9", "en-US,en;q=0.9"):
            body = self._page("/", headers={"Accept-Language": header})
            self.assertIn('<html lang="en">', body, header)
            body_zh = self._page("/?lang=zh", headers={"Accept-Language": header})
            self.assertIn('<html lang="zh-CN">', body_zh, header)


if __name__ == "__main__":
    unittest.main()
