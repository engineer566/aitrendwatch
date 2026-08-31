"""Tests for the generic hot-word stoplist (_TERM_STOPWORDS).

- dictionary extraction never returns stopwords (e.g. "AI" / "LLM");
- real terms (gpt-5, openai) still survive extraction;
- LLM-derived canonical keywords stored in news_cards.keywords are dropped at
  aggregation time, so they never enter the words pool / terms table;
- HF model cards whose canonical key is a stopword (e.g. "model") never enter
  the pool, while real model names (qwen3) are kept;
- is_stopword() normalizes any input spelling before checking.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


class StopwordTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises the real terms path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-stop-")
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
        cls.news_store = news_store
        cls.terms = terms

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

    def _card(self, url, title, keywords):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": "2026-08-31", "score": 100,
                "keywords": keywords}

    def _term_exists(self, canon):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT 1 FROM terms WHERE term=?", (canon,)).fetchone()
        conn.close()
        return r is not None

    def test_is_stopword_normalizes_any_spelling(self):
        t = self.terms
        for generic in ("ai", "AI", "llm", "LLM", "model", "Model",
                        "artificial intelligence", "machine-learning",
                        "deep learning", "technology", "tech"):
            self.assertTrue(t.is_stopword(generic), generic)
        for real in ("gpt-5", "openai", "qwen3", "claude", "rag", ""):
            self.assertFalse(t.is_stopword(real), real)

    def test_extract_keywords_dict_skips_stopwords(self):
        t = self.terms
        # "ai" is not a lexicon entry, but the stoplist guarantees it never
        # surfaces even if it later becomes one.
        self.assertNotIn("ai", t.extract_keywords_dict("AI breakthrough today"))
        # "llm" IS a lexicon entry: this exercises the stoplist filter itself.
        self.assertNotIn("llm", t.extract_keywords_dict("LLM breakthrough today"))
        # real terms survive; "ai" is never returned for an OpenAI headline
        kws = t.extract_keywords_dict("OpenAI releases GPT-5")
        self.assertIn("gpt-5", kws)
        self.assertIn("openai", kws)
        self.assertNotIn("ai", kws)

    def test_refresh_words_skips_stopword_keywords(self):
        t = self.terms
        self.news_store.upsert_cards([
            self._card("https://s.example/1", "AI breakthrough today", ["ai"]),
            self._card("https://s.example/2", "Big LLM release", ["llm"]),
            self._card("https://s.example/3", "OpenAI releases GPT-5",
                       ["gpt-5"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertFalse(self._term_exists("ai"))
        self.assertFalse(self._term_exists("llm"))
        self.assertTrue(self._term_exists("gpt-5"))

    def test_llm_derived_stopword_keywords_filtered(self):
        # LLM 抽词写入 news_cards.keywords 的通用词（如 "AI"）在聚合时被剔除，
        # 即使它已随卡持久化；同卡的 gpt-5 正常入池。
        t = self.terms
        self.news_store.upsert_cards([
            self._card("https://s.example/4", "OpenAI releases GPT-5",
                       ["AI", "gpt-5"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertFalse(self._term_exists("ai"))
        self.assertTrue(self._term_exists("gpt-5"))

    def test_hf_stopword_canon_skipped(self):
        t = self.terms
        model_cards = [
            {"full_id": "org/Model", "term": "Model",
             "trending_score": 100, "likes": 50},   # canon "model" → 停用
            {"full_id": "org/Qwen3", "term": "Qwen3",
             "trending_score": 200, "likes": 100},  # 真实模型名 → 保留
        ]
        t.refresh_words([], model_cards, fetched_at=1750000000)
        self.assertFalse(self._term_exists("model"))
        self.assertTrue(self._term_exists("qwen3"))


if __name__ == "__main__":
    unittest.main()
