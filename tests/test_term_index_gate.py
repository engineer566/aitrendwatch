"""P1 词条详情页可索引质量门槛回归测试（与其 500 个薄页面，不如 50 个扎实的页面）。

两类测试：

a) 行级（纯函数 `terms.term_row_indexable`，临时 env 下 import terms，零 token）：
   - row 为 None/空行 → False；
   - origin ∈ {hf, both} 且 hf_json 非空 → True（HF 词自带 likes/downloads/papers
     等实质内容，不受报道数门槛限制）；
   - 其余词需同时满足 total_mentions >= TERM_INDEX_MIN_NEWS 且
     cur_hot >= TERM_INDEX_MIN_HOT（默认 2 / 0）；
   - 缺失键 / 脏数据永不抛异常。

b) 渲染级（临时 DB + Flask test client，仿 tests/test_jsonld.py，零 token 降级
   路径，DEEPSEEK_API_KEY / GLM_API_KEY 置空）：
   - 薄新闻词（1 篇报道）详情页 GET /term/<词> 200 且 robots=noindex,nofollow，
     不含 index,follow；
   - 达标新闻词（3 篇报道）→ 含 index,follow；
   - HF 词（hf_json 非空、0 篇报道）→ 含 index,follow（实质词不受报道数门槛）；
   - 词池外 HF 长尾回退页（patch tracker.get_term_detail 返回 ok）→ noindex；
   - sitemap 只含达标词（BASE_URL 于 setUpClass 设入并在 tearDownClass 恢复，
     仿 test_jsonld 的 _old_env 模式）：薄词热榜第一也被滤掉。
"""

import json
import os
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

# robots meta 精确匹配串（断言用）
_INDEX_META = 'name="robots" content="index,follow"'
_NOINDEX_META = 'name="robots" content="noindex,nofollow"'

# 需要 save/restore 的环境变量（test_jsonld 的 _old_env 模式 + BASE_URL + 门槛）
_ENV_KEYS = ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
             "DEEPSEEK_API_KEY", "GLM_API_KEY", "BASE_URL",
             "TERM_INDEX_MIN_NEWS", "TERM_INDEX_MIN_HOT")

_FAKE_HF_JSON = json.dumps({
    "full_id": "Acme/ig-hf-42", "likes": 3, "downloads": 5,
    "official_url": "https://huggingface.co/Acme/ig-hf-42",
    "author": "Acme", "tags": [],
}, ensure_ascii=False)


def _switch_env(tmp, db_path, cache_dir):
    """按测试环境切 env（调用方先 save 过 _old_env）。"""
    os.environ["DATA_DIR"] = tmp
    os.environ["NEWS_DB_PATH"] = db_path
    os.environ["CACHE_DIR"] = cache_dir
    # 零 token 降级路径：key 必须为空（worktree 开发纪律）
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ["GLM_API_KEY"] = ""
    # sitemap 绝对 URL 需要 BASE_URL
    os.environ["BASE_URL"] = "https://index-gate.test"
    # 门槛固定为默认值，避免宿主环境变量干扰断言
    os.environ["TERM_INDEX_MIN_NEWS"] = "2"
    os.environ["TERM_INDEX_MIN_HOT"] = "0"


def _restore_env(old_env):
    for key, value in old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TermRowIndexableUnitTest(unittest.TestCase):
    """行级判定（纯函数 term_row_indexable）各分支。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-ig-row-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        # 空库：import 时 terms.init_db 会建全表
        sqlite3.connect(cls.db_path).close()
        _switch_env(cls._tmp.name, cls.db_path, cls.cache_dir)

        import config
        import terms

        importlib.reload(config)
        importlib.reload(terms)
        cls.config = config
        cls.terms = terms

    @classmethod
    def tearDownClass(cls):
        _restore_env(cls._old_env)
        cls._tmp.cleanup()

    # ---------- tests ----------

    def test_none_or_empty_row_not_indexable(self):
        self.assertFalse(self.terms.term_row_indexable(None))
        self.assertFalse(self.terms.term_row_indexable({}))

    def test_hf_and_both_origin_with_hf_json_indexable_without_news(self):
        """origin ∈ {hf, both} 且 hf_json 非空 → 即使 0 报道也 indexable。"""
        for origin in ("hf", "both"):
            row = {"origin": origin, "hf_json": _FAKE_HF_JSON,
                   "total_mentions": 0, "cur_hot": 0}
            self.assertTrue(self.terms.term_row_indexable(row),
                            f"origin={origin} + hf_json 应恒 indexable")

    def test_hf_origin_without_hf_json_uses_news_gate(self):
        """origin=hf 但 hf_json 空（数据丢失/未快照）→ 回落报道数门槛。"""
        base = {"origin": "hf", "hf_json": "", "cur_hot": 100}
        self.assertFalse(self.terms.term_row_indexable({**base, "total_mentions": 1}))
        self.assertTrue(self.terms.term_row_indexable({**base, "total_mentions": 2}))

    def test_news_origin_requires_min_news(self):
        """默认 TERM_INDEX_MIN_NEWS=2：1 篇不达标，2/3 篇达标。"""
        base = {"origin": "news", "cur_hot": 0}
        self.assertFalse(self.terms.term_row_indexable({**base, "total_mentions": 1}))
        self.assertTrue(self.terms.term_row_indexable({**base, "total_mentions": 2}))
        self.assertTrue(self.terms.term_row_indexable({**base, "total_mentions": 3}))
        # origin 缺失（脏行/旧行）按 news 门槛处理
        self.assertFalse(self.terms.term_row_indexable({"total_mentions": 1}))
        self.assertTrue(self.terms.term_row_indexable({"total_mentions": 3}))

    def test_cur_hot_gate_respected(self):
        """cur_hot < TERM_INDEX_MIN_HOT → 不达标（默认 0 门槛下用 patch 验证）。"""
        with patch.object(self.config, "TERM_INDEX_MIN_HOT", 5):
            row = {"origin": "news", "total_mentions": 9, "cur_hot": 4}
            self.assertFalse(self.terms.term_row_indexable(row))
            row["cur_hot"] = 5
            self.assertTrue(self.terms.term_row_indexable(row))
            # cur_hot 缺失 → 按 0 计 → 低于门槛
            del row["cur_hot"]
            self.assertFalse(self.terms.term_row_indexable(row))

    def test_missing_keys_and_junk_values_never_raise(self):
        """缺失键/脏数据永不抛异常：缺键按 0，解析失败按 0。"""
        self.assertFalse(self.terms.term_row_indexable({"origin": "news",
                                                        "total_mentions": "abc",
                                                        "cur_hot": None}))
        # 数字字符串可解析（sqlite 整数列经 JSON 反序列化后可能是 str）
        self.assertTrue(self.terms.term_row_indexable(
            {"origin": "news", "total_mentions": "3", "cur_hot": "0"}))
        self.assertFalse(self.terms.term_row_indexable("not-a-row"))
        self.assertFalse(self.terms.term_row_indexable([1, 2, 3]))


class IndexGateRenderTest(unittest.TestCase):
    """临时 DB + Flask test client 的真实渲染 + sitemap 验证。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-ig-render-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        # 空库：import 时 news_store/terms 的 init_db 会建全表（含 keywords 列）
        sqlite3.connect(cls.db_path).close()
        _switch_env(cls._tmp.name, cls.db_path, cls.cache_dir)

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

        # 详情页 HF live 慢路径打桩：默认不联网返回未命中（hf_detail=None）；
        # 词池外 HF 长尾用例内再局部 patch 为命中。
        cls._hf_detail_patch = patch.object(
            app_module.tracker, "get_term_detail", return_value={"ok": False})
        cls._hf_detail_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._hf_detail_patch.stop()
        _restore_env(cls._old_env)
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

    def _insert_term(self, canonical, origin="news", total=0, hot=0,
                     hf_json=None, display=None):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "first_seen_at, last_seen_at, total_mentions, hf_json, cur_hot, "
            "cur_rise, cur_novelty) "
            "VALUES (?, ?, '', '', ?, '2026-09-01', '2026-09-01', ?, ?, ?, 0, 0)",
            (canonical, display or canonical, origin, total, hf_json or "", hot),
        )
        conn.commit()
        conn.close()

    def _assert_noindex(self, html):
        self.assertIn(_NOINDEX_META, html)
        self.assertNotIn(_INDEX_META, html)

    def _assert_indexable(self, html):
        self.assertIn(_INDEX_META, html)
        self.assertNotIn(_NOINDEX_META, html)

    # ---------- tests ----------

    def test_thin_news_detail_renders_but_noindex(self):
        """薄新闻词（1 篇报道 < TERM_INDEX_MIN_NEWS）：页面 200，robots=noindex。"""
        self._insert_term("ig-thin-1", origin="news", total=1, hot=100)

        resp = self.client.get("/term/ig-thin-1?lang=zh")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self._assert_noindex(html)
        # 零 token 降级路径纪律：key 必须为空
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")

    def test_rich_news_detail_is_indexable(self):
        """达标新闻词（3 篇报道 >= TERM_INDEX_MIN_NEWS）→ index,follow。"""
        self._insert_term("ig-rich-3", origin="news", total=3, hot=100)

        resp = self.client.get("/term/ig-rich-3?lang=zh")
        self.assertEqual(resp.status_code, 200)
        self._assert_indexable(resp.get_data(as_text=True))

    def test_hf_term_indexable_even_with_zero_news(self):
        """HF 词（hf_json 非空、0 篇报道）→ index,follow（实质词不受报道数门槛）。"""
        self._insert_term("ig-hf-42", origin="hf", total=0, hot=0,
                          hf_json=_FAKE_HF_JSON)

        resp = self.client.get("/term/ig-hf-42?lang=zh")
        self.assertEqual(resp.status_code, 200)
        self._assert_indexable(resp.get_data(as_text=True))

    def test_out_of_pool_hf_longtail_detail_is_noindex(self):
        """词池外 HF 长尾回退页（row=None）→ noindex（页面仍正常渲染）。"""
        card = {
            "term": "ig-tail-9", "full_id": "Acme/ig-tail-9",
            "author": "Acme", "type": "模型",
            "official_url": "https://huggingface.co/Acme/ig-tail-9",
            "official_label": "HuggingFace · Acme/ig-tail-9",
            "score": 0, "likes": 5, "downloads": 100, "trending_score": 0,
            "created_at": "", "tags": [], "pipeline_tag": "", "meta": "",
            "community": [], "papers": [],
        }
        with patch.object(self.app.tracker, "get_term_detail",
                          return_value={"ok": True, "term": card}):
            resp = self.client.get("/term/ig-tail-9?lang=zh")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self._assert_noindex(html)
        # 词池外但页面的 HF 官方区块照常渲染
        self.assertIn("Acme/ig-tail-9", html)

    def test_sitemap_only_lists_indexable_terms(self):
        """sitemap 只含达标词：薄词即使热度最高也被滤掉，HF 词 0 报道也保留。"""
        self._insert_term("ig-rich-3", origin="news", total=3, hot=100)
        self._insert_term("ig-thin-1", origin="news", total=1, hot=9999)
        self._insert_term("ig-hf-42", origin="hf", total=0, hot=0,
                          hf_json=_FAKE_HF_JSON)

        resp = self.client.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("/term/ig-rich-3", body)
        self.assertIn("/term/ig-hf-42", body)
        self.assertNotIn("/term/ig-thin-1", body)

    def test_list_terms_for_sitemap_filters_and_orders(self):
        """函数级：按 cur_hot DESC 排序，仅达标词；limit 切片在过滤之后。"""
        self._insert_term("ig-rich-3", origin="news", total=3, hot=100)
        self._insert_term("ig-thin-1", origin="news", total=1, hot=9999)
        self._insert_term("ig-hf-42", origin="hf", total=0, hot=0,
                          hf_json=_FAKE_HF_JSON)

        self.assertEqual(self.terms.list_terms_for_sitemap(),
                         ["ig-rich-3", "ig-hf-42"])
        self.assertEqual(self.terms.list_terms_for_sitemap(limit=1),
                         ["ig-rich-3"])


if __name__ == "__main__":
    unittest.main()
