"""2026-09-02 DeepSeek 用量事故的修复回归测试（LLM 成本护栏）。

事故：故障转移链进程级单向下沉（成功清零计数但不回退首档），两 worker 先后
逃逸到 DeepSeek（最贵档）后整日不回收 → 4 轮定点刷新全量打 DeepSeek
（日 ~300+ 次调用），账户余额被打穿（HTTP 402 欠费）、站点 LLM 全面降级。

修复（本文件锁定的行为）：
1. ``_llm_cycle_reset``：每轮 dims 刷新起始把链复位回链首——DeepSeek 只做
   「当轮逃生舱」，不许跨轮沉淀。
2. HTTP 402（余额不足）归类为账户级限流（``_LLMAccountRateLimit``），非瞬态
   不重试，顺链跳过，避免对欠费账户每轮反复白打。
3. ``_llm_classify_batch`` 逐条校验：缺翻译/混杂的坏条目不回填并整批按失败计，
   但已通过校验的好条目保留 LLM 结果；``enrich_with_llm`` 只降级/重试坏条目，
   不再整批 6 条重来（省近 1 倍调用量）。
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
    """choices 响应体：entries 为逐条 JSON 对象字符串（含 idx）。"""
    return {"choices": [{"message": {"content": "[" + ",".join(entries) + "]"},
                         "finish_reason": "stop"}]}


def _en_item(i, title=None):
    return {"title": title or ("OpenAI Releases GPT-5 %d" % i),
            "source": "S", "lang": "en", "published": "2026-09-01",
            "official_url": "https://x", "default_dim": "模型与技术"}


class LLMCycleResetTests(unittest.TestCase):
    """修复 1：每轮刷新起始链复位回链首（DeepSeek 只做当轮逃生舱）。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._old_env = {k: os.environ.get(k)
                        for k in ("GLM_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["GLM_API_KEY"] = "fake-glm-key"
        os.environ["DEEPSEEK_API_KEY"] = "fake-ds-key"
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    def setUp(self):
        self.dims._LLM_ACTIVE_IDX = 0
        self.dims._LLM_FAILS = 0
        self.dims._LLM_CYCLE_FAILS = 0
        self.dims._LLM_QUALITY_FAILS = 0
        self.dims._LLM_QUALITY_CYCLE_FAILS = 0

    def test_cycle_reset_returns_chain_to_head(self):
        # 模拟整日事故状态：worker 已逃逸到最贵档（末档 deepseek）
        dims = self.dims
        dims._LLM_ACTIVE_IDX = len(self.config.LLM_CHAIN) - 1
        dims._LLM_FAILS = 3
        dims._LLM_CYCLE_FAILS = 9
        dims._llm_cycle_reset()
        self.assertEqual(dims._LLM_ACTIVE_IDX, 0)      # 回到链首 GLM
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_CYCLE_FAILS, 0)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LLM402AccountLevelTests(unittest.TestCase):
    """修复 2：HTTP 402（余额不足）等同账户级限流，非瞬态、不逐档烧重试。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._old_env = {k: os.environ.get(k)
                        for k in ("GLM_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["GLM_API_KEY"] = "fake-glm-key"
        os.environ["DEEPSEEK_API_KEY"] = "fake-ds-key"
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    def setUp(self):
        self.dims._LLM_ACTIVE_IDX = 0
        self.dims._LLM_FAILS = 0
        self.dims._LLM_CYCLE_FAILS = 0
        self.dims._LLM_QUALITY_FAILS = 0
        self.dims._LLM_QUALITY_CYCLE_FAILS = 0

    def test_402_is_account_level_and_skips_provider_family(self):
        dims = self.dims
        exc = dims.requests.exceptions.HTTPError(
            "402 Client Error: Payment Required")
        exc.response = types.SimpleNamespace(status_code=402)
        with patch.object(dims.requests, "post", side_effect=exc):
            with self.assertRaises(dims._LLMAccountRateLimit):
                dims._llm_classify_batch([_en_item(0)])
        # 账户级限流语义：跳过当前 provider（GLM 全档）直达 DeepSeek，
        # 而不是烧满 LLM_FAILOVER_THRESHOLD 次重试
        self.assertEqual(
            self.config.LLM_CHAIN[dims._LLM_ACTIVE_IDX], "deepseek-v4-flash")
        self.assertEqual(dims._LLM_FAILS, 0)  # 不累计普通失败计数

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class LLMPartialFillTests(unittest.TestCase):
    """修复 3：逐条校验回填——好条目保留结果，坏条目才进重试集。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._old_env = {k: os.environ.get(k)
                        for k in ("GLM_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["GLM_API_KEY"] = "fake-glm-key"
        os.environ["DEEPSEEK_API_KEY"] = "fake-ds-key"
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims
        cls.config = config

    def setUp(self):
        self.dims._LLM_ACTIVE_IDX = 0
        self.dims._LLM_FAILS = 0
        self.dims._LLM_CYCLE_FAILS = 0
        self.dims._LLM_QUALITY_FAILS = 0
        self.dims._LLM_QUALITY_CYCLE_FAILS = 0

    def _clean_entry(self, i):
        return ('{"idx":%d,"dimension":"模型与技术",'
                '"title_zh":"OpenAI 发布新一代推理模型 GPT-5，性能大幅提升",'
                '"title_en":"OpenAI Releases GPT-5 %d",'
                '"summary_zh":"OpenAI 发布 GPT-5。",'
                '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}'
                % (i, i))

    def test_mixed_sub_batch_keeps_good_items_filled(self):
        # 6 条里 1 条混杂（idx5 title_zh 仍是英文）：属质量失败（_LLM_QUALITY_FAILS
        # +1，2026-09-03 起不触发快速换档），前 5 条已回填、坏条目不回填——
        # 经二次提示修正（mock 固定返回同一坏内容）后仍失败才 raise。
        dims = self.dims
        entries = [self._clean_entry(i) for i in range(6)]
        entries[5] = ('{"idx":5,"dimension":"模型与技术",'
                      '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning 5",'
                      '"title_en":"OpenAI Releases GPT-5 5",'
                      '"summary_zh":"OpenAI 发布 GPT-5。",'
                      '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}')
        batch = [_en_item(i) for i in range(6)]
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(_body(entries))):
            with self.assertRaises(dims._LLMQualityError):
                dims._llm_classify_batch(batch)
        # 质量失败：可用性失败计数不动，质量熔断计数 +1
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_QUALITY_FAILS, 1)
        # 前 5 条（通过校验）已回填
        for it in batch[:5]:
            self.assertTrue((it.get("title_zh") or "").strip())
            self.assertTrue((it.get("summary_zh") or "").strip())
            self.assertEqual(it["dimension"], "模型与技术")
        # 混杂的第 6 条未回填 → enrich 判定为坏条目，只重试它
        self.assertFalse((batch[5].get("title_zh") or "").strip())

    def test_enrich_retries_only_bad_items(self):
        # 12 条（两个 6 条子批）各 1 条坏：重试只发 2 条坏条目，好条目不重发
        dims = self.dims
        items = [_en_item(i) for i in range(12)]
        calls = []

        def _fill(it):
            it["dimension"] = it.get("default_dim") or "模型与技术"
            it["title"] = decode(it["title"])
            it["title_en"] = it["title"]
            it["title_zh"] = "译文 " + it["title"][:40]
            it["summary_en"] = "Summary of the story."
            it["summary_zh"] = "故事摘要。"
            it["keywords"] = []

        def decode(s):
            return s

        def fake_classify(sub):
            calls.append(len(sub))
            if len(sub) == 6:
                # 模拟「5 好 1 坏」：坏条目不回填并抛异常
                for it in sub[:-1]:
                    _fill(it)
                raise RuntimeError("LLM 返回 1/6 条未通过校验，按失败计")
            for it in sub:  # 末尾重试批次：全部成功
                _fill(it)

        with patch.object(dims, "_llm_classify_batch",
                          side_effect=fake_classify):
            dims.enrich_with_llm(items)
        # 主循环两个 6 条子批 + 末尾重试 2 条坏条目（不再整批 6 条重来）
        self.assertEqual(calls, [6, 6, 2])
        for it in items:
            self.assertTrue((it.get("title_zh") or "").strip())
            self.assertTrue((it.get("summary_zh") or "").strip())

    def test_repair_pass_second_prompt_fixes_bad_item(self):
        # 首轮 1 条混杂 → 二次提示（带失败原因）只重发坏条目 → 修正成功：
        # 批次整体成功、不换档、调用只多 1 次（2026-09-03 用户要求）。
        dims = self.dims
        clean = [self._clean_entry(i) for i in range(6)]
        bad = list(clean)
        bad[5] = ('{"idx":5,"dimension":"模型与技术",'
                  '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning 5",'
                  '"title_en":"OpenAI Releases GPT-5 5",'
                  '"summary_zh":"OpenAI 发布 GPT-5。",'
                  '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}')
        calls = []

        def _fake_post(url, headers=None, json=None, timeout=None):
            content = json["messages"][1]["content"]
            calls.append(content)
            if len(calls) == 1:
                return _FakeResp(_body(bad))    # 首轮：idx5 混杂
            return _FakeResp(_body(clean))      # 二次提示：全部修正

        batch = [_en_item(i) for i in range(6)]
        with patch.object(dims.requests, "post", side_effect=_fake_post):
            dims._llm_classify_batch(batch)
        # 主调用 1 次 + 二次提示 1 次；第二段 user 消息带失败原因
        self.assertEqual(len(calls), 2)
        self.assertIn("上次未通过: 混杂", calls[1])
        # 修正成功：整批回填、质量/可用性失败计数都为零（不换档）
        self.assertEqual(dims._LLM_FAILS, 0)
        self.assertEqual(dims._LLM_QUALITY_FAILS, 0)
        self.assertEqual(dims._LLM_ACTIVE_IDX, 0)
        for it in batch:
            self.assertTrue((it.get("title_zh") or "").strip())
            self.assertTrue((it.get("summary_zh") or "").strip())

    def test_quality_failure_does_not_fast_failover(self):
        # 零星质量失败（连续 2 次 < 6 阈值）：不换档、可用性计数不动（2026-09-03）
        dims = self.dims
        clean = [self._clean_entry(i) for i in range(6)]
        bad = list(clean)
        bad[3] = ('{"idx":3,"dimension":"模型与技术",'
                  '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning 3",'
                  '"title_en":"OpenAI Releases GPT-5 3",'
                  '"summary_zh":"OpenAI 发布 GPT-5。",'
                  '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}')
        body = _body(bad)
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(body)):
            for _ in range(2):
                with self.assertRaises(dims._LLMQualityError):
                    dims._llm_classify_batch([_en_item(i) for i in range(6)])
        self.assertEqual(dims._LLM_ACTIVE_IDX, 0)          # 仍在链首
        self.assertEqual(dims._LLM_FAILS, 0)               # 可用性计数不动
        self.assertEqual(dims._LLM_QUALITY_FAILS, 2)       # 质量熔断计数 +2

    def test_quality_failover_after_six_consecutive(self):
        # 系统性质量恶化（连续 6 次整批混杂）→ 质量熔断才换档兜底
        dims = self.dims
        clean = [self._clean_entry(i) for i in range(6)]
        bad = list(clean)
        bad[0] = ('{"idx":0,"dimension":"模型与技术",'
                  '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning 0",'
                  '"title_en":"OpenAI Releases GPT-5 0",'
                  '"summary_zh":"OpenAI 发布 GPT-5。",'
                  '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}')
        body = _body(bad)
        with patch.object(dims.requests, "post",
                          return_value=_FakeResp(body)):
            for _ in range(6):
                with self.assertRaises(dims._LLMQualityError):
                    dims._llm_classify_batch([_en_item(i) for i in range(6)])
        # 连续 6 次质量失败 → 顺链切到 glm-5.3（第 2 档）
        self.assertEqual(
            self.config.LLM_CHAIN[dims._LLM_ACTIVE_IDX], "glm-5.3-flash")

    def test_repair_rounds_are_bounded(self):
        # 坏条目二次提示最多 LLM_REPAIR_ROUNDS 轮：固定坏响应 → 1 主 + 2 修正 = 3 次调用
        dims = self.dims
        clean = [self._clean_entry(i) for i in range(6)]
        bad = list(clean)
        bad[2] = ('{"idx":2,"dimension":"模型与技术",'
                  '"title_zh":"OpenAI Releases GPT-5 with Advanced Reasoning 2",'
                  '"title_en":"OpenAI Releases GPT-5 2",'
                  '"summary_zh":"OpenAI 发布 GPT-5。",'
                  '"summary_en":"OpenAI released GPT-5.","keywords":["gpt-5"]}')
        n_posts = [0]

        def _fake_post(url, headers=None, json=None, timeout=None):
            n_posts[0] += 1
            return _FakeResp(_body(bad))   # 每次都返回同样的坏内容

        with patch.object(dims.requests, "post", side_effect=_fake_post):
            with self.assertRaises(dims._LLMQualityError):
                dims._llm_classify_batch([_en_item(i) for i in range(6)])
        # 1 次主调用 + 2 轮二次提示（LLM_REPAIR_ROUNDS=2），不再无限重试
        self.assertEqual(n_posts[0], 3)

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
