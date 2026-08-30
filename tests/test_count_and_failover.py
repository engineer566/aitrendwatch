"""Regression tests for the 2026-08-30 fixes:

1. Word-card ``news_cnt`` must equal the detail-page association count
   (keywords + title-surface hits), fixing "卡片右上角数量与 view page 数量不同".
2. LLM account-level rate limit (GLM 1302, same key blocks every tier of the
   provider) must skip the whole provider family instead of burning
   ``LLM_FAILOVER_THRESHOLD`` failures per tier.
"""

import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class CountConsistencyTests(unittest.TestCase):
    """词卡 news_cnt 与 get_term_news 同口径（关键词 + 标题命中），且边界防误配。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-count-")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = os.path.join(cls._tmp.name, "news.db")
        os.environ["CACHE_DIR"] = os.path.join(cls._tmp.name, "cache")
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        terms.init_db()
        cls.terms = terms
        cls.news_store = news_store

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def test_news_cnt_matches_get_term_news(self):
        # abot-recon 不在词典：第二张卡仅靠标题命中（pass 2 标题关联计数），
        # 第三张无关卡（词典可能误抽 chip，但绝不能算进 abot-recon）。
        cards = [
            {"official_url": "https://ab.example/1",
             "title": "ABot-Recon demo released", "title_zh": "ABot-Recon 演示发布",
             "title_en": "ABot-Recon demo released",
             "published": "2026-08-28", "score": 100,
             "keywords": ["abot-recon"]},
            {"official_url": "https://ab.example/2",
             "title": "Inside the ABot-Recon team", "title_zh": "ABot-Recon 团队内幕",
             "title_en": "Inside the ABot-Recon team",
             "published": "2026-08-25", "score": 50,
             "keywords": []},
            {"official_url": "https://ab.example/3",
             "title": "Unrelated news about chips", "title_zh": "无关芯片新闻",
             "title_en": "Unrelated news about chips",
             "published": "2026-08-24", "score": 10,
             "keywords": []},
        ]
        self.news_store.upsert_cards(cards)
        self.terms._refresh_words_inner(cards, [], fetched_at=1750000000)
        words, _ = self.terms.get_word_cards("hot", "zh", limit=60)
        by_term = {w.get("term"): w.get("news_cnt") for w in words}
        # 关键词 1 + 标题命中 1 = 2，与详情页关联口径一致
        self.assertEqual(by_term.get("Abot Recon"), 2)
        self.assertEqual(
            len(self.terms.get_term_news("abot-recon", limit=50)), 2)

    def test_version_boundary_not_counted(self):
        # GPT-5.5 不得计入 gpt-5（版本感知边界）。
        cards = [
            {"official_url": "https://g.example/1",
             "title": "GPT-5 release notes", "title_zh": "GPT-5 发布说明",
             "title_en": "GPT-5 release notes",
             "published": "2026-08-27", "score": 200,
             "keywords": ["gpt-5"]},
            {"official_url": "https://g.example/2",
             "title": "GPT-5.5 arrives", "title_zh": "GPT-5.5 到来",
             "title_en": "GPT-5.5 arrives",
             "published": "2026-08-26", "score": 500,
             "keywords": []},
        ]
        self.news_store.upsert_cards(cards)
        self.terms._refresh_words_inner(cards, [], fetched_at=1750000000)
        words, _ = self.terms.get_word_cards("hot", "zh", limit=60)
        by_term = {w.get("term"): w.get("news_cnt") for w in words}
        self.assertEqual(by_term.get("GPT 5"), 1)
        self.assertEqual(
            len(self.terms.get_term_news("gpt-5", limit=50)), 1)


class LLMFailoverSkipTests(unittest.TestCase):
    """账户级限流（GLM 1302）应整族跳过，而不是逐档烧满失败阈值。"""

    @classmethod
    def setUpClass(cls):
        if "fcntl" not in sys.modules:
            stub = types.ModuleType("fcntl")
            stub.LOCK_EX = 2
            stub.LOCK_NB = 4
            stub.LOCK_UN = 8
            stub.flock = lambda *args: None
            sys.modules["fcntl"] = stub
        os.environ["GLM_API_KEY"] = "fake-glm-key"
        os.environ["DEEPSEEK_API_KEY"] = "fake-ds-key"

        import config
        import dims

        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    def _batch(self):
        return [{"title": "Some title", "source": "S", "lang": "en",
                 "published": "2026-08-28", "official_url": "https://x"}]

    def test_1302_skips_provider_family(self):
        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(
                              {"error": {"code": "1302",
                                         "message": "rate limited"}})):
            with self.assertRaises(dims._LLMAccountRateLimit):
                dims._llm_classify_batch(self._batch())
        # 同 key 下 glm-* 全档受限：应直达 deepseek-v4-flash
        self.assertEqual(
            self.config.LLM_CHAIN[dims._LLM_ACTIVE_IDX], "deepseek-v4-flash")

    def test_1305_stays_transient_and_keeps_tier(self):
        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(
                              {"error": {"code": "1305",
                                         "message": "overloaded"}})):
            with self.assertRaises(dims._LLMTransientError):
                dims._llm_classify_batch(self._batch())
        self.assertEqual(dims._LLM_ACTIVE_IDX, 0)

    def test_skip_provider_from_mid_chain(self):
        dims = self.dims
        dims._LLM_ACTIVE_IDX = 2
        dims._llm_skip_provider()
        self.assertEqual(
            self.config.LLM_CHAIN[dims._LLM_ACTIVE_IDX], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
