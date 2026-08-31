"""Tests for the term-detail explanation feature (_EXPLANATIONS + get_term_explanation).

Covers: known terms return a non-empty bilingual explanation, unknown terms
return an empty string without raising, _word_detail projects the explanation
per language, and the /term/<name> page renders the explanation block.
"""

import os
import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from urllib.parse import quote
from unittest.mock import patch


class TermExplanationTests(unittest.TestCase):
    """Isolated temp DB + zero-token environment; exercises real terms/app path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-term-expl-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        cls._create_db(cls.db_path)
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # Stay on the zero-token fallback path (no LLM calls, no keys).
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

        # Importing app normally starts daemon refreshers; disable only those
        # startup side effects.  Provide the import-time fcntl/requests surface
        # when the stdlib is missing them (Windows dev environments).
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

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    @staticmethod
    def _create_db(path):
        """Minimal news_cards schema; terms/term_snapshots are created by init_db."""
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE news_cards (
                url TEXT PRIMARY KEY,
                title TEXT,
                title_zh TEXT,
                title_en TEXT,
                summary_zh TEXT,
                summary_en TEXT,
                dimension TEXT,
                source TEXT,
                region TEXT,
                published TEXT,
                hn_points INTEGER DEFAULT 0,
                reddit_score INTEGER DEFAULT 0,
                reddit_comments INTEGER DEFAULT 0,
                score INTEGER DEFAULT 0,
                trend INTEGER DEFAULT 0,
                hot INTEGER DEFAULT 0,
                first_seen_at TEXT,
                last_refresh_at TEXT,
                active INTEGER DEFAULT 1
            );
            """
        )
        conn.commit()
        conn.close()

    def setUp(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM news_cards")
        conn.execute("DELETE FROM terms")
        conn.execute("DELETE FROM term_snapshots")
        conn.commit()
        conn.close()
        self.app._detail_cache.clear()

    def _insert_term(self, canonical="gpt-5", count=1):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO terms (
                term, display, display_zh, origin, total_mentions
            ) VALUES (?, ?, '', 'news', ?)
            """,
            (canonical, canonical.upper(), count),
        )
        conn.commit()
        conn.close()

    def test_known_terms_have_bilingual_explanations(self):
        zh = self.terms.get_term_explanation("gpt-5", "zh")
        en = self.terms.get_term_explanation("gpt-5", "en")
        self.assertIsInstance(zh, str)
        self.assertIsInstance(en, str)
        self.assertTrue(zh)
        self.assertTrue(en)
        self.assertNotEqual(zh, en)
        # Aliased / non-canonical spellings normalize to the same entry.
        self.assertEqual(self.terms.get_term_explanation("GPT5", "en"), en)
        self.assertEqual(self.terms.get_term_explanation("GPT 5", "zh"), zh)
        # Unknown language falls back to zh.
        self.assertEqual(self.terms.get_term_explanation("gpt-5", "fr"), zh)

    def test_lexicon_is_fully_covered(self):
        # Every _LEXICON canonical key must have a non-empty zh+en explanation.
        missing = [
            canon for canon in self.terms._LEXICON
            if not (self.terms._EXPLANATIONS.get(canon, {}).get("zh")
                    and self.terms._EXPLANATIONS.get(canon, {}).get("en"))
        ]
        self.assertEqual(missing, [], f"missing explanations: {missing}")
        # And at least 40 entries exist overall.
        self.assertGreaterEqual(len(self.terms._EXPLANATIONS), 40)

    def test_unknown_term_returns_empty_string(self):
        self.assertEqual(self.terms.get_term_explanation("zzz-not-a-term", "zh"), "")
        self.assertEqual(self.terms.get_term_explanation("zzz-not-a-term", "en"), "")
        self.assertEqual(self.terms.get_term_explanation("", "zh"), "")
        self.assertEqual(self.terms.get_term_explanation(None, "zh"), "")

    def test_word_detail_projects_explanation_per_language(self):
        self._insert_term("gpt-5")
        self._insert_term("zzz-custom-term", count=2)

        zh = self.app._word_detail("gpt-5", lang="zh")
        en = self.app._word_detail("gpt-5", lang="en")
        self.assertTrue(zh["ok"])
        self.assertTrue(en["ok"])
        self.assertTrue(zh["term"]["explain"])
        self.assertTrue(en["term"]["explain"])
        self.assertNotEqual(zh["term"]["explain"], en["term"]["explain"])

        # A term in the DB without an explanation entry gets an empty string.
        custom = self.app._word_detail("zzz-custom-term", lang="zh")
        self.assertTrue(custom["ok"])
        self.assertEqual(custom["term"]["explain"], "")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")

    def test_term_page_renders_explanation_block(self):
        self._insert_term("gpt-5")
        client = self.app.app.test_client()

        zh_resp = client.get("/term/gpt-5?lang=zh")
        self.assertEqual(zh_resp.status_code, 200)
        zh_html = zh_resp.get_data(as_text=True)
        self.assertIn('class="term-explain"', zh_html)
        self.assertIn("OpenAI 于 2025 年发布的旗舰多模态大模型", zh_html)

        en_resp = client.get("/term/gpt-5?lang=en")
        self.assertEqual(en_resp.status_code, 200)
        en_html = en_resp.get_data(as_text=True)
        self.assertIn('class="term-explain"', en_html)
        # Apostrophes are HTML-escaped by Jinja2 (OpenAI&#39;s); assert a plain
        # substring that contains no special characters.
        self.assertIn("flagship multimodal model released in 2025", en_html)

    def test_term_page_without_explanation_stays_clean(self):
        self._insert_term("zzz-custom-term")
        client = self.app.app.test_client()
        resp = client.get("/term/zzz-custom-term?lang=zh")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # The CSS rule for .term-explain is always present; the rendered
        # explanation block itself must be absent.
        self.assertNotIn('class="term-explain"', html)


if __name__ == "__main__":
    unittest.main()
