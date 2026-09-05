"""词条「近 7 天活跃度」趋势迷你图回归测试（P2 原创增量：term_snapshots 数据化）。

两类测试：

a) 单元（仅 import config/news_store/terms，临时 DB + env，仿
   tests/test_jsonld.py 的 setUpClass 前半段）：
   - 同一天多 cycle 取当日最后一个（末次刷新值），backfill 合成 "-00" 周期
     不覆盖真实刷新周期；仅 backfill 的日子也成数据点；
   - 输出按日期升序；days 截断（默认 7 / 显式 N）；
   - 不足 2 个数据点 / 全部 win7_cnt==0 / 空表 / 无此词 → []；
   - DB 不可用（连接异常 / 路径不存在）→ []，不抛。

b) 渲染级（临时 DB + Flask test client，零 token 降级路径，key 置空）：
   - 插词 + 插 ≥2 天 snapshots → GET /term/<词>?lang=zh 与 ?lang=en 均含
     趋势区块（.trend-block）与对应语言文案（近 7 天活跃度 / 7-day
     activity）+ MM-DD 柱标签 + title 精确值文案；
   - 不插 snapshots → 区块不出现；/api/word 顶层带 trend 数组；未命中词
     trend=[]。
"""

import os
import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class TermTrendUnitTests(unittest.TestCase):
    """get_term_trend 纯单元测试（terms 层，不 import app）。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-term-trend-unit-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        # 空库：import 时 terms.init_db 会建 terms / term_snapshots 全表
        sqlite3.connect(cls.db_path).close()
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # 零 token 降级路径：与其余测试一致，key 必须为空
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

        # terms 模块本身不需要 requests/fcntl；预置与其余套件相同的 import
        # 打桩，保证后续同类套件（import app）行为一致。
        if "fcntl" not in sys.modules:
            fcntl_stub = types.ModuleType("fcntl")
            fcntl_stub.LOCK_EX = 2
            fcntl_stub.LOCK_NB = 4
            fcntl_stub.LOCK_UN = 8
            fcntl_stub.flock = lambda *args: None
            sys.modules["fcntl"] = fcntl_stub
        if "requests" not in sys.modules:
            requests_stub = types.ModuleType("requests")
            requests_stub.get = lambda *args, **kwargs: None
            requests_stub.post = lambda *args, **kwargs: None
            sys.modules["requests"] = requests_stub

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def setUp(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM term_snapshots")
        conn.commit()
        conn.close()

    def _snap(self, term, cycle, news_cnt, win7_cnt, score_sum=0, signal_sum=0):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, "
            "score_sum, signal_sum) VALUES (?, ?, ?, ?, ?, ?)",
            (term, cycle, news_cnt, win7_cnt, score_sum, signal_sum),
        )
        conn.commit()
        conn.close()

    # ---------- 聚合/排序 ----------

    def test_same_day_last_cycle_wins_backfill_00_does_not_override(self):
        """同一天多 cycle 取当日最后一个；backfill 合成 "-00" 排在真实周期前。"""
        self._snap("gpt-5", "2026-08-29-00", 2, 50)   # backfill 合成（更早）
        self._snap("gpt-5", "2026-08-29-01", 2, 5)
        self._snap("gpt-5", "2026-08-29-07", 3, 9)    # 当日最后一次刷新
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        out = self.terms.get_term_trend("gpt-5")
        self.assertEqual(out, [
            {"date": "2026-08-28", "win7_cnt": 4, "news_cnt": 1},
            {"date": "2026-08-29", "win7_cnt": 9, "news_cnt": 3},
        ])

    def test_backfill_only_day_is_a_data_point(self):
        """某日只有 backfill 合成周期（无真实刷新）也输出该日数据点。"""
        self._snap("gpt-5", "2026-08-27-00", 2, 2)    # 仅 backfill
        self._snap("gpt-5", "2026-08-29-07", 3, 9)
        out = self.terms.get_term_trend("gpt-5")
        self.assertEqual([p["date"] for p in out], ["2026-08-27", "2026-08-29"])
        self.assertEqual(out[0], {"date": "2026-08-27", "win7_cnt": 2,
                                  "news_cnt": 2})

    def test_output_ascending_even_if_inserted_out_of_order(self):
        """输出按日期升序，不依赖插入顺序（SQL 按 cycle ASC）。"""
        self._snap("gpt-5", "2026-08-30-07", 1, 7)
        self._snap("gpt-5", "2026-08-28-07", 1, 4)
        self._snap("gpt-5", "2026-08-29-07", 1, 9)
        out = self.terms.get_term_trend("gpt-5")
        self.assertEqual([p["date"] for p in out],
                         ["2026-08-28", "2026-08-29", "2026-08-30"])

    # ---------- days 截断 ----------

    def test_days_truncation_keeps_last_n(self):
        for i in range(10):
            day = f"2026-08-{20 + i:02d}"
            self._snap("gpt-5", f"{day}-13", 1, i + 1)
        out = self.terms.get_term_trend("gpt-5", days=7)
        self.assertEqual(len(out), 7)
        self.assertEqual([p["date"] for p in out],
                         [f"2026-08-{d:02d}" for d in range(23, 30)])
        self.assertEqual([p["win7_cnt"] for p in out],
                         [4, 5, 6, 7, 8, 9, 10])

    def test_default_days_is_seven(self):
        for i in range(10):
            day = f"2026-08-{20 + i:02d}"
            self._snap("gpt-5", f"{day}-13", 1, i + 1)
        out = self.terms.get_term_trend("gpt-5")
        self.assertEqual(len(out), 7)
        self.assertEqual(out[-1]["date"], "2026-08-29")

    def test_days_zero_returns_empty(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        self.assertEqual(self.terms.get_term_trend("gpt-5", days=0), [])

    def test_invalid_days_falls_back_to_default(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        out = self.terms.get_term_trend("gpt-5", days="oops")
        self.assertEqual(len(out), 2)   # 非法 days → 默认 7，不截断

    # ---------- 隐藏条件 ----------

    def test_fewer_than_two_points_returns_empty(self):
        self._snap("gpt-5", "2026-08-29-07", 3, 9)
        self.assertEqual(self.terms.get_term_trend("gpt-5"), [])

    def test_all_zero_win7_returns_empty(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 0)
        self._snap("gpt-5", "2026-08-29-13", 1, 0)
        self.assertEqual(self.terms.get_term_trend("gpt-5"), [])

    def test_empty_table_returns_empty(self):
        self.assertEqual(self.terms.get_term_trend("gpt-5"), [])

    def test_other_terms_not_leaked(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        self.assertEqual(self.terms.get_term_trend("claude"), [])

    # ---------- 词形/异常 ----------

    def test_alias_query_matches_canonical_key(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        self.assertEqual(self.terms.get_term_trend("GPT-5"),
                         self.terms.get_term_trend("gpt-5"))
        self.assertEqual(self.terms.get_term_trend("Gpt-5"),
                         self.terms.get_term_trend("gpt-5"))

    def test_db_connection_error_returns_empty(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        with patch.object(self.terms, "_conn",
                          side_effect=RuntimeError("db down")):
            self.assertEqual(self.terms.get_term_trend("gpt-5"), [])

    def test_db_path_missing_returns_empty(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-13", 1, 9)
        old = self.terms.config.NEWS_DB_PATH
        self.terms.config.NEWS_DB_PATH = os.path.join(
            self._tmp.name, "missing", "news.db")
        try:
            self.assertEqual(self.terms.get_term_trend("gpt-5"), [])
        finally:
            self.terms.config.NEWS_DB_PATH = old


class TermTrendRenderTests(unittest.TestCase):
    """临时 DB + Flask test client：详情页趋势区块真实渲染。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-term-trend-r-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        sqlite3.connect(cls.db_path).close()
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
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

        # fcntl / requests 打桩 + 禁用后台刷新（与 test_jsonld.py 同款 harness）
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
                sys.modules["requests"] = requests_stub
        import dims
        import tracker
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            import app as app_module
            importlib.reload(app_module)
        cls.app = app_module
        # 详情页 HF live 慢路径打桩：不联网，返回未命中
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
        conn.execute("DELETE FROM terms")
        conn.execute("DELETE FROM term_snapshots")
        conn.commit()
        conn.close()
        self.app._detail_cache.clear()
        self.client = self.app.app.test_client()

    def _insert_term(self, canonical="gpt-5"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "first_seen_at, last_seen_at, total_mentions, hf_json, "
            "cur_hot, cur_rise, cur_novelty) "
            "VALUES (?, 'GPT-5', '', '', 'news', '2026-08-28', '2026-08-29', "
            "3, '', 42, 0.5, 0.1)",
            (canonical,),
        )
        conn.commit()
        conn.close()

    def _snap(self, term, cycle, news_cnt, win7_cnt):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, "
            "score_sum, signal_sum) VALUES (?, ?, ?, ?, 0, 0)",
            (term, cycle, news_cnt, win7_cnt),
        )
        conn.commit()
        conn.close()

    def _insert_two_days(self):
        self._snap("gpt-5", "2026-08-28-13", 1, 4)
        self._snap("gpt-5", "2026-08-29-01", 2, 5)
        self._snap("gpt-5", "2026-08-29-07", 3, 9)   # 当日末次刷新

    # ---------- tests ----------

    def test_detail_page_shows_trend_block_zh_and_en(self):
        self._insert_term()
        self._insert_two_days()

        zh = self.client.get("/term/gpt-5?lang=zh")
        self.assertEqual(zh.status_code, 200)
        html_zh = zh.get_data(as_text=True)
        self.assertIn('class="trend-block"', html_zh)
        self.assertIn("近 7 天活跃度", html_zh)
        # MM-DD 柱标签（两日都在）
        self.assertIn('class="trend-date">08-28<', html_zh)
        self.assertIn('class="trend-date">08-29<', html_zh)
        # 柱高按 win7_cnt/max 百分比缩放：9 是最大值 → 100.0%
        self.assertIn("height: 100.0%", html_zh)
        # title 精确值文案（双语各自成文）
        self.assertIn("2026-08-29：近 7 天 9 篇报道", html_zh)

        en = self.client.get("/term/gpt-5?lang=en")
        self.assertEqual(en.status_code, 200)
        html_en = en.get_data(as_text=True)
        self.assertIn('class="trend-block"', html_en)
        self.assertIn("7-day activity", html_en)
        self.assertIn('class="trend-date">08-28<', html_en)
        self.assertIn('class="trend-date">08-29<', html_en)
        self.assertIn("2026-08-29: 9 reports (last 7 days)", html_en)

        # 零 token 降级路径
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")

    def test_detail_page_hides_trend_block_without_snapshots(self):
        self._insert_term()
        for lang in ("zh", "en"):
            resp = self.client.get(f"/term/gpt-5?lang={lang}")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            # 注意：模板 <style> 里恒有 .trend-block CSS 规则，负断言须查渲染出的
            # 区块元素（含 '>'），不能全页搜 "trend-block"。
            self.assertNotIn('<div class="trend-block">', html)
            self.assertNotIn("7-day activity", html)
            self.assertNotIn("近 7 天活跃度", html)

    def test_detail_page_hides_trend_block_with_single_point(self):
        """仅 1 个数据点（<2 点）→ 区块不出现。"""
        self._insert_term()
        self._snap("gpt-5", "2026-08-29-07", 3, 9)
        resp = self.client.get("/term/gpt-5?lang=zh")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('<div class="trend-block">',
                         resp.get_data(as_text=True))

    def test_word_detail_and_api_carry_trend_field(self):
        self._insert_term()
        self._insert_two_days()
        data = self.app._word_detail("GPT-5", lang="zh")
        self.assertTrue(data["ok"])
        self.assertEqual(data["trend"], [
            {"date": "2026-08-28", "win7_cnt": 4, "news_cnt": 1},
            {"date": "2026-08-29", "win7_cnt": 9, "news_cnt": 3},
        ])

        resp = self.client.get("/api/word/gpt-5?lang=zh")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["trend"], data["trend"])

    def test_miss_and_hf_fallback_branches_carry_empty_trend(self):
        """未命中词池 / HF 长尾回退两分支 trend=[]（模板据此隐藏）。"""
        # miss 分支：_word_detail 返回 {"ok": False, "trend": []}
        data = self.app._word_detail("definitely-no-such-term-xyz", lang="zh")
        self.assertFalse(data["ok"])
        self.assertEqual(data.get("trend"), [])
        # 路由层 404 响应自带错误结构（不经过 _word_detail 的 dict）
        resp = self.client.get("/api/word/definitely-no-such-term-xyz")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["ok"])

        # HF 长尾回退分支：未命中词池但 tracker live 命中 → ok:True + trend:[]
        fake_hf = {"ok": True,
                   "term": {"term": "some-hf-model", "full_id": "org/some-hf-model",
                            "likes": 1, "trending_score": 2, "downloads": 3,
                            "official_url": "https://huggingface.co/org/some-hf-model",
                            "author": "org", "tags": []},
                   "hf_detail": {}}
        with patch.object(self.app.tracker, "get_term_detail",
                          return_value=fake_hf):
            data = self.app._word_detail("some-hf-model", lang="zh")
        self.assertTrue(data["ok"])
        self.assertEqual(data["trend"], [])


if __name__ == "__main__":
    unittest.main()
