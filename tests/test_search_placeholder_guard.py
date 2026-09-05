"""record_search_query 模板占位符过滤回归测试。

背景：首页 JSON-LD Sitelinks SearchBox 的 target 含 schema.org 占位符
{search_term_string}（见 templates/index.html ld+json SearchAction）。Googlebot
等爬虫会按字面量抓取 /search?q={search_term_string}，若原样入库会污染后台
「用户搜索关键词」统计与搜索建议（monitor 页 / /api/search/suggest）。
本测试断言：占位符/含花括号查询不入库，正常查询照常入库。
零 token：纯 sqlite，不触 LLM。
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

# 工作区测试严禁使用真实 LLM key；本测试不触 LLM，仍显式清除。
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GLM_API_KEY", None)

import config  # noqa: E402
import store   # noqa: E402


class SearchPlaceholderGuardTest(unittest.TestCase):
    """临时 sqlite + patch 隔离：不动进程内全局 config/store 状态。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aitw-searchguard-")
        self.db = os.path.join(self._tmp.name, "sponsors.db")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE search_queries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, "
            "lang TEXT, ip TEXT, country TEXT, ts TEXT NOT NULL, "
            "date TEXT NOT NULL)")
        conn.commit()
        conn.close()
        self._patches = [
            patch.object(store, "_DB_OK", True),
            patch.object(config, "DB_PATH", self.db),
            patch.object(config, "ANALYTICS_ENABLED", True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _count(self, q):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM search_queries WHERE query=?",
                (q,)).fetchone()[0]
        finally:
            conn.close()

    def test_jsonld_placeholder_query_not_recorded(self):
        """JSON-LD SearchAction 占位符字面量（真实事故形态）不入库。"""
        store.record_search_query("{search_term_string}", lang="en")
        self.assertEqual(self._count("{search_term_string}"), 0)

    def test_any_braced_query_not_recorded(self):
        """任意含花括号的查询（工具探针形态）不入库。"""
        store.record_search_query("foo {bar} baz", lang="en")
        self.assertEqual(self._count("foo {bar} baz"), 0)

    def test_normal_query_still_recorded(self):
        """正常关键词不受影响，照常入库。"""
        store.record_search_query("openai", lang="en")
        self.assertEqual(self._count("openai"), 1)

    def test_blank_query_still_skipped(self):
        """空串/纯空白仍按原逻辑跳过（回归保护）。"""
        store.record_search_query("   ")
        store.record_search_query("")
        conn = sqlite3.connect(self.db)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM search_queries").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(total, 0)
