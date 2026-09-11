"""P1（2026-09-07）隐私政策页与页脚入口回归测试。

覆盖：
① GET /privacy → 200，中英双语正文都在（单页切换），canonical 固定裸 URL；
② GET /privacy-policy → 301 跳 /privacy（AdSense/审核方常见拼写）；
③ 首页（en/zh）SSR footer 同时含 Terms + Privacy 两个链接；
④ /terms、/hf 页 footer 也补了 Privacy 入口；
⑤ sitemap.xml 含 /privacy（单页双语裸 URL，与 /terms 同构）。
"""

import importlib
import types
import unittest
from unittest.mock import patch


class PrivacyPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "fcntl" not in __import__("sys").modules:
            fake_fcntl = types.ModuleType("fcntl")
            fake_fcntl.LOCK_EX = 0
            fake_fcntl.LOCK_NB = 0
            fake_fcntl.LOCK_UN = 0
            fake_fcntl.flock = lambda *args: None
            __import__("sys").modules["fcntl"] = fake_fcntl
        import dims
        import tracker
        # app.py 在 import 时启动后台预热线程——离线测试全部 patch 掉
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            cls.app_module = importlib.import_module("app")
        cls.client = cls.app_module.app.test_client()

    def test_privacy_page_bilingual_and_bare_canonical(self):
        base = self.app_module.config.BASE_URL
        with patch.object(self.app_module.config, "BASE_URL",
                          "https://aitrendwatch.top"):
            resp = self.client.get("/privacy")
        self.app_module.config.BASE_URL = base
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # 英文版默认可见 + 中文版隐藏待切换
        self.assertIn("Privacy Policy", body)
        self.assertIn("id=\"lang-en\"", body)
        self.assertIn("id=\"lang-zh\"", body)
        self.assertIn("Google Analytics", body)
        # 单页双语 → canonical 固定裸 URL（不挂 lang 参数；BASE_URL 已设时为绝对 URL）
        self.assertIn('<link rel="canonical" href="https://aitrendwatch.top/privacy">',
                      body)
        self.assertIn("隐私政策", body)          # 中文版正文在页面里（隐藏待切换）
        self.assertIn("与我们联系", body)
        # contact_email 配置与缺省都要渲染（占位文案 vs 邮箱）
        self.app_module.config.CONTACT_EMAIL = ""
        body2 = self.client.get("/privacy").get_data(as_text=True)
        self.assertIn("contact us", body2)
        self.app_module.config.CONTACT_EMAIL = "privacy@example.com"
        body3 = self.client.get("/privacy").get_data(as_text=True)
        self.assertIn("privacy@example.com", body3)
        self.app_module.config.CONTACT_EMAIL = ""

    def test_privacy_policy_alias_redirects(self):
        resp = self.client.get("/privacy-policy")
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp.headers.get("Location"), "/privacy")

    def test_home_footer_has_terms_and_privacy(self):
        base = self.app_module.config.BASE_URL
        with patch.object(self.app_module.config, "BASE_URL",
                          "https://aitrendwatch.top"):
            en = self.client.get("/").get_data(as_text=True)
            zh = self.client.get("/?lang=zh").get_data(as_text=True)
        self.app_module.config.BASE_URL = base
        # SSR 页脚 + JS i18n 文案都要带 Privacy 入口
        # 单页双语页 canonical 是裸 URL → 页脚链接不带 lang（带 lang 会命中 301）
        self.assertIn('href="/terms" class="footer-link">Terms of Service', en)
        self.assertIn('href="/privacy" class="footer-link">Privacy Policy', en)
        self.assertIn("footer_privacy", en)  # JS 动态重建页脚用同一 i18n key
        self.assertIn('href="/privacy" class="footer-link">隐私政策', zh)
        self.assertIn("隐私政策", zh)

    def test_terms_and_hf_pages_have_privacy_links(self):
        terms = self.client.get("/terms").get_data(as_text=True)
        self.assertIn('href="/privacy" class="footer-link">Privacy Policy', terms)
        hf = self.client.get("/hf").get_data(as_text=True)
        self.assertIn('href="/privacy" class="footer-link">Privacy Policy', hf)

    def test_sitemap_includes_privacy(self):
        base = self.app_module.config.BASE_URL
        with patch.object(self.app_module.config, "BASE_URL",
                          "https://aitrendwatch.top"):
            body = self.client.get("/sitemap.xml").get_data(as_text=True)
        self.app_module.config.BASE_URL = base
        self.assertIn("<loc>https://aitrendwatch.top/terms</loc>", body)
        self.assertIn("<loc>https://aitrendwatch.top/privacy</loc>", body)


if __name__ == "__main__":
    unittest.main()
