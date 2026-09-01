"""需求 5：关键词大小写硬编码校验（case_match_original）测试。

背景：normalize_term 会把提取的关键词小写化成 canonical 键（"OpenClaw"→
"openclaw"、"GPT-5"→"gpt-5"），把原文里的大小写抹平。需求 5 要求提取的
关键词必须与原文大小写完全一致——在原文中大小写不敏感地找到该关键词
（含词典表面形式/空格变体），命中则用原文的确切大小写替换；未命中保持
canonical；纯 CJK 关键词无大小写概念，原样返回。这是确定性代码校验，
不依赖 LLM。

覆盖：
1. case_match_original 命中原文 → 返回原文确切大小写（ASCII / 词典表面 /
   空格变体 / 缩写）；
2. 未命中 → 保持 canonical 形式；
3. 纯 CJK 关键词 → 原样返回；
4. extract_keywords_dict 返回与原文大小写一致的表面形式；
5. _news_row_canons 聚合键仍归一回 canonical（大小写无关归并）；
6. dims._llm_classify_batch 的 LLM 抽词结果同样过大小写校验。
"""

import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _fcntl_stub():
    if "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8
        stub.flock = lambda *args: None
        sys.modules["fcntl"] = stub


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class KeywordCaseMatchTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises the real terms path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-case-")
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
        cls.terms = terms

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    # ---- 1. case_match_original：命中原文 → 返回原文确切大小写 ----

    def test_ascii_hit_returns_exact_original_case(self):
        t = self.terms
        self.assertEqual(t.case_match_original("gpt-5",
                                               "OpenAI releases GPT-5"), "GPT-5")
        self.assertEqual(t.case_match_original("openai",
                                               "OpenAI releases GPT-5"), "OpenAI")

    def test_lexicon_surface_hit_returns_original_case(self):
        t = self.terms
        self.assertEqual(t.case_match_original(
            "openclaw", "OpenClaw Releases OpenClaw 2.0: Guided Model Setup"),
            "OpenClaw")

    def test_space_variant_surface_hit(self):
        # 经 _term_surfaces 的空格变体（"gpt 5"）命中原文的 "GPT 5"
        t = self.terms
        self.assertEqual(t.case_match_original("gpt-5", "GPT 5 is here"), "GPT 5")

    def test_acronym_canonical_unchanged(self):
        t = self.terms
        self.assertEqual(t.case_match_original(
            "GLM", "GLM-5 model achieves new benchmark record"), "GLM")

    # ---- 2. 未命中 → 保持 canonical 形式 ----

    def test_miss_keeps_canonical(self):
        t = self.terms
        self.assertEqual(t.case_match_original("gpt-5", "Some AI news headline"),
                         "gpt-5")

    # ---- 3. 纯 CJK 关键词 → 原样返回（不抛错）----

    def test_cjk_keyword_returned_as_is(self):
        t = self.terms
        self.assertEqual(t.case_match_original("大模型", "大模型突破"), "大模型")
        self.assertEqual(t.case_match_original("", "大模型突破"), "")

    # ---- 4. extract_keywords_dict 返回与原文大小写一致的表面形式 ----

    def test_extract_keywords_dict_returns_original_case(self):
        kws = self.terms.extract_keywords_dict("OpenAI releases GPT-5")
        self.assertIn("GPT-5", kws)
        self.assertIn("OpenAI", kws)

    # ---- 5. _news_row_canons 聚合键仍归一回 canonical ----

    def test_news_row_canons_still_canonical(self):
        # keywords 为空 → 词典抽词兜底；返回的必须是 canonical 键集合
        # （大小写无关归并），而不是原文表面形式。
        row = {"title": "OpenAI releases GPT-5",
               "title_zh": "", "title_en": "", "keywords": ""}
        self.assertEqual(self.terms._news_row_canons(row),
                         {"gpt-5", "openai"})


class DimsCaseMatchTests(unittest.TestCase):
    """dims._llm_classify_batch 的 LLM 抽词结果也过大小写校验（需求 5）。

    mock requests.post（_FakeResp 模式，同 test_mixed_translation.py），
    零 token 消耗。
    """

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._old_env = {k: os.environ.get(k)
                        for k in ("GLM_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["GLM_API_KEY"] = "fake-glm-key"
        os.environ["DEEPSEEK_API_KEY"] = ""
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        self.dims._LLM_ACTIVE_IDX = 0
        self.dims._LLM_FAILS = 0
        self.dims._LLM_CYCLE_FAILS = 0

    def test_llm_keywords_matched_to_original_case(self):
        # LLM 返回 canonical 小写 "gpt-5"，但标题里是 "GPT-5" →
        # 硬编码校验把关键词对齐为原文确切大小写。
        dims = self.dims
        body = {"choices": [{"message": {"content":
            '[{"idx":0,"dimension":"模型与技术",'
            '"title_zh":"OpenAI 发布 GPT-5。",'
            '"title_en":"OpenAI Releases GPT-5 with Advanced Reasoning 0",'
            '"summary_zh":"OpenAI 发布 GPT-5。",'
            '"summary_en":"OpenAI released GPT-5.",'
            '"keywords":["gpt-5"]}]'}}]}
        batch = [{"title": "OpenAI Releases GPT-5 with Advanced Reasoning 0",
                  "source": "S", "lang": "en", "published": "2026-09-01",
                  "official_url": "https://x", "default_dim": "模型与技术"}]
        with patch.object(dims.requests, "post", return_value=_FakeResp(body)):
            dims._llm_classify_batch(batch)
        self.assertEqual(batch[0]["keywords"], ["GPT-5"])


if __name__ == "__main__":
    unittest.main()
