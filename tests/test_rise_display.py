"""回归：词页 Rise 显示 -1.00 的修复测试。

cur_rise == -1.0 表示该词本周期无活跃报道（m_cur=0 的环比占位），不是真实
「下跌 100%」。修复后：
- term_detail.html / index.html 的 rise 展示条件都要求 rise > -0.999；
- _explain_fallback 的解释文案不再输出 "Rise -1.00" / "环比上升 -1.00"。
"""

import os
import sys
import types
import unittest
from pathlib import Path


# 工作区测试严禁使用真实 LLM key；删除环境变量只影响本测试进程。
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GLM_API_KEY", None)

# Windows 本地测试没有 fcntl；这些测试不执行锁路径。
if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: None
    )

import tracker  # noqa: E402
import dims  # noqa: E402

tracker.start_background_refresher = lambda: None
dims.start_background_dims_refresher = lambda: None

import app as app_module  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class RiseDisplayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.term_detail = (ROOT / "templates" / "term_detail.html").read_text(
            encoding="utf-8"
        )
        cls.index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    # ---- 详情页模板：rise == -1.0 时隐藏 ----

    def test_term_detail_hides_inactive_rise(self):
        self.assertIn("word.term.rise > -0.999", self.term_detail)

    def test_term_detail_keeps_none_guard(self):
        self.assertIn("word.term.rise != none and word.term.rise > -0.999",
                      self.term_detail)

    # ---- 首页词卡（JS 渲染 + SSR）：同样隐藏 ----

    def test_index_js_hides_inactive_rise(self):
        self.assertIn('Number(w.rise) > -0.999', self.index)

    def test_index_ssr_hides_inactive_rise(self):
        self.assertIn("t.rise > -0.999", self.index)

    # ---- _explain_fallback：-1.0 不写进解释文案 ----

    def test_explain_fallback_skips_inactive_rise(self):
        zh = app_module._explain_fallback("测试", "zh", 3, 100, -1.0)
        self.assertNotIn("-1.00", zh)
        self.assertNotIn("环比上升", zh)
        en = app_module._explain_fallback("Test", "en", 3, 100, -1.0)
        self.assertNotIn("-1.00", en)
        self.assertNotIn("Rise", en)

    def test_explain_fallback_keeps_real_decline(self):
        zh = app_module._explain_fallback("测试", "zh", 3, 100, -0.5)
        self.assertIn("环比上升 -0.50", zh)
        en = app_module._explain_fallback("Test", "en", 3, 100, -0.5)
        self.assertIn("Rise -0.50", en)


if __name__ == "__main__":
    unittest.main()
