"""Regression tests for the 2026-08-31 GLM-5.3-Flash prompt/thinking optimizations:

1. ``reasoning_effort`` is attached only for GLM-5.2+ models (glm-5.3-flash),
   never for glm-4.7 or deepseek (unknown param would error the request).
   Default ``LLM_REASONING_EFFORT=low`` reduces thinking token consumption,
   which in turn lowers max_tokens truncation (finish=length → empty content).
2. The optimized classify prompt explicitly forbids echoing the original title
   into the translated slot, requires non-empty translation fields, exact
   array length matching input, and forbids Markdown code fences.
3. A single-tier chain ``LLM_CHAIN="glm-5.3-flash"`` (GLM-5.3 alone) works
   end-to-end; the no-key degradation path is untouched.
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


def _full_batch_body(count=6):
    entries = []
    for i in range(count):
        entries.append(
            '{"idx":%d,"dimension":"模型与技术","title_zh":"中文标题%d",'
            '"title_en":"English title %d","summary_zh":"中文摘要%d。",'
            '"summary_en":"English summary %d.","keywords":["GPT-5"]}'
            % (i, i, i, i, i))
    return {"choices": [{"message": {"content": "[" + ",".join(entries) + "]"},
                         "finish_reason": "stop"}]}


class _Env:
    """setUpClass 级别的环境管理：只覆盖 LLM 相关变量（不动 DATA_DIR 等，避免
    污染同进程后续测试文件），记录旧值、清理时恢复并重载 config。"""

    def __init__(self, **env):
        self._old = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            os.environ[k] = v

    def cleanup(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import config
        importlib.reload(config)


class ReasoningParamTests(unittest.TestCase):
    """config.llm_reasoning_params 只对 GLM-5.2+ 返回 reasoning_effort。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._env = _Env(GLM_API_KEY="fake-glm-key", DEEPSEEK_API_KEY="")
        import config
        importlib.reload(config)
        cls.config = config

    @classmethod
    def tearDownClass(cls):
        cls._env.cleanup()

    def test_glm53_flash_gets_low_effort(self):
        self.assertEqual(self.config.llm_reasoning_params("glm-5.3-flash"),
                         {"reasoning_effort": "low"})

    def test_glm52_gets_low_effort(self):
        self.assertEqual(self.config.llm_reasoning_params("glm-5.2"),
                         {"reasoning_effort": "low"})

    def test_glm51_gets_nothing(self):
        self.assertEqual(self.config.llm_reasoning_params("glm-5.1"), {})

    def test_glm47_gets_nothing(self):
        self.assertEqual(self.config.llm_reasoning_params("glm-4.7-flash"), {})

    def test_deepseek_gets_nothing(self):
        self.assertEqual(self.config.llm_reasoning_params("deepseek-v4-flash"), {})

    def test_env_override_high(self):
        os.environ["LLM_REASONING_EFFORT"] = "high"
        import config
        importlib.reload(config)
        self.assertEqual(config.llm_reasoning_params("glm-5.3-flash"),
                         {"reasoning_effort": "high"})
        os.environ.pop("LLM_REASONING_EFFORT", None)
        importlib.reload(config)

    def test_invalid_value_falls_back_to_low(self):
        os.environ["LLM_REASONING_EFFORT"] = "ultra"
        import config
        importlib.reload(config)
        self.assertEqual(config.llm_reasoning_params("glm-5.3-flash"),
                         {"reasoning_effort": "low"})
        os.environ.pop("LLM_REASONING_EFFORT", None)
        importlib.reload(config)


class GLM53SingleTierTests(unittest.TestCase):
    """LLM_CHAIN="glm-5.3-flash" 单档：payload 带 reasoning_effort，批次成功。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._env = _Env(GLM_API_KEY="fake-glm-key", DEEPSEEK_API_KEY="",
                        LLM_CHAIN="glm-5.3-flash")
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    @classmethod
    def tearDownClass(cls):
        cls._env.cleanup()

    def _batch(self):
        return [{"title": "Some AI news headline %d" % i, "source": "S",
                 "lang": "en", "published": "2026-08-28",
                 "official_url": "https://x", "default_dim": "模型与技术"}
                for i in range(6)]

    def test_payload_carries_reasoning_effort(self):
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(_full_batch_body(6))

        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post", side_effect=_fake_post):
            items = self._batch()
            dims._llm_classify_batch(items)
        self.assertEqual(captured["payload"]["reasoning_effort"], "low")
        self.assertEqual(captured["payload"]["model"], "glm-5.3-flash")

    def test_prompt_forbids_echo_and_requires_fields(self):
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(_full_batch_body(6))

        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post", side_effect=_fake_post):
            dims._llm_classify_batch(self._batch())
        user_msg = captured["payload"]["messages"][1]["content"]
        sys_msg = captured["payload"]["messages"][0]["content"]
        self.assertIn("不得照抄英文原标题", user_msg)
        self.assertIn("翻译字段禁止空字符串", user_msg)
        self.assertIn("数组长度必须与输入条数一致", user_msg)
        self.assertIn("Markdown代码块", user_msg)
        self.assertIn("不要任何解释、前后缀、Markdown代码块或思考过程", sys_msg)

    def test_single_tier_batch_backfills_translations(self):
        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_full_batch_body(6))):
            items = self._batch()
            dims._llm_classify_batch(items)
        for it in items:
            self.assertEqual(it["dimension"], "模型与技术")
            self.assertTrue(it["title_zh"].startswith("中文标题"))
            self.assertEqual(it["title_en"], it["title"])  # 原生语言槽 = 原标题
            self.assertTrue(it["summary_zh"].strip())
            self.assertTrue(it["summary_en"].strip())
            self.assertEqual(it["keywords"], ["gpt-5"])  # normalize_term 归一为小写
        # 单档成功不清零切档：仍在链首（len=1 即末档）
        self.assertEqual(dims._LLM_ACTIVE_IDX, 0)
        self.assertEqual(dims._LLM_FAILS, 0)


    def test_translate_terms_payload_carries_reasoning_effort(self):
        # _translate_terms 是独立 payload 路径（热词翻译），同样应挂 reasoning_effort。
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp({"choices": [{
                "message": {"content": '{"大模型": "LLM"}'},
                "finish_reason": "stop"}]})

        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post", side_effect=_fake_post):
            out = dims._translate_terms(["大模型"])
        self.assertEqual(out, {"大模型": "LLM"})
        self.assertEqual(captured["payload"]["reasoning_effort"], "low")


class LegacyTierNoEffortTests(unittest.TestCase):
    """glm-4.7 与 deepseek 档不得携带 reasoning_effort（未知参数会报错）。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._env = _Env(GLM_API_KEY="fake-glm-key", DEEPSEEK_API_KEY="fake-ds-key",
                        LLM_CHAIN="glm-4.7-flash,deepseek-v4-flash")
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    @classmethod
    def tearDownClass(cls):
        cls._env.cleanup()

    def _capture_once(self, chain_model):
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResp(_full_batch_body(6))

        dims = self.dims
        dims._LLM_ACTIVE_IDX = self.config.LLM_CHAIN.index(chain_model)
        dims._LLM_FAILS = 0
        with patch.object(dims.requests, "post", side_effect=_fake_post):
            dims._llm_classify_batch(
                [{"title": "T%d" % i, "source": "S", "lang": "en",
                  "published": "2026-08-28", "official_url": "https://x",
                  "default_dim": "模型与技术"} for i in range(6)])
        return captured["payload"]

    def test_glm47_payload_has_no_reasoning_effort(self):
        payload = self._capture_once("glm-4.7-flash")
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["model"], "glm-4.7-flash")

    def test_deepseek_payload_has_no_reasoning_effort(self):
        payload = self._capture_once("deepseek-v4-flash")
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["model"], "deepseek-v4-flash")


class NoKeyDegradationTests(unittest.TestCase):
    """无 key 时降级路径不变：default_dim + 原标题 + 前 30 字摘要。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._env = _Env(GLM_API_KEY="", DEEPSEEK_API_KEY="",
                        LLM_CHAIN="glm-5.3-flash")
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims

    @classmethod
    def tearDownClass(cls):
        cls._env.cleanup()

    def test_enrich_with_llm_falls_back_to_default_dim(self):
        items = [{"title": "Meta opensources its most powerful AI model",
                  "source": "Yahoo Finance", "lang": "en",
                  "published": "2026-08-29",
                  "official_url": "https://x", "default_dim": "模型与技术"}]
        self.dims.enrich_with_llm(items)
        it = items[0]
        self.assertEqual(it["dimension"], "模型与技术")
        self.assertEqual(it["title_zh"], it["title"])          # 原标题
        self.assertEqual(it["summary_zh"], it["title"][:30])   # 前 30 字


if __name__ == "__main__":
    unittest.main()
