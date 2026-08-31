"""回归：英文页热词显示语言一致性（display_en 不被 LLM 限流轮次清空）。

场景：中文热词某轮刷新 LLM 翻译成功 → display_en 落库（英文页/词页显示英文）；
下一轮 GLM 限流（429/1305）→ term_translator 返回空映射。修复前 ON CONFLICT
会把 display_en 覆盖成 ''，英文页与词页热词集体回退中文（churn）；修复后保留
旧翻译，直到某轮翻译成功才更新。
"""

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


class DisplayEnPreserveTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises real terms path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-disp-en-")
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

    def _card(self, url, title, keywords):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": "2026-08-31", "score": 100,
                "keywords": keywords}

    def _display_en(self, canon):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT display_en FROM terms WHERE term=?",
                         (canon,)).fetchone()
        conn.close()
        return r[0] if r else None

    def test_translation_failure_keeps_old_display_en(self):
        # 第一轮：翻译成功 → display_en 落库
        self.news_store.upsert_cards([self._card(
            "https://t.example/1", "债务融资新规出台", ["债务融资"])])
        self.terms.refresh_words(
            [self._card("https://t.example/1", "债务融资新规出台", ["债务融资"])],
            [], fetched_at=1750000000,
            term_translator=lambda terms: {t: "Debt Financing" for t in terms})
        self.assertEqual(self._display_en("债务融资"), "Debt Financing")

        # 第二轮：LLM 限流 → term_translator 返回空映射 → display_en 必须保留
        self.terms.refresh_words(
            [self._card("https://t.example/1", "债务融资新规出台", ["债务融资"])],
            [], fetched_at=1750000100,
            term_translator=lambda terms: {})
        self.assertEqual(self._display_en("债务融资"), "Debt Financing")

    def test_translation_success_updates_display_en(self):
        self.news_store.upsert_cards([self._card(
            "https://t.example/2", "国产算力再突破", ["国产算力"])])
        self.terms.refresh_words(
            [self._card("https://t.example/2", "国产算力再突破", ["国产算力"])],
            [], fetched_at=1750000000,
            term_translator=lambda terms: {t: "domestic computing power"
                                           for t in terms})
        self.assertEqual(self._display_en("国产算力"),
                         "domestic computing power")

        # 后续翻译给出更准确英文 → 允许更新
        self.terms.refresh_words(
            [self._card("https://t.example/2", "国产算力再突破", ["国产算力"])],
            [], fetched_at=1750000100,
            term_translator=lambda terms: {t: "Domestic AI Compute"
                                           for t in terms})
        self.assertEqual(self._display_en("国产算力"), "Domestic AI Compute")

    def test_no_translator_keeps_old_display_en(self):
        self.news_store.upsert_cards([self._card(
            "https://t.example/3", "灵犀智涌发布新方案", ["灵犀智涌"])])
        self.terms.refresh_words(
            [self._card("https://t.example/3", "灵犀智涌发布新方案", ["灵犀智涌"])],
            [], fetched_at=1750000000,
            term_translator=lambda terms: {t: "Lingxi Zhiyong" for t in terms})
        self.assertEqual(self._display_en("灵犀智涌"), "Lingxi Zhiyong")

        # 无回调（等价无 LLM key 环境）：旧值同样保留
        self.terms.refresh_words(
            [self._card("https://t.example/3", "灵犀智涌发布新方案", ["灵犀智涌"])],
            [], fetched_at=1750000100)
        self.assertEqual(self._display_en("灵犀智涌"), "Lingxi Zhiyong")


if __name__ == "__main__":
    unittest.main()
