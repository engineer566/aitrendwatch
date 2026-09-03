"""Regression tests for the 2026-09-01 issue-11 fix (部分词中英混杂):

1. ``_is_mixed_translation`` 硬编码检查能识别「只翻了一半」的翻译：
   - native zh（中文原文 → 英文翻译）：残留任一 CJK 字符即判混杂；
   - native en（英文原文 → 中文翻译）：长文本 ASCII 字母占比 >60% 判混杂，
     保留常见 AI 术语（GPT-5、OpenAI 等）不误伤。
2. ``_llm_classify_batch`` 把中英混杂翻译按失败计（计入故障转移计数），
   而不是静默回退成原文标题；干净的双语翻译不受影响。
"""

import importlib
import os
import sys
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


def _body(entries):
    return {"choices": [{"message": {"content": "[" + ",".join(entries) + "]"},
                         "finish_reason": "stop"}]}


class MixedTranslationUnitTests(unittest.TestCase):
    """_is_mixed_translation 纯函数单测（不依赖 LLM / key）。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._old_env = {k: os.environ.get(k)
                        for k in ("GLM_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["GLM_API_KEY"] = ""
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

    # ---- native='zh'：中文原文 → 英文翻译，残留 CJK 即混杂 ----
    def test_zh_native_any_cjk_in_en_slot_is_mixed(self):
        self.assertTrue(self.dims._is_mixed_translation("GPT-5 发布 by OpenAI", "zh"))
        self.assertTrue(self.dims._is_mixed_translation("Only one 中 char", "zh"))
        self.assertTrue(self.dims._is_mixed_translation("中文残留", "zh"))

    def test_zh_native_clean_english_passes(self):
        self.assertFalse(
            self.dims._is_mixed_translation("GPT-5 released by OpenAI", "zh"))
        self.assertFalse(self.dims._is_mixed_translation("OpenAI", "zh"))

    # ---- native='en'：英文原文 → 中文翻译，ASCII 占比过高即混杂 ----
    def test_en_native_echoed_english_long_text_is_mixed(self):
        # 整段英文照抄/只翻一半：长文本 ASCII 字母占比 >60%
        self.assertTrue(self.dims._is_mixed_translation(
            "OpenAI Releases GPT-5 with Advanced Reasoning", "en"))

    def test_en_native_chinese_with_ai_terms_passes(self):
        # 保留常见 AI 术语/专名（GPT-5、OpenAI）不误伤
        self.assertFalse(self.dims._is_mixed_translation(
            "GPT-5 发布：OpenAI 推出新一代推理模型", "en"))
        self.assertFalse(self.dims._is_mixed_translation(
            "OpenAI 发布新一代推理模型 GPT-5。", "en"))

    def test_en_native_short_text_skipped(self):
        # 长度 <=15 不判：短串可能是术语密集标题
        self.assertFalse(self.dims._is_mixed_translation("OpenAI GPT-5", "en"))
        self.assertFalse(self.dims._is_mixed_translation("abc", "en"))

    def test_empty_text_counts_as_mixed(self):
        # 空文本按混杂计（调用方先过缺翻译检查，这里只是兜底）
        self.assertTrue(self.dims._is_mixed_translation("", "en"))
        self.assertTrue(self.dims._is_mixed_translation("", "zh"))
        self.assertTrue(self.dims._is_mixed_translation(None, "en"))


class BatchMixedCheckTests(unittest.TestCase):
    """_llm_classify_batch 对中英混杂翻译按失败计（触发故障转移）。"""

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
        cls.config = config

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
        self.dims._LLM_QUALITY_FAILS = 0
        self.dims._LLM_QUALITY_CYCLE_FAILS = 0

    def _en_batch(self):
        return [{"title": "OpenAI Releases GPT-5 with Advanced Reasoning %d" % i,
                 "source": "S", "lang": "en", "published": "2026-09-01",
                 "official_url": "https://x", "default_dim": "模型与技术"}
                for i in range(6)]

    def test_english_title_echoed_into_zh_slot_is_mixed_failure(self):
        # 英文源：title_zh 仍是整段英文（漏翻/照抄）→ 混杂 → 按失败计
        dims = self.dims
        entries = []
        for i in range(6):
            entries.append(
                '{"idx":%d,"dimension":"模型与技术",'
                '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning %d",'
                '"title_en":"OpenAI Releases GPT-5 with Advanced Reasoning %d",'
                '"summary_zh":"OpenAI 发布 GPT-5。",'
                '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}'
                % (i, i, i))
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_body(entries))):
            with self.assertRaises(dims._LLMQualityError):
                dims._llm_classify_batch(self._en_batch())
        # 混杂属质量失败（2026-09-03）：不触发快速换档（_LLM_FAILS 不动），
        # 只走高阈值质量熔断计数（_LLM_QUALITY_FAILS +1，连续 6 次才换档）
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_QUALITY_FAILS, 1)

    def test_chinese_summary_mixed_with_english_is_failure(self):
        # 英文源：title_zh 已翻译，但 summary_zh 仍是英文 → 同样按失败计
        dims = self.dims
        entries = []
        for i in range(6):
            entries.append(
                '{"idx":%d,"dimension":"模型与技术",'
                '"title_zh":"OpenAI 发布新一代推理模型 GPT-5。",'
                '"title_en":"OpenAI Releases GPT-5 with Advanced Reasoning %d",'
                '"summary_zh":"OpenAI announced GPT-5 with 100x speedup",'
                '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}'
                % (i, i))
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_body(entries))):
            with self.assertRaises(dims._LLMQualityError):
                dims._llm_classify_batch(self._en_batch())
        # 混杂属质量失败（2026-09-03）：_LLM_FAILS 不动，质量熔断计数 +1
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_QUALITY_FAILS, 1)

    def test_zh_native_cjk_remaining_in_en_slot_is_mixed_failure(self):
        # 中文源：title_en 仍残留中文 → 混杂 → 按失败计
        dims = self.dims
        batch = [{"title": "OpenAI 发布 GPT-5 %d" % i, "source": "S",
                  "lang": "zh", "published": "2026-09-01",
                  "official_url": "https://x", "default_dim": "模型与技术"}
                 for i in range(6)]
        entries = []
        for i in range(6):
            entries.append(
                '{"idx":%d,"dimension":"模型与技术",'
                '"title_zh":"OpenAI 发布 GPT-5 %d",'
                '"title_en":"OpenAI 发布 GPT-5 %d",'
                '"summary_zh":"OpenAI 发布 GPT-5。",'
                '"summary_en":"OpenAI released GPT-5.","keywords":[]}'
                % (i, i, i))
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_body(entries))):
            with self.assertRaises(dims._LLMQualityError):
                dims._llm_classify_batch(batch)
        # 混杂属质量失败（2026-09-03）：_LLM_FAILS 不动，质量熔断计数 +1
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_QUALITY_FAILS, 1)

    def test_clean_bilingual_translations_pass_mixed_check(self):
        # 干净的双语翻译：混杂检查不误伤，正常回填
        dims = self.dims
        entries = []
        for i in range(6):
            entries.append(
                '{"idx":%d,"dimension":"模型与技术",'
                '"title_zh":"OpenAI 发布 GPT-5，推理性能大幅提升",'
                '"title_en":"OpenAI Releases GPT-5 with Advanced Reasoning %d",'
                '"summary_zh":"OpenAI 发布新一代推理模型 GPT-5。",'
                '"summary_en":"OpenAI released GPT-5, its new reasoning model.",'
                '"keywords":["gpt-5"]}' % (i, i))
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_body(entries))):
            items = self._en_batch()
            dims._llm_classify_batch(items)
        self.assertEqual(dims._LLM_FAILS, 0)
        for it in items:
            self.assertEqual(it["title_en"], it["title"])       # 原生槽 = 原标题
            self.assertTrue(it["title_zh"].startswith("OpenAI 发布"))
            self.assertTrue(it["summary_zh"].strip())
            self.assertTrue(it["summary_en"].strip())


if __name__ == "__main__":
    unittest.main()
