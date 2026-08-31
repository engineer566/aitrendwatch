"""Regression tests for the duplicate related-news fix.

Bug: the same article syndicated under two URLs/sources (identical title, e.g.
Yahoo Finance vs The Motley Fool mirror) appeared twice, consecutively, in the
word-detail related-news list and in word-card ``top_news``.

Fix: ``terms.get_term_news`` and ``_refresh_words_inner`` top-news aggregation
deduplicate by normalized title (strip + casefold + whitespace collapse) before
limit truncation, keeping the first occurrence (highest score).  Mentions /
hot_score counting stays URL-based and is untouched.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from urllib.parse import quote
from unittest.mock import patch


class DupRelatedNewsTests(unittest.TestCase):
    """Use a legacy database and exercise the real terms/app lookup path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-dup-news-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        cls._create_legacy_db(cls.db_path)
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # The tests must stay on the zero-token fallback path.
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        # Other suites import these modules with their own temporary database
        # before this class is initialized.  Reload them after switching the
        # environment so this class always exercises its isolated legacy DB.
        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)

        cls.news_store = news_store
        cls.terms = terms

        # Importing app normally starts daemon refreshers.  Disable only
        # those startup side effects; _word_detail itself remains real.
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
                     keywords="[]", score=100, source="", published="2026-08-29"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO news_cards (
                url, title, title_zh, title_en, published, score, keywords, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, title, title_zh or title, title_en or title, published, score,
             keywords, source),
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

    def test_same_title_different_url_dedupes_keeping_highest_score(self):
        # The reported bug: one article mirrored by two sources, identical
        # title, different URL/source — both keyword-matched to the same term.
        self._insert_card(
            "https://finance.example/1",
            "Mark Zuckerberg's Meta Just Open-Sourced Its Newest AI Model",
            keywords='["gpt-5"]', score=100, source="Yahoo Finance",
        )
        self._insert_card(
            "https://fool.example/1",
            "Mark Zuckerberg's Meta Just Open-Sourced Its Newest AI Model",
            keywords='["gpt-5"]', score=50, source="The Motley Fool",
        )
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["official_url"], "https://finance.example/1")
        self.assertEqual(cards[0]["score"], 100)

    def test_case_and_whitespace_variant_title_dedupes(self):
        # Normalization covers case and runs of whitespace.
        self._insert_card(
            "https://a.example/1", "Meta Just Open-Sourced Its Newest AI Model",
            keywords='["gpt-5"]', score=200,
        )
        self._insert_card(
            "https://b.example/1", "  meta   just open-sourced its newest ai model  ",
            keywords='["gpt-5"]', score=90,
        )
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["official_url"], "https://a.example/1")

    def test_title_zh_shared_dedupes_even_when_raw_titles_differ(self):
        # LLM path: the two mirrors share the translated title_zh while the
        # original titles differ slightly.  Dedup keys on title_zh first.
        self._insert_card(
            "https://a.example/2", "Meta Open-Sources Newest AI Model",
            title_zh="Meta 开源最新 AI 模型", keywords='["gpt-5"]', score=100,
        )
        self._insert_card(
            "https://b.example/2", "Meta Open-Sources Its Newest AI Model Today",
            title_zh="Meta 开源最新 AI 模型", keywords='["gpt-5"]', score=60,
        )
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["official_url"], "https://a.example/2")

    def test_different_titles_are_all_kept(self):
        # Distinct articles must not be collapsed.
        self._insert_card(
            "https://c.example/1", "OpenAI launches GPT-5",
            keywords='["gpt-5"]', score=100,
        )
        self._insert_card(
            "https://c.example/2", "OpenAI releases GPT-5 API",
            keywords='["gpt-5"]', score=80,
        )
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 2)
        self.assertEqual(
            {c["official_url"] for c in cards},
            {"https://c.example/1", "https://c.example/2"},
        )

    def test_limit_counts_unique_titles_after_dedup(self):
        # Dedup happens before limit truncation: the mirrored second copy must
        # not occupy a limit slot and crowd out a valid distinct card.
        self._insert_card(
            "https://d.example/dup-high", "Meta Open-Sources New Model",
            keywords='["gpt-5"]', score=100, published="2026-08-30",
        )
        self._insert_card(
            "https://d.example/dup-low", "Meta Open-Sources New Model",
            keywords='["gpt-5"]', score=90, published="2026-08-29",
        )
        self._insert_card(
            "https://d.example/other", "GPT-5 benchmark results released",
            keywords='["gpt-5"]', score=80, published="2026-08-28",
        )
        cards = self.terms.get_term_news("GPT-5", limit=2)
        self.assertEqual(len(cards), 2)
        self.assertEqual(
            [c["official_url"] for c in cards],
            ["https://d.example/dup-high", "https://d.example/other"],
        )
        titles = [c["title_zh"] for c in cards]
        self.assertEqual(len(set(titles)), len(titles))

    def test_word_card_top_news_has_no_duplicate_titles(self):
        self._insert_card(
            "https://e.example/1", "Meta Open-Sources New Model",
            keywords='["gpt-5"]', score=100,
        )
        self._insert_card(
            "https://e.example/2", "Meta Open-Sources New Model",
            keywords='["gpt-5"]', score=50,
        )
        self._insert_card(
            "https://e.example/3", "GPT-5 benchmark results released",
            keywords='["gpt-5"]', score=80,
        )
        self.terms.refresh_words([], [], fetched_at=123)
        cards, _ = self.terms.get_word_cards(sort="hot", lang="zh", limit=60)
        gpt = next(card for card in cards if card["id"] == "gpt-5")
        top = gpt["top_news"]
        self.assertEqual(len(top), 2)
        titles = [n["title"] for n in top]
        self.assertEqual(len(set(titles)), len(titles))
        self.assertEqual(top[0]["official_url"], "https://e.example/1")

    def test_word_detail_news_has_no_duplicate_titles(self):
        self._insert_card(
            "https://f.example/1", "Mark Zuckerberg's Meta Just Open-Sourced "
            "Its Newest AI Model", keywords='["gpt-5"]', score=100,
            source="Yahoo Finance",
        )
        self._insert_card(
            "https://f.example/2", "Mark Zuckerberg's Meta Just Open-Sourced "
            "Its Newest AI Model", keywords='["gpt-5"]', score=50,
            source="The Motley Fool",
        )
        self._insert_term()
        detail = self.app._word_detail("GPT-5", lang="zh")
        self.assertTrue(detail["ok"])
        news = detail["news"]
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["official_url"], "https://f.example/1")
        self.assertEqual(
            [c["title"] for c in news],
            ["Mark Zuckerberg's Meta Just Open-Sourced Its Newest AI Model"],
        )


if __name__ == "__main__":
    unittest.main()
