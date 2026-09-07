"""P1（2026-09-07）公开端点限流回归测试。

① ratelimit 单元：固定窗口计数 / 超限拒绝 / 窗口滑过恢复 / key 隔离 /
   limit<=0 与空 key 不限流 / 表满 fail-open（内存有界）。
② 端点：/admin/login 与 /api/event 在窗口内超限返回 429 + Retry-After，
   不同 IP（X-Forwarded-For）各自独立计数。
"""

import importlib
import sys
import types
import unittest
from unittest.mock import patch

import ratelimit


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RateLimitUnitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        ratelimit.reset()
        self._old_now = ratelimit._now
        ratelimit._now = self.clock.now

    def tearDown(self):
        ratelimit._now = self._old_now
        ratelimit.reset()

    def test_window_counts_per_key(self):
        for _ in range(3):
            ok, _ = ratelimit.allow("b", "ip-1", 3, 60)
            self.assertTrue(ok)
        ok, retry = ratelimit.allow("b", "ip-1", 3, 60)
        self.assertFalse(ok)
        self.assertGreater(retry, 0)
        # 不同 key 独立配额
        ok, _ = ratelimit.allow("b", "ip-2", 3, 60)
        self.assertTrue(ok)

    def test_window_slides_after_elapse(self):
        ratelimit.allow("b", "k", 2, 60)
        ratelimit.allow("b", "k", 2, 60)
        ok, _ = ratelimit.allow("b", "k", 2, 60)
        self.assertFalse(ok)
        self.clock.advance(61)
        ok, _ = ratelimit.allow("b", "k", 2, 60)
        self.assertTrue(ok)

    def test_disabled_or_empty_key_never_limited(self):
        ok, _ = ratelimit.allow("b", "k", 0, 60)
        self.assertTrue(ok)
        for _ in range(10):
            ok, _ = ratelimit.allow("b", "", 2, 60)
            self.assertTrue(ok)

    def test_bucket_isolation(self):
        for _ in range(5):
            ratelimit.allow("api_event", "same-ip", 5, 60)
        # 另一 bucket 同 IP 不受影响
        ok, _ = ratelimit.allow("admin_login", "same-ip", 5, 60)
        self.assertTrue(ok)

    def test_table_full_fails_open(self):
        old = ratelimit._MAX_KEYS
        ratelimit._MAX_KEYS = 3
        try:
            for i in range(3):
                ok, _ = ratelimit.allow("b", f"k{i}", 5, 60)
                self.assertTrue(ok)
            # 表满：新 key 放行而非打爆内存
            ok, _ = ratelimit.allow("b", "k-new", 5, 60)
            self.assertTrue(ok)
        finally:
            ratelimit._MAX_KEYS = old


class EndpointRateLimitTests(unittest.TestCase):
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
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            cls.app_module = importlib.import_module("app")
        cls.client = cls.app_module.app.test_client()

    def setUp(self):
        ratelimit.reset()

    def tearDown(self):
        ratelimit.reset()

    def test_admin_login_rate_limited_per_ip(self):
        cfg = self.app_module.config
        with patch.object(cfg, "ADMIN_TOKEN", "topsecret"), \
                patch.object(cfg, "LOGIN_RATE_LIMIT", 2):
            statuses = []
            for _ in range(4):
                resp = self.client.post(
                    "/admin/login", data={"token": "wrong"},
                    headers={"X-Forwarded-For": "9.9.9.9"})
                statuses.append(resp.status_code)
            self.assertEqual(statuses, [401, 401, 429, 429])
            last = self.client.post(
                "/admin/login", data={"token": "wrong"},
                headers={"X-Forwarded-For": "9.9.9.9"})
            self.assertEqual(last.status_code, 429)
            self.assertIn("Retry-After", last.headers)
            body = last.get_json()
            self.assertFalse(body.get("ok"))
            # 别的 IP 不受影响（各自独立计数）
            other = self.client.post(
                "/admin/login", data={"token": "wrong"},
                headers={"X-Forwarded-For": "8.8.8.8"})
            self.assertEqual(other.status_code, 401)

    def test_api_event_rate_limited(self):
        cfg = self.app_module.config
        with patch.object(cfg, "EVENT_RATE_LIMIT", 2):
            def post(ip):
                return self.client.post(
                    "/api/event",
                    json={"event_type": "view_news"},
                    headers={"X-Forwarded-For": ip})

            self.assertEqual(post("1.1.1.1").status_code, 200)
            self.assertEqual(post("1.1.1.1").status_code, 200)
            blocked = post("1.1.1.1")
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.get_json().get("ok"), False)
            self.assertIn("Retry-After", blocked.headers)
            # 别的 IP 正常
            self.assertEqual(post("2.2.2.2").status_code, 200)


if __name__ == "__main__":
    unittest.main()
