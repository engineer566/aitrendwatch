"""2026-09-17 生产 DNS 风暴事故的修复回归测试（环境级网络死亡熔断）。

事故：宿主机资源耗尽 → 容器 DNS（dockerd 内嵌 127.0.0.11）饿死 → 刷新管线
（36 RSS + 每卡 HN/Reddit + 全部 LLM 批次）不感知环境死亡，逐批次×3档×3次
重试空转几十分钟；gunicorn 60s 超时 SIGKILL worker → 新 worker import app
无条件重跑整轮 → 风暴自我放大（19:18~20:09 连续 10+ 次 WORKER TIMEOUT）。

修复（本文件锁定的行为）：
1. ``_net_dead_note``：环境级 DNS 失败（NameResolutionError 等）按轮累计，
   达 ``_NET_DEAD_LIMIT`` 即判定环境死亡；非 DNS 失败（429 等）不计数。
2. ``fetch_one_rss``/``enrich_with_signals``/``_llm_classify_batch``：达阈值
   抛 ``_NetDeadError`` 中止剩余抓取/信号/打标；``enrich_with_llm`` 不吞
   ``_NetDeadError``（不降级不重试）。
3. ``_translate_terms``/``explain_terms``：达阈值 break 保留部分结果，不整轮作废。
4. ``_startup_refresh_once``（dims）/``_startup_refresh``（tracker）：启动预热
   前检查文件缓存新鲜度，新鲜（<3h）则跳过——worker churn 不再放大成刷新风暴。
"""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

import requests


def _fcntl_stub():
    if "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8
        stub.flock = lambda *args: None
        sys.modules["fcntl"] = stub


def _dns_exc():
    """模拟 getaddrinfo 失败被 urllib3 包装后的 ConnectionError。"""
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='open.bigmodel.cn', port=443): Max retries "
        "exceeded with url: /api/paas/v4/chat/completions (Caused by "
        "NameResolutionError(\"HTTPSConnection(host='open.bigmodel.cn', "
        "port=443): Failed to resolve 'open.bigmodel.cn' "
        "([Errno -3] Temporary failure in name resolution)\"))")


class NetDeadBreakerTests(unittest.TestCase):
    """修复 1+2：环境级 DNS 失败计数、熔断抛错与传播。"""

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

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.dims._net_dead_reset()

    def test_is_net_dead_detection(self):
        self.assertTrue(self.dims._is_net_dead(_dns_exc()))
        self.assertTrue(self.dims._is_net_dead(
            Exception("Temporary failure in name resolution")))
        # 429/超时等非 DNS 失败不触发熔断
        self.assertFalse(self.dims._is_net_dead(
            requests.exceptions.HTTPError("429 Client Error: Too Many Requests")))
        self.assertFalse(self.dims._is_net_dead(
            requests.exceptions.ReadTimeout("read timed out")))

    def test_note_counts_and_trips_at_limit(self):
        d = self.dims
        for _ in range(d._NET_DEAD_LIMIT - 1):
            self.assertFalse(d._net_dead_note(_dns_exc()))
        self.assertTrue(d._net_dead_note(_dns_exc()))  # 第 LIMIT 次达阈值
        self.assertTrue(d._net_dead_tripped())
        d._net_dead_reset()
        self.assertFalse(d._net_dead_tripped())

    def test_fetch_one_rss_silent_then_raises(self):
        d = self.dims
        src = {"feed": "https://example.com/rss", "name": "X"}
        with patch.object(d.requests, "get", side_effect=_dns_exc()):
            # 未达阈值：按原契约静默返回 []
            self.assertEqual(d.fetch_one_rss(src), [])
            for _ in range(d._NET_DEAD_LIMIT - 2):
                self.assertEqual(d.fetch_one_rss(src), [])
            with self.assertRaises(d._NetDeadError):
                d.fetch_one_rss(src)
        d._net_dead_reset()

    def test_enrich_with_signals_aborts(self):
        d = self.dims
        items = [{"title": f"AI news {i}", "url": "https://x",
                  "default_dim": "其他"} for i in range(20)]
        with patch.object(d.requests, "get", side_effect=_dns_exc()):
            with self.assertRaises(d._NetDeadError):
                d.enrich_with_signals(items)
        d._net_dead_reset()

    def test_enrich_with_llm_reraises_net_dead(self):
        d = self.dims
        items = [{"title": f"OpenAI news {i}", "source": "S", "lang": "en",
                  "published": "2026-09-01", "official_url": "https://x",
                  "default_dim": "模型与技术"} for i in range(12)]
        with patch.object(d, "_llm_classify_batch",
                          side_effect=d._NetDeadError("dns dead")):
            with self.assertRaises(d._NetDeadError):
                d.enrich_with_llm(items)

    def test_llm_classify_batch_fast_fail_when_tripped(self):
        d = self.dims
        batch = [{"title": "AI", "source": "S", "lang": "en",
                  "default_dim": "模型与技术"}]
        for _ in range(d._NET_DEAD_LIMIT):
            d._net_dead_note(_dns_exc())
        with patch.object(d.requests, "post",
                          side_effect=AssertionError("不应再发起网络调用")):
            with self.assertRaises(d._NetDeadError):
                d._llm_classify_batch(batch)
        d._net_dead_reset()

    def test_llm_classify_batch_aborts_retries_on_dns_death(self):
        d = self.dims
        # 预置接近阈值：_post 首轮重试即达阈值，立即中止且不再烧剩余重试
        d._net_dead_reset()
        for _ in range(d._NET_DEAD_LIMIT - 2):
            d._net_dead_note(_dns_exc())
        batch = [{"title": "AI", "source": "S", "lang": "en",
                  "default_dim": "模型与技术"}]
        calls = []

        def _count_post(*a, **kw):
            calls.append(1)
            raise _dns_exc()

        with patch.object(d.requests, "post", side_effect=_count_post):
            with self.assertRaises(d._NetDeadError):
                d._llm_classify_batch(batch)
        self.assertLessEqual(len(calls), 2)  # 第 2 次重试前已熔断
        d._net_dead_reset()

    def test_translate_terms_breaks_and_keeps_partial(self):
        d = self.dims
        # 第一块成功、其后 DNS 死亡：返回已解析的部分结果且不抛
        d._net_dead_reset()

        def _fake_post(url, **kw):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    content = '{"大模型": "LLM"}'
                    return {"choices": [{"message": {"content": content}}]}
            return R()

        calls = {"n": 0}

        def _seq_post(url, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise _dns_exc()
            return _fake_post(url, **kw)

        with patch.object(d.requests, "post", side_effect=_seq_post):
            out = d._translate_terms(["大模型", "智能体", "多模态",
                                      "智能合约", "智算中心", "具身智能",
                                      "端侧模型", "世界模型", "AI 编程",
                                      "数据飞轮", "推理芯片", "开源权重",
                                      "模型蒸馏", "智能体编排", "多智能体"])
        self.assertEqual(out.get("大模型"), "LLM")
        d._net_dead_reset()


class StartupFreshnessSkipTests(unittest.TestCase):
    """修复 4：worker 启动预热前检查缓存新鲜度，新鲜则跳过。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        os.environ.setdefault("GLM_API_KEY", "")
        os.environ.setdefault("DEEPSEEK_API_KEY", "")
        import dims
        import tracker
        importlib.reload(dims)
        importlib.reload(tracker)
        cls.dims = dims
        cls.tracker = tracker

    def test_dims_startup_skips_when_fresh(self):
        d = self.dims
        calls = []
        with patch.object(d, "_dims_cache_age", return_value=600.0), \
             patch.object(d, "_dims_refresh_once",
                          side_effect=lambda: calls.append(1) or True):
            self.assertTrue(d._startup_refresh_once())
        self.assertEqual(calls, [])  # 缓存新鲜：未触发全量预热

    def test_dims_startup_runs_when_stale(self):
        d = self.dims
        calls = []
        with patch.object(d, "_dims_cache_age",
                          return_value=float("inf")), \
             patch.object(d, "_dims_refresh_once",
                          side_effect=lambda: calls.append(1) or True):
            self.assertTrue(d._startup_refresh_once())
        self.assertEqual(len(calls), 1)  # 冷启动/缓存过期：立即预热

    def test_tracker_startup_skips_when_fresh(self):
        t = self.tracker
        calls = []
        with patch.object(t, "_cache_age", return_value=600.0), \
             patch.object(t, "_refresh_once",
                          side_effect=lambda s: calls.append(s) or True):
            t._startup_refresh()
        self.assertEqual(calls, [])

    def test_tracker_startup_runs_when_stale(self):
        t = self.tracker
        calls = []
        with patch.object(t, "_cache_age",
                          return_value=float("inf")), \
             patch.object(t, "_refresh_once",
                          side_effect=lambda s: calls.append(s) or True):
            t._startup_refresh()
        self.assertEqual(sorted(calls), ["top", "trending"])


if __name__ == "__main__":
    unittest.main()
