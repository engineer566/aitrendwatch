"""Openclaw 热词逻辑优化回归测试（需求 2）。

覆盖四项优化：
1. openclaw 进入 _LEXICON → 无 LLM key 降级抽词（extract_keywords_dict）能命中；
2. openclaw 有静态解释（_EXPLANATIONS）→ 详情页三级取词直接命中；
3. news_store.upsert_cards 关键词 churn 防护：降级子集不覆盖 LLM 抽取的丰富集合；
4. 热窗 hot 按报道新鲜度加权 → 今日热词（近 1-3 天高分报道）排名高于存量累计词。
"""

import datetime
import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest


def _fcntl_stub():
    if "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8
        stub.flock = lambda *args: None
        sys.modules["fcntl"] = stub


class OpenclawHotwordTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises real terms/news_store path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-openclaw-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
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
        terms.init_db()
        news_store.init_db()
        cls.news_store = news_store
        cls.terms = terms

        _fcntl_stub()

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

    def _card(self, url, title, keywords, published, score=100):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": published, "score": score,
                "keywords": keywords}

    # ---- 1. 词典命中（无 key 降级抽词）----

    def test_openclaw_in_lexicon_degradation_extraction(self):
        kws = self.terms.extract_keywords_dict(
            "OpenClaw Releases OpenClaw 2.0: Guided Model Setup")
        self.assertIn("openclaw", kws)

    def test_openclaw_has_static_explanation(self):
        zh = self.terms.get_term_explanation("openclaw", "zh")
        en = self.terms.get_term_explanation("openclaw", "en")
        self.assertTrue(zh and "开源" in zh)
        self.assertTrue(en and "open-source" in en)

    # ---- 2. keywords churn 防护 ----

    def test_upsert_keeps_richer_keywords_on_degradation_subset(self):
        # 第一轮：LLM 抽取的丰富关键词（含词典外词 openclaw）
        self.news_store.upsert_cards([self._card(
            "https://oc.example/1", "OpenClaw 新版本发布", ["openclaw", "agent"],
            "2026-08-31", 100)])
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT keywords FROM news_cards WHERE url=?",
                         ("https://oc.example/1",)).fetchone()
        conn.close()
        self.assertEqual(r[0], '["openclaw", "agent"]')

        # 第二轮：GLM 限流 → 降级词典匹配只出子集 ["agent"] → 必须保留旧集合
        self.news_store.upsert_cards([self._card(
            "https://oc.example/1", "OpenClaw 新版本发布", ["agent"],
            "2026-08-31", 120)])
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT keywords FROM news_cards WHERE url=?",
                         ("https://oc.example/1",)).fetchone()
        conn.close()
        self.assertEqual(r[0], '["openclaw", "agent"]')

    def test_upsert_overwrites_when_new_keywords_add_words(self):
        self.news_store.upsert_cards([self._card(
            "https://oc.example/2", "OpenClaw 新版本发布", ["openclaw"],
            "2026-08-31", 100)])
        # 新集合含旧集合没有的词 → 正常覆盖
        self.news_store.upsert_cards([self._card(
            "https://oc.example/2", "OpenClaw 新版本发布",
            ["openclaw", "agent", "网关"], "2026-08-31", 120)])
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT keywords FROM news_cards WHERE url=?",
                         ("https://oc.example/2",)).fetchone()
        conn.close()
        self.assertEqual(r[0], '["openclaw", "agent", "网关"]')

    # ---- 3. 热窗 hot 新鲜度加权 ----

    def test_hot_weights_recent_articles(self):
        today = datetime.date.today().isoformat()
        old = (datetime.date.today()
               - datetime.timedelta(days=5)).isoformat()
        # 词 A：今日 1 篇高分（score 100）→ 加权后 hot=300
        # 词 B：5 天前 2 篇（score 60+60）→ 加权后 hot=120（无加权时 120 > 100 排前）
        self.news_store.upsert_cards([
            self._card("https://h.example/a1", "今日热词 A 报道", ["word-a"],
                       today, 100),
            self._card("https://h.example/b1", "旧热词 B 报道一", ["word-b"],
                       old, 60),
            self._card("https://h.example/b2", "旧热词 B 报道二", ["word-b"],
                       old, 60),
        ])
        self.terms.refresh_words(
            [self._card("https://h.example/a1", "今日热词 A 报道", ["word-a"],
                        today, 100),
             self._card("https://h.example/b1", "旧热词 B 报道一", ["word-b"],
                        old, 60),
             self._card("https://h.example/b2", "旧热词 B 报道二", ["word-b"],
                        old, 60)],
            [], fetched_at=1750000000)
        conn = sqlite3.connect(self.db_path)
        hot_a = conn.execute("SELECT cur_hot FROM terms WHERE term='word-a'"
                             ).fetchone()[0]
        hot_b = conn.execute("SELECT cur_hot FROM terms WHERE term='word-b'"
                             ).fetchone()[0]
        conn.close()
        self.assertGreater(hot_a, hot_b,
                           "今日词（×3 加权）应高于 5 天前的累计词")
        # 展示排序（words.json）同样 A 在前
        cards, _ = self.terms.get_word_cards(sort="hot", lang="zh", limit=10)
        ids = [c["id"] for c in cards]
        self.assertEqual(ids[0], "word-a")
        self.assertEqual(ids[1], "word-b")

    def test_hot_weight_ignores_future_dates(self):
        # 未来日期（时区/脏数据）不产生负权重异常
        future = (datetime.date.today()
                  + datetime.timedelta(days=2)).isoformat()
        w = self.terms._hot_recency_weight(future, datetime.date.today())
        self.assertEqual(w, 3.0)


if __name__ == "__main__":
    unittest.main()
