"""大小写无关聚类：normalize_term 与词聚合（refresh_words）的大小写无关性。

覆盖：
1. normalize_term 对 GPT-5 / gpt-5 / Gpt-5 / GPT 5 / GPT5 归一到同一 canonical 键，
   且首尾 ASCII 标点噪音（LLM 输出）同样归一；版本感知边界保留（gpt-5 ≠ gpt-5.5）。
2. refresh_words 把混合大小写 keywords 的卡聚合为单条词卡（不分裂）。
3. news_store.upsert_cards 落库前把 keywords 归一成 canonical 键（去重）。
4. 详情页缓存键按 canonical 归一（别名拼写共享同一条目）。
"""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class CaseInsensitiveClusteringTests(unittest.TestCase):
    """用隔离临时库走真实 terms/news_store/app 路径，全程零 LLM（无 key 降级）。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-case-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # 必须留在零 token 降级路径（LLM 调用纪律）。
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

        # 详情页缓存键断言需要 app；禁用其启动副作用（与 test_term_news 同套路）。
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
                requests_stub.utils = types.SimpleNamespace(quote=lambda s: s)
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
        for t in ("news_cards", "terms", "term_snapshots"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.close()
        self.app._detail_cache.clear()

    def _insert_card(self, url, title, keywords):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, "
            "published, score, keywords) VALUES (?,?,?,?,?,?,?)",
            (url, title, title, title, "2026-08-29", 100, keywords))
        conn.commit()
        conn.close()

    def test_normalize_term_is_case_insensitive(self):
        t = self.terms
        # 核心要求：三种大小写归一到同一 canonical 键
        self.assertEqual(t.normalize_term("GPT-5"), "gpt-5")
        self.assertEqual(t.normalize_term("gpt-5"), "gpt-5")
        self.assertEqual(t.normalize_term("Gpt-5"), "gpt-5")
        # 空白/下划线/无连字符别名同键
        self.assertEqual(t.normalize_term("GPT 5"), "gpt-5")
        self.assertEqual(t.normalize_term("GPT5"), "gpt-5")
        self.assertEqual(t.normalize_term("GPT_5"), "gpt-5")
        # 首尾 ASCII 标点噪音（LLM 抽词偶发）同键
        self.assertEqual(t.normalize_term("GPT-5."), "gpt-5")
        self.assertEqual(t.normalize_term("(GPT-5)"), "gpt-5")
        self.assertEqual(t.normalize_term("GPT-5!"), "gpt-5")
        # CJK 词整词保留（去首尾标点不得吃掉汉字）；词典别名仍归一为英文 canonical
        self.assertEqual(t.normalize_term("!测试词!"), "测试词")
        self.assertEqual(t.normalize_term("智能体"), "agent")
        # 版本感知边界保留：GPT-5 ≠ GPT-5.5 ≠ GPT-50
        self.assertNotEqual(t.normalize_term("GPT-5"), t.normalize_term("GPT-5.5"))
        self.assertNotEqual(t.normalize_term("GPT-5"), t.normalize_term("GPT-50"))

    def test_refresh_words_clusters_mixed_case_into_one_entry(self):
        # 直接写库模拟历史/旁路落库的混合大小写 keywords；refresh_words 必须
        # 归并为单个 canonical 词（news_cnt=3），不得分裂出多条 gpt-5 词卡。
        for i, kw in enumerate(['["GPT-5"]', '["gpt-5"]', '["Gpt-5"]']):
            self._insert_card(f"mixed-{i}", f"GPT-5 card {i}", kw)

        self.terms.refresh_words([], [], fetched_at=123)
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en")
        gpt = [c for c in cards if c["id"] == "gpt-5"]
        self.assertEqual(len(gpt), 1)
        self.assertEqual(gpt[0]["news_cnt"], 3)
        # 词池不得出现其它大小写/别名变体词卡
        self.assertEqual([c["id"] for c in cards if "gpt" in c["id"].lower()],
                         ["gpt-5"])
        # 详情页按任意大小写查到同一 canonical 词
        self.assertEqual(self.terms.get_term_row("GPT-5")["term"], "gpt-5")
        self.assertEqual(self.terms.get_term_row("gpt-5")["term"], "gpt-5")

    def test_upsert_cards_stores_canonical_keywords(self):
        # 落库前归一：列表里同一词的 4 种大小写/拼写只存一个 canonical 键。
        self.news_store.upsert_cards([{
            "official_url": "https://mix.example/1",
            "title": "Mixed-case keywords card",
            "title_zh": "混合大小写关键词卡",
            "title_en": "Mixed-case keywords card",
            "published": "2026-08-29",
            "score": 100,
            "keywords": ["GPT-5", "gpt-5", "Gpt-5", "GPT 5"],
        }])
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT keywords FROM news_cards WHERE url=?",
            ("https://mix.example/1",)).fetchone()
        conn.close()
        self.assertEqual(json.loads(row[0]), ["gpt-5"])
        # canonical 落库后 refresh_words 自然只出一个词
        self.terms.refresh_words([], [], fetched_at=123)
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en")
        gpt = [c for c in cards if c["id"] == "gpt-5"]
        self.assertEqual(len(gpt), 1)
        self.assertEqual(gpt[0]["news_cnt"], 1)

    def test_refresh_words_merges_legacy_mixed_case_term_row(self):
        # 老库可能落过 "GPT-5" 混合大小写词行：刷新时 canonical 键应视为已存在，
        # display 演进保留旧展示名，且不产生重复词卡。
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, origin, "
            "total_mentions) VALUES (?,?,?,?,?)",
            ("GPT-5", "GPT-5", "", "news", 5))
        conn.commit()
        conn.close()
        # 该词本轮有报道命中（否则不出榜，无法验证 display 演进）
        self._insert_card("legacy-mixed", "GPT-5 ships", '["gpt-5"]')

        self.terms.refresh_words([], [], fetched_at=123)
        row = self.terms.get_term_row("gpt-5")
        self.assertIsNotNone(row)
        self.assertEqual(row["display"], "GPT-5")  # 旧展示名保留（display 演进）
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en")
        self.assertEqual([c["id"] for c in cards], ["gpt-5"])

    def test_term_detail_cache_key_is_canonical(self):
        # 别名拼写（GPT5）与 canonical（gpt-5）共享同一条详情缓存。
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "total_mentions) VALUES (?,?,?,?,?,?)",
            ("gpt-5", "GPT-5", "", "", "news", 1))
        conn.commit()
        conn.close()

        client = self.app.app.test_client()
        for spelling in ("GPT-5", "gpt-5", "GPT5", "GPT 5"):
            self.assertEqual(client.get(f"/term/{spelling}").status_code, 200)
        term_keys = [k for k in self.app._detail_cache
                     if k.split(":", 1)[0] in ("zh", "en")
                     and k.split(":", 1)[1] in ("gpt-5", "gpt5")]
        self.assertEqual(len(term_keys), 1)
        self.assertTrue(term_keys[0].endswith(":gpt-5"))


if __name__ == "__main__":
    unittest.main()
