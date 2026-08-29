"""Regression tests for word-detail news lookup against old/new news.db rows."""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from urllib.parse import quote
from unittest.mock import patch


class TermNewsLookupTests(unittest.TestCase):
    """Use a legacy database and exercise the real terms/app lookup path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-term-news-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        cls._create_legacy_db(cls.db_path)
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # The tests must stay on the zero-token fallback path.
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import news_store
        import terms

        cls.news_store = news_store
        cls.terms = terms

        # Importing app normally starts daemon refreshers.  Disable only
        # those startup side effects; _word_detail itself remains real.
        # Production runs on Linux; this repository's Windows development
        # environment has no stdlib fcntl, so provide only the import-time
        # surface needed by the unstarted refresher lock helper.
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
            import app
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    @staticmethod
    def _create_legacy_db(path):
        """Create the pre-keywords schema used by production before migration."""
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

    def _insert_card(self, url, title, title_zh=None, title_en=None,
                     keywords="[]", score=100):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO news_cards (
                url, title, title_zh, title_en, published, score, keywords
            ) VALUES (?, ?, ?, ?, '2026-08-29', ?, ?)
            """,
            (url, title, title_zh or title, title_en or title, score, keywords),
        )
        conn.commit()
        conn.close()

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

    def test_legacy_schema_migrates_and_aliases_are_found(self):
        self._insert_card(
            "old-gpt5", "OpenAI launches GPT5",
            "OpenAI 发布 GPT5", "OpenAI launches GPT5",
        )
        self._insert_card(
            "old-gpt-space", "OpenAI studies GPT 5",
            "OpenAI 研究 GPT 5", "OpenAI studies GPT 5",
        )
        self._insert_card(
            "old-agent", "智能体应用获得融资",
            "智能体应用获得融资", "Agent application raises funding",
        )
        self._insert_card(
            "wrong-version", "OpenAI launches GPT-5.5",
            "OpenAI 发布 GPT-5.5", "OpenAI launches GPT-5.5", score=9999,
        )
        self._insert_term(count=3)

        conn = sqlite3.connect(self.db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(news_cards)")}
        migrated_values = conn.execute(
            "SELECT keywords FROM news_cards ORDER BY url"
        ).fetchall()
        conn.close()
        self.assertIn("keywords", columns)
        self.assertTrue(all(row[0] == "[]" for row in migrated_values))

        gpt_urls = {
            card["official_url"]
            for card in self.terms.get_term_news("GPT-5", limit=50)
        }
        self.assertEqual(gpt_urls, {"old-gpt5", "old-gpt-space"})
        self.assertEqual(
            {card["official_url"] for card in self.terms.get_term_news("gpt5")},
            {"old-gpt5", "old-gpt-space"},
        )
        self.assertEqual(
            {card["official_url"] for card in self.terms.get_term_news("智能体")},
            {"old-agent"},
        )
        self.assertEqual(
            {card["official_url"] for card in self.terms.get_term_news("Agent")},
            {"old-agent"},
        )

    def test_unmigrated_schema_is_read_without_keywords_column(self):
        # This covers the safe read path if a production process cannot run
        # ALTER TABLE (for example while an old SQLite file is read-only).
        legacy_path = os.path.join(self._tmp.name, "unmigrated.db")
        self._create_legacy_db(legacy_path)
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "INSERT INTO news_cards(url, title, title_zh, title_en, published) "
            "VALUES ('raw-legacy', 'GPT5 release', 'GPT5 发布', 'GPT5 release', "
            "'2026-08-29')"
        )
        conn.commit()
        conn.close()

        old_path = self.terms.config.NEWS_DB_PATH
        self.terms.config.NEWS_DB_PATH = legacy_path
        try:
            self.assertEqual(
                [card["official_url"]
                 for card in self.terms.get_term_news("gpt-5")],
                ["raw-legacy"],
            )
        finally:
            self.terms.config.NEWS_DB_PATH = old_path

    def test_legacy_keyword_surface_and_new_canonical_keyword_are_read(self):
        # The title does not contain GPT; only the old surface keyword does.
        self._insert_card("old-keyword", "OpenAI update", keywords='["GPT5"]')
        self._insert_term()
        self.assertEqual(
            [card["official_url"] for card in self.terms.get_term_news("gpt-5")],
            ["old-keyword"],
        )

        self.news_store.upsert_cards([{
            "official_url": "new-canonical",
            "title": "OpenAI ships a model",
            "title_zh": "OpenAI 发布模型",
            "title_en": "OpenAI ships a model",
            "published": "2026-08-29",
            "score": 200,
            "keywords": ["gpt-5"],
        }])
        self.assertEqual(
            {card["official_url"]
             for card in self.terms.get_term_news("GPT-5")},
            {"old-keyword", "new-canonical"},
        )

        # A hand-migrated plain keyword value is also a candidate, then gets
        # canonicalized and verified in Python.
        self._insert_card("old-plain-keyword", "OpenAI update", keywords="GPT5")
        self.assertEqual(
            {card["official_url"]
             for card in self.terms.get_term_news("GPT-5")},
            {"old-keyword", "new-canonical", "old-plain-keyword"},
        )

    def test_limit_is_applied_after_version_filter(self):
        self._insert_card(
            "wrong-version", "GPT-5.5 release", score=9999,
        )
        self._insert_card(
            "valid-version", "GPT-5 release", score=1,
        )
        self._insert_term(count=1)
        cards = self.terms.get_term_news("GPT-5", limit=1)
        self.assertEqual([card["official_url"] for card in cards], ["valid-version"])

    def test_word_refresh_rebuilds_legacy_count_without_llm(self):
        self._insert_card(
            "legacy-refresh", "OpenAI launches GPT5",
            "OpenAI 发布 GPT5", "OpenAI launches GPT5",
        )
        self.terms.refresh_words([], [], fetched_at=123)
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en")
        gpt = next(card for card in cards if card["id"] == "gpt-5")
        self.assertEqual(gpt["news_cnt"], 1)
        self.assertEqual(gpt["top_news"][0]["official_url"], "legacy-refresh")

    def test_word_detail_returns_news_in_both_languages(self):
        self._insert_card(
            "bilingual-card", "OpenAI ships GPT5",
            "OpenAI 发布 GPT5", "OpenAI ships GPT5",
        )
        self._insert_term()

        zh = self.app._word_detail("GPT-5", lang="zh")
        en = self.app._word_detail("GPT-5", lang="en")
        self.assertTrue(zh["ok"])
        self.assertTrue(en["ok"])
        self.assertEqual([card["official_url"] for card in zh["news"]],
                         ["bilingual-card"])
        self.assertEqual([card["official_url"] for card in en["news"]],
                         ["bilingual-card"])
        self.assertEqual(zh["news"][0]["title"], "OpenAI 发布 GPT5")
        self.assertEqual(en["news"][0]["title"], "OpenAI ships GPT5")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")

        client = self.app.app.test_client()
        for lang in ("zh", "en"):
            response = client.get("/api/word/GPT-5?lang=" + lang)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [card["official_url"] for card in response.get_json()["news"]],
                ["bilingual-card"],
            )


if __name__ == "__main__":
    unittest.main()
