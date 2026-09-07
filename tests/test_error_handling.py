"""P2（2026-09-07）500 错误处理回归测试。

此前只有 404 errorhandler，未捕获异常裸露 Flask/nginx 默认错误页。
验证：页面请求 500 → noindex HTML；/api/* 或 Accept: application/json
请求 500 → JSON {ok:false}。
"""

import importlib
import sys
import types
import unittest
from unittest.mock import patch


class ErrorHandlerTests(unittest.TestCase):
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

    @staticmethod
    def _handler():
        spec = (ErrorHandlerTests.app_module.app
                .error_handler_spec.get(None, {}).get(500))
        # Flask 内部把注册的 handler 挂在「异常类 → handler」dict 下
        if isinstance(spec, dict):
            spec = next(iter(spec.values()))
        if isinstance(spec, list):
            return spec[0]
        return spec

    def test_page_500_returns_noindex_html(self):
        app = self.app_module.app
        with app.test_request_context("/", headers={"Accept": "text/html"}):
            resp = self._handler()(RuntimeError("boom"))
        self.assertEqual(resp.status_code, 500)
        body = resp.get_data(as_text=True)
        self.assertIn("noindex,nofollow", body)
        self.assertIn("500", body)

    def test_api_500_returns_json(self):
        app = self.app_module.app
        with app.test_request_context("/api/stream", headers={"Accept": "application/json"}):
            resp = self._handler()(RuntimeError("boom"))
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.mimetype, "application/json")
        body = resp.get_json()
        self.assertFalse(body.get("ok"))
        self.assertIn("error", body)

    def test_api_path_returns_json_even_without_accept(self):
        app = self.app_module.app
        with app.test_request_context("/api/stream"):
            resp = self._handler()(RuntimeError("boom"))
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.mimetype, "application/json")


if __name__ == "__main__":
    unittest.main()
