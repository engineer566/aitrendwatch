"""Offline regression tests for the bounded unified stream."""

import json
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import stream_utils
import terms


class WordStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cache_file = terms.WORDS_CACHE_FILE
        self.old_cache = terms._file_cache
        self.old_loaded = terms._file_cache_loaded
        self.old_mtime = terms._file_cache_mtime
        terms.WORDS_CACHE_FILE = str(Path(self.temp_dir.name) / "words.json")
        terms._file_cache = {}
        terms._file_cache_loaded = False
        terms._file_cache_mtime = 0

    def tearDown(self):
        terms.WORDS_CACHE_FILE = self.old_cache_file
        terms._file_cache = self.old_cache
        terms._file_cache_loaded = self.old_loaded
        terms._file_cache_mtime = self.old_mtime
        self.temp_dir.cleanup()

    @staticmethod
    def make_cards():
        cards = []
        for i in range(75):
            cards.append({
                "kind": "word",
                "id": f"term-{i:03d}",
                "term": f"Term {i}",
                "hot": i,
                "rise": (i % 9) / 10,
                "novelty": (i % 7) / 10,
                "display_zh": "",
                "top_news": [],
            })
        # This card is outside the cache's hot ordering but must still win rise.
        cards[-1]["rise"] = 99
        cards[-1]["hot"] = 1
        # A duplicate must not consume one of the 60 returned slots.
        cards.append(dict(cards[10]))
        return cards

    def install(self, cards):
        with open(terms.WORDS_CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"words": {"data": {"ok": True, "terms": cards,
                                             "count": len(cards)},
                                  "fetched_at": 1}}, handle)
        terms._file_cache_loaded = False
        terms._file_cache = {}

    def test_sort_then_limit_is_stable_and_keeps_top_rise(self):
        cards = self.make_cards()
        self.install(cards)

        first, _ = terms.get_word_cards(sort="rise", lang="zh", limit=60)
        second, _ = terms.get_word_cards(sort="rise", lang="en", limit=60)

        self.assertEqual(len(first), 60)
        self.assertEqual(len({card["id"] for card in first}), 60)
        self.assertEqual([card["id"] for card in first],
                         [card["id"] for card in second])
        self.assertEqual(first[0]["id"], "term-074")
        unique = {card["id"]: card for card in cards}
        expected = sorted(unique.values(),
                          key=lambda card: (-card["rise"], -card["hot"], card["id"]))[:60]
        self.assertEqual([card["id"] for card in first],
                         [card["id"] for card in expected])

        for sort, field in (("hot", "hot"), ("new", "novelty")):
            result, _ = terms.get_word_cards(sort=sort, limit=60)
            expected = sorted(unique.values(),
                              key=lambda card: (-card[field], -card["hot"], card["id"]))[:60]
            self.assertEqual([card["id"] for card in result],
                             [card["id"] for card in expected])

    def test_equal_scores_use_deterministic_identity_order(self):
        cards = self.make_cards()[:75]
        for card in cards:
            card["hot"] = 10
            card["rise"] = 1
        self.install(list(reversed(cards)))

        result, _ = terms.get_word_cards(sort="rise", limit=60)

        self.assertEqual([card["id"] for card in result],
                         [f"term-{i:03d}" for i in range(60)])


class StreamDimensionTests(unittest.TestCase):
    def test_cross_dimension_counts_match_unique_renderable_cards(self):
        cards = [
            {"kind": "word", "id": "a", "dimension": "模型与技术",
             "dims": ["模型与技术", "研究与论文", "研究与论文"]},
            {"kind": "word", "id": "b", "dimension": "研究与论文",
             "dims": ["研究与论文"]},
            {"kind": "word", "id": "a", "dimension": "模型与技术",
             "dims": ["模型与技术"]},
        ]
        unique = stream_utils.dedupe_cards(cards)

        self.assertEqual(len(unique), 2)
        self.assertEqual(
            stream_utils.dimension_counts(unique, "words"),
            {"模型与技术": 1, "研究与论文": 2},
        )
        self.assertEqual(
            stream_utils.dimension_list(unique, "words", ["模型与技术"]),
            ["模型与技术", "研究与论文"],
        )


class StreamApiTests(unittest.TestCase):
    """Exercise /api/stream with all upstream/network work patched out."""

    @classmethod
    def setUpClass(cls):
        if "fcntl" not in sys.modules:
            fake_fcntl = types.ModuleType("fcntl")
            fake_fcntl.LOCK_EX = 0
            fake_fcntl.LOCK_NB = 0
            fake_fcntl.LOCK_UN = 0
            fake_fcntl.flock = lambda *args: None
            sys.modules["fcntl"] = fake_fcntl
        import dims
        import tracker

        # app.py starts daemon refreshers at import time.  Keep this test fully
        # offline and do not provide either LLM provider key.
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            cls.app_module = importlib.import_module("app")
        cls.client = cls.app_module.app.test_client()

    def test_words_response_count_and_dimensions_match_terms(self):
        cards = []
        for i in range(75):
            cards.append({
                "kind": "word", "id": f"term-{i:03d}",
                "dimension": "模型与技术",
                "dims": ["模型与技术"], "hot": 100 - i,
            })
        cards[0]["dims"] = ["模型与技术", "研究与论文", "研究与论文"]
        cards[1]["dimension"] = "研究与论文"
        cards[1]["dims"] = ["研究与论文"]
        cards.append(dict(cards[2]))

        with patch.object(self.app_module.terms_mod, "get_word_cards",
                          return_value=(cards, 123)) as get_cards:
            response = self.client.get("/api/stream?view=words&sort=rise&lang=zh")

        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_cards.call_args.kwargs, {"limit": 60})
        self.assertEqual(body["count"], len(body["terms"]))
        self.assertEqual(len({card["id"] for card in body["terms"]}), body["count"])
        self.assertEqual(body["count"], 60)
        self.assertEqual(body["dimension_counts"]["模型与技术"], 59)
        self.assertEqual(body["dimension_counts"]["研究与论文"], 2)
        self.assertIn("研究与论文", body["dimension_list"])


if __name__ == "__main__":
    unittest.main()
