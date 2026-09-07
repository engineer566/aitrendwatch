"""P0（2026-09-07）news 视图内容池缓存回归测试。

背景：/api/stream?view=news 每次请求都现装配 dims.json 当轮 + news.db 历史库
400+ 卡（线上实测 7-21s）。修复：后台刷新（_dims_refresh_once）后由拿到锁的
worker 把 neutral 内容池预装配写盘 cache/news.json；get_news_cards 改读该池
文件（mtime 感知），不再每次请求扫库。

本文件验证：
① _write_news_pool_file 产出的池文件与旧现装配语义一致（url 去重 + 标题级
   去重 + from_history 标记 + neutral 双语言 slot）；
② 池文件存在时 get_news_cards 走文件路径——list_history_cards 不被调用
   （零 DB 读），并按 lang 投影 title/summary；
③ 池文件缺失时回退旧现装配路径（DB 直读，与历史行为一致）——由
   test_task1_dup_reports 既有用例覆盖，这里补一条显式断言。
"""

import datetime
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class NewsPoolCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._data_dir = str(Path(cls._tmp.name) / "data")
        cls._cache_dir = str(Path(cls._tmp.name) / "cache")
        os.makedirs(cls._data_dir, exist_ok=True)
        os.makedirs(cls._cache_dir, exist_ok=True)
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR")}
        os.environ["DATA_DIR"] = cls._data_dir
        os.environ["NEWS_DB_PATH"] = os.path.join(cls._data_dir, "news.db")
        os.environ["CACHE_DIR"] = cls._cache_dir
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("GLM_API_KEY", None)
        import config
        import news_store
        import dims
        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(dims)
        cls.dims = dims
        cls.news_store = news_store
        cls.pool_file = dims.NEWS_STREAM_CACHE_FILE

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def setUp(self):
        # 每个用例从干净状态开始：清空历史库 + 移除内容池文件 + 复位内存池缓存
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        conn.execute("DELETE FROM news_cards")
        conn.commit()
        conn.close()
        for f in (self.pool_file, self.pool_file + ".tmp"):
            try:
                os.remove(f)
            except OSError:
                pass
        self.dims._news_pool = None
        self.dims._news_pool_loaded = False
        self.dims._news_pool_mtime = 0

    # ---------- 造数工具 ----------
    @staticmethod
    def _insert(url, title_zh, score=100, published=None):
        """往临时 news.db 插一行（语义同 test_task1_dup_reports._insert）。"""
        if published is None:
            published = datetime.date.today().isoformat()
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        conn.execute(
            """INSERT INTO news_cards
               (url, title, title_zh, title_en, summary_zh, summary_en,
                dimension, source, region, published, score, keywords, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (url, title_zh, title_zh, title_zh, "", "",
             "产品与应用", "源", "国际", published, score, "[]"))
        conn.commit()
        conn.close()

    @staticmethod
    def _cur_card(title_zh, url, score=500):
        """构造 dims.json 当轮卡（双语言 slot + 排序字段）。"""
        return {
            "title": title_zh, "title_zh": title_zh,
            "title_en": "EN " + title_zh,
            "summary_zh": "摘要" + title_zh, "summary_en": "Sum " + title_zh,
            "dimension": "产品与应用", "official_url": url,
            "source": "官方", "region": "国际",
            "published": datetime.date.today().isoformat(),
            "hn_points": 0, "reddit_score": 0, "reddit_comments": 0,
            "score": score, "trend": score // 5, "hot": score, "keywords": [],
        }

    def _install_dims_cache(self, current_cards):
        """把当轮卡写进 dims 内存 + dims.json（_build_news_pool_neutral 的输入）。"""
        data = {
            "ok": True, "fetched_at": 202609070101,
            "dimension_list": self.dims.DIMENSIONS,
            "dimensions": {"产品与应用": current_cards}, "count": len(current_cards),
        }
        self.dims._file_cache_set(data, data["fetched_at"])

    # ---------- 用例 ----------
    def test_write_pool_then_read_without_db(self):
        """写池后 get_news_cards 只读文件缓存：list_history_cards 不被调用，
        结果与旧现装配语义一致（历史卡合并 + 双语言 slot 保留 + 投影）。"""
        # 当轮卡 1 张 + 历史库 2 张（其中一张是当轮同标题镜像 → 标题级去重掉）
        cur = self._cur_card("Alpha 发布新模型", "https://a.example/1", score=500)
        self._install_dims_cache([cur])
        self._insert("https://b.example/2", "Alpha 发布新模型", score=400)
        self._insert("https://c.example/3", "Beta 开源 Agent 框架", score=300)

        # 刷新路径：写池（此时允许一次 DB 读，模拟 _dims_refresh_once 内调用）
        self.dims._write_news_pool_file()
        self.assertTrue(os.path.exists(self.pool_file), "池文件应已写盘")
        with open(self.pool_file, "r", encoding="utf-8") as f:
            blob = json.load(f)
        self.assertEqual(blob["fetched_at"], 202609070101)

        # 请求路径：复位内存池，强制从文件加载；DB 读必须被禁止
        self.dims._news_pool = None
        self.dims._news_pool_loaded = False
        with patch.object(self.news_store, "list_history_cards",
                          side_effect=AssertionError("请求路径不应读 news.db")), \
                patch.object(self.dims, "_file_cache_get",
                             side_effect=AssertionError("请求路径不应现装配")):
            cards_zh, fetched_at = self.dims.get_news_cards("zh")
            cards_en, _ = self.dims.get_news_cards("en")

        self.assertEqual(fetched_at, 202609070101)
        # Alpha 同标题镜像收敛为当轮 1 张 + Beta 历史卡 = 2 张
        self.assertEqual(len(cards_zh), 2)
        ids = {c["id"] for c in cards_zh}
        self.assertIn("https://a.example/1", ids)
        self.assertNotIn("https://b.example/2", ids)
        self.assertIn("https://c.example/3", ids)
        by_id = {c["id"]: c for c in cards_zh}
        # 当轮卡不标记 from_history；历史卡标记（供 API 排序按 published 固定序）
        self.assertNotIn("from_history", by_id["https://a.example/1"])
        self.assertTrue(by_id["https://c.example/3"].get("from_history"))
        # 双语言 slot 保留 + 按 lang 投影 title
        self.assertEqual(by_id["https://a.example/1"]["title"], "Alpha 发布新模型")
        self.assertEqual(by_id["https://a.example/1"]["title_zh"], "Alpha 发布新模型")
        self.assertEqual(by_id["https://a.example/1"]["title_en"], "EN Alpha 发布新模型")
        en_by_id = {c["id"]: c for c in cards_en}
        self.assertEqual(en_by_id["https://a.example/1"]["title"], "EN Alpha 发布新模型")
        self.assertEqual(en_by_id["https://a.example/1"]["summary"], "Sum Alpha 发布新模型")

    def test_pool_refresh_after_second_write(self):
        """同进程二次写池后，请求读到的是新池（mtime 感知收敛）。"""
        self._install_dims_cache([self._cur_card("第一轮", "https://x.example/1")])
        self.dims._write_news_pool_file()
        self._install_dims_cache([self._cur_card("第二轮", "https://x.example/2")])
        self.dims._write_news_pool_file()

        self.dims._news_pool = None
        self.dims._news_pool_loaded = False
        cards, _ = self.dims.get_news_cards("zh")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title_zh"], "第二轮")

    def test_fallback_builds_from_db_when_pool_missing(self):
        """池文件缺失（首刷前冷启动）→ get_news_cards 回退旧现装配路径（DB 直读）。"""
        self._insert("https://f.example/1", "回退路径报道", score=200)
        with patch.object(self.dims, "_file_cache_get",
                          return_value=(None, 0)):
            cards, _ = self.dims.get_news_cards("zh")
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].get("from_history"))
        self.assertEqual(cards[0]["title_zh"], "回退路径报道")


if __name__ == "__main__":
    unittest.main()
