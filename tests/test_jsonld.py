"""GSC 结构化数据修复回归测试（history/20260831.txt 第 6 点）。

两类测试：

a) 模板级契约（只读模板文件，不 import app，仿 tests/test_show_more_view_page.py）：
   - SoftwareApplication 块不再含 aggregateRating / ratingValue（修复报错 a：
     likes 被当作 ratingValue 超出 Google 允许范围）；
   - 所有 ld+json 块内的动态插值要么以 ``| tojson`` 结尾，要么是白名单内的
     纯数字表达式 / 纯固定文案（无用户文本注入面）。

b) 渲染级（临时 DB + Flask test client，仿 tests/test_term_news.py，零 token
   降级路径，DEEPSEEK_API_KEY / GLM_API_KEY 置空）：
   - term 显示名含双引号 → GET /term/<词> 全部 ld+json 块 json.loads 合法
     （报错 b 回归：`Reports about "GLM-5.3-Flash"` 场景）；
   - hf likes 巨大（99999）→ 不再出现 aggregateRating；
   - GET / → index.html 的 WebSite + ItemList ld+json 块 json.loads 合法。
"""

import json
import os
import re
import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]

_LDJSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_INTERP_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
# 在 ld+json 块内允许的非 tojson 原始插值：纯数字表达式（无引号/特殊字符注入面）。
_ALLOWED_RAW_INTERP = {"loop.index", "word.hf.downloads", "word.term.news_cnt"}
# 固定文案表达式允许出现的标识符（如 `'...' if is_en else '...'`）。
# is_en 是布尔开关；if/else 是 Python 关键字——都不携带用户数据。
_FIXED_COPY_IDENTS = {"is_en", "if", "else"}


def _is_fixed_copy(expr):
    """判定表达式是否纯固定文案：去掉字符串字面量后只剩 is_en 等安全标识符。"""
    rest = re.sub(r"'[^']*'", "", expr)
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rest):
        if ident not in _FIXED_COPY_IDENTS:
            return False
    return True


class JsonLdTemplateContractTest(unittest.TestCase):
    """只读模板的 ld+json 契约断言。"""

    @classmethod
    def setUpClass(cls):
        cls.term_detail = (ROOT / "templates" / "term_detail.html").read_text(encoding="utf-8")
        cls.index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        cls.templates = (("term_detail.html", cls.term_detail),
                         ("index.html", cls.index))

    @staticmethod
    def _ldjson_blocks(source):
        return [m.group(1) for m in _LDJSON_RE.finditer(source)]

    def test_software_application_has_no_aggregate_rating(self):
        """报错 a 修复：SoftwareApplication 块不再有 aggregateRating / ratingValue。

        likes 是点赞数而非评分，删除整块最诚实，也避免再踩「范围」坑；
        SoftwareApplication 无 aggregateRating 仍是合法富媒体结构。
        """
        start = self.term_detail.index('"@type": "SoftwareApplication"')
        end = self.term_detail.index("</script>", start)
        block = self.term_detail[start:end]
        self.assertNotIn("aggregateRating", block)
        self.assertNotIn("ratingValue", block)
        # 全模板兜底：任何 ld+json 块都不再把 hf.likes 当评分输出
        self.assertNotIn('"ratingValue": {{ word.hf.likes }}', self.term_detail)
        self.assertNotIn("ratingValue", self.term_detail)

    def test_all_ldjson_interpolations_are_tojson_or_numeric_or_fixed(self):
        """ld+json 块内所有插值均以 tojson 结尾，或为纯数字/固定文案。"""
        for name, source in self.templates:
            blocks = self._ldjson_blocks(source)
            self.assertTrue(blocks, f"{name} 应包含 ld+json 块")
            for block in blocks:
                for m in _INTERP_RE.finditer(block):
                    expr = m.group(1).strip()
                    if re.search(r"\|\s*tojson\s*$", expr, re.I):
                        continue
                    if expr in _ALLOWED_RAW_INTERP:
                        continue
                    if _is_fixed_copy(expr):
                        continue
                    self.fail(
                        f"{name} ld+json 内发现非 tojson 动态插值（有注入面）: {expr!r}"
                    )

    def test_dynamic_text_keys_end_with_tojson(self):
        """粗扫：`"name": {{` / `"url": {{` / `"headline": {{` / `"description": {{`
        后面的表达式必须以 | tojson 结尾。"""
        for name, source in self.templates:
            for block in self._ldjson_blocks(source):
                for key in ('"name":', '"url":', '"headline":', '"description":'):
                    for m in re.finditer(re.escape(key) + r"\s*\{\{(.*?)\}\}", block, re.S):
                        expr = m.group(1).strip()
                        self.assertRegex(
                            expr, r"\|\s*tojson\s*$",
                            msg=f"{name} 的 {key} 插值未走 tojson: {expr!r}")


class JsonLdRenderTest(unittest.TestCase):
    """临时 DB + Flask test client 的真实渲染验证。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-jsonld-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        # 空库：import 时 news_store/terms 的 init_db 会建全表（含 keywords 列）
        sqlite3.connect(cls.db_path).close()
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
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

        # 与 test_term_news.py 相同的 import 打桩：fcntl / requests + 禁用后台刷新
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
        # （模板 `(word.hf_detail.papers or [])` 对 None 走 Jinja undefined 兜底为空）。
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

    def _insert_term(self, canonical="glm-5.3-flash", display='"GLM-5.3-Flash"',
                     origin="hf", total=3, hf_json=None, hot=10, rise=1.5):
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

    def _insert_card(self, url, title, title_zh=None, keywords="[]"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, "
            "published, score, keywords) VALUES (?, ?, ?, ?, '2026-08-29', 100, ?)",
            (url, title, title_zh or title, title, keywords),
        )
        conn.commit()
        conn.close()

    def _ldjson_blocks(self, html):
        return [m.group(1) for m in _LDJSON_RE.finditer(html)]

    def _assert_blocks_valid(self, html, template_name):
        blocks = self._ldjson_blocks(html)
        self.assertTrue(blocks, f"{template_name} 应输出至少一个 ld+json 块")
        for block in blocks:
            try:
                json.loads(block)
            except json.JSONDecodeError as exc:
                self.fail(f"{template_name} ld+json 块非法 JSON: {exc}\n{block}")
        return blocks

    # ---------- tests ----------

    def test_index_ldjson_blocks_are_valid(self):
        """GET / 的 WebSite + ItemList ld+json 均为合法 JSON。"""
        self._insert_term(display="GLM-5.3-Flash", origin="news", hot=42)
        # 首页 SSR 词卡读 words.json 文件缓存（模式复刻 terms._file_cache_set）
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, "words.json"), "w",
                  encoding="utf-8") as f:
            json.dump({
                "words": {"data": {"terms": [{
                    "id": "glm-5.3-flash", "term": "glm-5.3-flash",
                    "display": "GLM-5.3-Flash", "display_zh": "",
                    "cur_hot": 42, "cur_rise": 1.5, "cur_novelty": 0.0,
                    "official_url": "https://huggingface.co/openai/glm-5.3-flash",
                    "top_news": [],
                }]}, "fetched_at": 123},
            }, f, ensure_ascii=False)

        resp = self.client.get("/?lang=zh")
        self.assertEqual(resp.status_code, 200)
        blocks = self._assert_blocks_valid(resp.get_data(as_text=True), "index.html")
        types_ = {json.loads(b).get("@type") for b in blocks}
        self.assertIn("WebSite", types_)
        self.assertIn("ItemList", types_)
        for block in blocks:
            self.assertNotIn("aggregateRating", block)
            self.assertNotIn("ratingValue", block)

    def test_term_detail_large_likes_has_no_aggregate_rating(self):
        """报错 a 回归：hf likes 巨大（99999）时不再出现 aggregateRating。"""
        self._insert_term(
            display="GLM-5.3-Flash",
            hf_json=json.dumps({
                "full_id": "openai/glm-5.3-flash", "likes": 99999,
                "downloads": 777777,
                "official_url": "https://huggingface.co/openai/glm-5.3-flash",
                "author": "OpenAI", "tags": [],
            }, ensure_ascii=False),
        )

        resp = self.client.get("/term/glm-5.3-flash?lang=zh")
        self.assertEqual(resp.status_code, 200)
        blocks = self._assert_blocks_valid(resp.get_data(as_text=True), "term_detail.html")
        for block in blocks:
            self.assertNotIn("aggregateRating", block)
            self.assertNotIn("ratingValue", block)
            obj = json.loads(block)
            if obj.get("@type") == "SoftwareApplication":
                # downloadCount 保留（纯数字），且整块不再有任何评分字段
                self.assertEqual(obj.get("downloadCount"), 777777)
                self.assertNotIn("aggregateRating", obj)

    def test_term_detail_with_quoted_term_name_renders_valid_ldjson(self):
        """报错 b 回归：term 显示名含双引号时 ld+json 仍为合法 JSON。

        旧模板直接插值导致 `Reports about "GLM-5.3-Flash"` 缺转义；当前模板
        全部走 tojson，双引号被转义为 \\"。
        """
        self._insert_term(
            display='"GLM-5.3-Flash"',
            hf_json=json.dumps({
                "full_id": "openai/glm-5.3-flash", "likes": 1568,
                "downloads": 123456,
                "official_url": "https://huggingface.co/openai/glm-5.3-flash",
                "author": "OpenAI", "tags": [],
            }, ensure_ascii=False),
        )
        self._insert_card("q-1", 'OpenAI says "GLM-5.3-Flash" is fast',
                          keywords='["glm-5.3-flash"]')

        resp = self.client.get("/term/glm-5.3-flash?lang=zh")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        blocks = self._assert_blocks_valid(html, "term_detail.html")
        self.assertGreaterEqual(len(blocks), 2)  # DefinedTerm + ItemList + SoftwareApplication

        itemlist_names = []
        for block in blocks:
            obj = json.loads(block)
            if obj.get("@type") == "ItemList":
                itemlist_names.append(obj.get("name", ""))
        self.assertTrue(
            any('"GLM-5.3-Flash"' in n for n in itemlist_names),
            f"应渲染含引号的 term 名到 ItemList name: {itemlist_names}",
        )
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")


if __name__ == "__main__":
    unittest.main()
