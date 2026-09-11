"""HuggingFace 独立排序页 /hf 与 /api/hf 路由测试（无网络）。

测试在导入 Flask app 前禁用后台刷新线程；HF 数据全部 mock，
不触发 RSS/HF/arXiv/LLM 请求。
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# 工作区测试严禁使用真实 LLM key；删除环境变量只影响本测试进程。
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GLM_API_KEY", None)

if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(
        LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: None
    )

import tracker  # noqa: E402
import dims  # noqa: E402

tracker.start_background_refresher = lambda: None
dims.start_background_dims_refresher = lambda: None

import app as app_module  # noqa: E402


def make_model(name, trending=0, likes=0, downloads=0,
               pipeline="text-generation", tags=None):
    """构造一张统一 schema 的 model 卡（与 tracker.get_model_cards 输出一致）。"""
    return {
        "kind": "model",
        "id": f"org/{name}",
        "full_id": f"org/{name}",
        "term": name,
        "author": "org",
        "type": "模型",
        "official_url": f"https://huggingface.co/org/{name}",
        "official_label": f"HuggingFace · org/{name}",
        "trending_score": trending,
        "likes": likes,
        "downloads": downloads,
        "created_at": "2026-08-30",
        "tags": tags or [f"tag-{name.lower()}"],
        "pipeline_tag": pipeline,
        "community": [{"site": "GitHub", "url": "https://github.com/search"}],
        "papers": [],
        "meta": "",
    }


class HfPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config.update(TESTING=True)

    def setUp(self):
        self.client = app_module.app.test_client()
        self.patches = [
            patch.object(app_module.store, "list_slots", return_value=[]),
            patch.object(app_module.store, "record_pageview"),
            patch.object(app_module.store, "record_visit"),
            patch.object(app_module.store, "record_impression"),
            patch.object(app_module.store, "record_search_query"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    # ---- /hf HTML 页面 ----

    def test_hf_page_renders_models_rank_tags_and_links(self):
        models = [
            make_model("Beta", trending=20, likes=9, downloads=99),
            make_model("Alpha", trending=10, likes=3, downloads=50),
        ]
        with patch.object(app_module, "_hf_models_for",
                          return_value=(models, 1750000000)):
            r = self.client.get("/hf?sort=trending&lang=zh")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Beta", body)
        self.assertIn("Alpha", body)
        self.assertIn("tag-alpha", body)              # 标签渲染
        self.assertIn("文本生成", body)                 # pipeline_tag 主徽标（zh）
        self.assertIn("huggingface.co/org/Beta", body)  # 官方链接
        self.assertIn("github.com/search", body)        # 社区链接
        self.assertIn("更新于", body)                    # 更新时间文案

    def test_hf_page_all_sorts_return_200(self):
        for sort in ("trending", "likes", "downloads"):
            with patch.object(app_module, "_hf_models_for",
                              return_value=([make_model("A", likes=1)], 0)):
                r = self.client.get(f"/hf?sort={sort}")
            self.assertEqual(r.status_code, 200, sort)
            body = r.get_data(as_text=True)
            # 英文（裸 URL 主语言）：排序链接不带 lang；语言切换链接带 lang=zh
            self.assertIn(f'href="/hf?sort={sort}"', body)
            self.assertIn(f'href="/hf?sort={sort}&amp;lang=zh"', body)

    def test_hf_page_english_and_lang_toggle_keeps_sort(self):
        models = [make_model("Alpha", likes=3)]
        with patch.object(app_module, "_hf_models_for",
                          return_value=(models, 0)):
            r = self.client.get("/hf?sort=likes")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('<html lang="en">', body)
        self.assertIn("Downloads", body)                        # 英文排序按钮
        self.assertIn('href="/hf?sort=likes&amp;lang=zh"', body)  # 语言切换保留排序（& 被 HTML escape）

    def test_hf_page_empty_models_shows_state(self):
        with patch.object(app_module, "_hf_models_for",
                          return_value=([], 0)):
            r = self.client.get("/hf?sort=trending&lang=zh")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("暂无数据", body)

    # ---- /api/hf JSON ----

    def test_api_hf_sorts_by_requested_key(self):
        models = [
            make_model("Mid", trending=1, likes=5, downloads=50),
            make_model("Top", trending=3, likes=9, downloads=99),
            make_model("Low", trending=2, likes=1, downloads=10),
        ]
        with patch.object(app_module.tracker, "get_model_cards",
                          return_value=(models, 1750000000)):
            for sort, key in (("trending", "trending_score"),
                              ("likes", "likes"),
                              ("downloads", "downloads")):
                r = self.client.get(f"/api/hf?sort={sort}")
                data = r.get_json()
                self.assertEqual(r.status_code, 200, sort)
                self.assertTrue(data["ok"], sort)
                self.assertEqual(data["sort"], sort)
                self.assertEqual(data["count"], 3)
                values = [t[key] for t in data["terms"]]
                self.assertEqual(values, sorted(values, reverse=True), sort)
                # 模型卡包含标签 / pipeline_tag / 官方与社区链接等字段
                first = data["terms"][0]
                self.assertIn("pipeline_tag", first)
                self.assertIn("tags", first)
                self.assertIn("official_url", first)
                self.assertIn("community", first)
                self.assertIn("fetched_at", data)

    def test_api_hf_falls_back_to_get_terms_on_cold_start(self):
        with patch.object(app_module.tracker, "get_model_cards",
                          return_value=([], 0)), \
                patch.object(app_module.tracker, "get_terms",
                             return_value={"ok": True, "sort": "top",
                                           "fetched_at": 1750000000,
                                           "count": 1,
                                           "terms": [make_model(
                                               "Cold", likes=7)]}) as gt:
            r = self.client.get("/api/hf?sort=likes")
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["terms"][0]["term"], "Cold")
        gt.assert_called_once_with("top")

    def test_api_hf_invalid_sort_defaults_to_trending(self):
        with patch.object(app_module.tracker, "get_model_cards",
                          return_value=([make_model("A", trending=2)], 0)):
            r = self.client.get("/api/hf?sort=bogus")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["sort"], "trending")

    # ---- 首页入口链接 ----

    def test_index_page_links_to_hf_page(self):
        source = (Path(app_module.__file__).with_name("templates")
                  .joinpath("index.html").read_text(encoding="utf-8"))
        # 2026-09-11：英文 = 裸 URL 主语言，SSR 入口链接按语言取 lang_qs，
        # JS updateHfLink() 同步为 "/hf"（en）/ "/hf?lang=zh"（zh）
        self.assertIn('href="/hf{{ lang_qs }}"', source)
        self.assertIn('LANG === "en" ? "/hf" : "/hf?lang=zh"', source)

    def test_index_hf_entry_lives_in_view_seg_not_header(self):
        """2026-09-05 需求：HF 入口从 header 小按钮迁到视图 seg 第三项（改名「开源」）。"""
        source = (Path(app_module.__file__).with_name("templates")
                  .joinpath("index.html").read_text(encoding="utf-8"))
        # seg 第三项：<a class="seg-link" id="hf-link">，↗ 角标暗示跨页跳转
        self.assertIn('class="seg-link" id="hf-link"', source)
        self.assertIn('seg-ext', source)
        self.assertIn('view_hf', source)  # i18n 文案 key
        # 2026-09-05 需求 2：导航文案为「开源 / Open Source」（板块语义，不用 HF 榜）
        self.assertIn('view_hf: "🤗 开源"', source)
        self.assertIn('view_hf: "🤗 Open Source"', source)
        # header 右上角不再保留独立 HF 按钮
        header = source.split('<div class="header-right">', 1)[1] \
                       .split('</div>', 1)[0]
        self.assertNotIn('hf-link', header)

    def test_hf_page_has_mirror_view_nav(self):
        """HF 页镜像三视图导航：热词/逐条新闻链回首页对应视图，「开源」板块入口 active。"""
        with patch.object(app_module, "_hf_models_for",
                          return_value=([make_model("A", likes=1)], 0)):
            r = self.client.get("/hf?sort=trending&lang=zh")
        body = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="view-nav"', body)
        self.assertIn('href="/?view=words&amp;lang=zh"', body)
        self.assertIn('href="/?view=news&amp;lang=zh"', body)
        self.assertIn('aria-current="page"', body)   # 「开源」板块入口为当前项
        self.assertIn("🤗 开源", body)                # 2026-09-05 需求 2 改名
        self.assertNotIn("返回首页", body)            # 旧单按钮已移除

    def test_hf_entry_click_event_type_is_whitelisted(self):
        self.assertIn("hf_entry_click", app_module.store._VALID_EVENT_TYPES)

    def test_community_links_language_split(self):
        """2026-09-05 需求：社区按钮名与渠道集按页面语言分流——
        中文页 知乎/B站/GitHub（国内平台回中文名），英文页
        YouTube/Reddit/X/GitHub（知乎无英文官方渠道，英文读者以
        YouTube/Reddit/X 为主流讨论渠道）。"""
        zh = app_module.tracker.community_links("Qwen3.8-27B", "zh")
        self.assertEqual([l["site"] for l in zh],
                         ["知乎", "B站", "GitHub"])
        self.assertIn("zhihu.com/search?q=Qwen", zh[0]["url"])
        self.assertIn("search.bilibili.com/all?keyword=Qwen", zh[1]["url"])
        self.assertIn("github.com/search?q=Qwen", zh[2]["url"])

        en = app_module.tracker.community_links("Qwen3.8-27B", "en")
        self.assertEqual([l["site"] for l in en],
                         ["YouTube", "Reddit", "X", "GitHub"])
        self.assertIn("youtube.com/results?search_query=Qwen", en[0]["url"])
        self.assertIn("reddit.com/search/?q=Qwen", en[1]["url"])
        self.assertIn("x.com/search?q=Qwen", en[2]["url"])
        self.assertIn("github.com/search?q=Qwen", en[3]["url"])

    def test_hf_page_community_links_follow_page_lang(self):
        """/hf 渲染：zh 页出 知乎/B站/GitHub 按钮，en 页出 YouTube/Reddit/X/GitHub，
        互不串台（_hf_models_for 读取时按 lang 投影，缓存共享）。"""
        with patch.object(app_module.tracker, "get_model_cards",
                          return_value=([make_model("Beta", likes=9)], 0)):
            zh = self.client.get("/hf?sort=trending&lang=zh").get_data(as_text=True)
            en = self.client.get("/hf?sort=trending").get_data(as_text=True)
        self.assertIn("💬 知乎", zh)
        self.assertIn("💬 B站", zh)
        self.assertIn("💬 GitHub", zh)
        self.assertNotIn("💬 YouTube", zh)
        self.assertNotIn("💬 Reddit", zh)
        self.assertNotIn("💬 X</a>", zh)
        self.assertIn("💬 YouTube", en)
        self.assertIn("💬 Reddit", en)
        self.assertIn("💬 X</a>", en)
        self.assertIn("💬 GitHub", en)
        self.assertNotIn("💬 知乎", en)
        self.assertNotIn("💬 B站", en)

    def test_no_llm_keys_are_used(self):
        self.assertFalse(os.environ.get("DEEPSEEK_API_KEY"))
        self.assertFalse(os.environ.get("GLM_API_KEY"))


if __name__ == "__main__":
    unittest.main()
