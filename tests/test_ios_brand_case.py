"""iOS 品牌大小写修复回归（"Ios Shortcuts" → "iOS Shortcuts"）。

背景：少数派等中文报道标题无英文表面，LLM 抽出的英文词键（iOS Shortcuts）落库为
canonical `ios-shortcuts`；词卡展示无含大写表面可依时，`_display_of` 的 capitalize
美化兜底把 token "ios" 判成 "Ios"（"Ios Shortcuts"）。

修复三层：
1. `_OVERRIDES`（config/terms_canonical.json display_overrides 同步）：精确 canonical
   `ios`→"iOS"、`ios-shortcuts`→"iOS Shortcuts"，成为词典权威词（不被表面偶然大小写干扰）；
2. `_display_of` 美化兜底新增品牌混合大小写 token 表 `MIXED = {"ios": "iOS"}`——
   兜住 ios-* 家族其他组合（如 ios-18→iOS 18）；
3. 大小写表面仍优先：标题确含 "iOS Shortcuts" 时沿用表面形态。

覆盖：normalize 键稳定、_display_of 各形态、词典权威判定、refresh_words 真实路径
（模拟生产：中文标题 + canonical 关键词、无英文表面）出词卡 term == "iOS Shortcuts"。
全程零 LLM（无 key 降级），不设任何 API key、不发网络请求。
"""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest


class IosBrandCaseTests(unittest.TestCase):
    """用隔离临时库走真实 terms/news_store 路径，全程零 LLM（无 key 降级）。"""

    FIXED_TS = 1750000000

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-ioscase-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # LLM 纪律：必须留在零 token 降级路径。
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        cls.news_store = news_store
        cls.terms = terms

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def setUp(self):
        conn = sqlite3.connect(self.db_path)
        for t in ("news_cards", "terms", "term_snapshots"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.close()

    # ---- 工具（与 test_dup_terms_board 同款，真实库路径）----

    def _insert_card(self, url, title, keywords, published="2026-09-04",
                     score=100):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, "
            "published, score, keywords) VALUES (?,?,?,?,?,?,?)",
            (url, title, title, title, published, score,
             json.dumps(keywords, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def _refresh(self):
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)

    def _word_cards(self):
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en", limit=200)
        return cards

    # ---- ① canonical 键稳定（大小写无关，不引入新分裂）----

    def test_normalize_term_ios_spellings(self):
        t = self.terms
        self.assertEqual(t.normalize_term("iOS Shortcuts"), "ios-shortcuts")
        self.assertEqual(t.normalize_term("iOS-Shorts"), "ios-shorts")
        self.assertEqual(t.normalize_term("iOS"), "ios")
        self.assertEqual(t.normalize_term("Ios Shortcuts"), "ios-shortcuts")

    # ---- ② _display_of 各形态 ----

    def test_display_of_ios_rules(self):
        d = self.terms._display_of
        # 无表面可依（canonical 自指 / 全小写表面）→ 规则渲染 iOS Shortcuts
        self.assertEqual(d("ios-shortcuts", ["ios-shortcuts"]),
                         "iOS Shortcuts")
        self.assertEqual(d("ios-shortcuts", ["ios shortcuts"]),
                         "iOS Shortcuts")
        self.assertEqual(d("ios", ["ios"]), "iOS")
        # 标题确含规范表面 → 表面优先（不破坏既有 display 表面路径）
        self.assertEqual(d("ios-shortcuts", ["iOS Shortcuts"]),
                         "iOS Shortcuts")
        # 家族兜底：ios-* 其他组合也按 iOS 渲染
        self.assertEqual(d("ios-18", ["ios-18"]), "iOS 18")
        # 非 ios 词不受影响（普通名词仍 capitalize）
        self.assertEqual(d("shortcuts-app", ["shortcuts-app"]),
                         "Shortcuts App")

    def test_ios_is_dictionary_governed(self):
        self.assertTrue(self.terms._is_dictionary_governed("ios-shortcuts"))
        self.assertTrue(self.terms._is_dictionary_governed("ios"))

    # ---- ③ 真实刷新路径：中文标题 + canonical 关键词（模拟生产现场）----

    def test_refresh_word_card_displays_ios_shortcuts(self):
        # 生产现场复刻：少数派中文文「三条快捷指令」→ keywords ["快捷指令", "ios-shortcuts"]
        self._insert_card(
            "https://sspai.example/post/114117",
            "开学季：三条快捷指令让学校生活轻松一点",
            ["快捷指令", "ios-shortcuts"], published="2026-09-04")
        self._refresh()
        cards = self._word_cards()
        by_id = {c["id"]: c for c in cards}
        self.assertIn("ios-shortcuts", by_id)
        self.assertEqual(by_id["ios-shortcuts"]["term"], "iOS Shortcuts")
        # 词条页/搜索同源（get_term_row display 一致）
        row = self.terms.get_term_row("ios-shortcuts")
        self.assertIsNotNone(row)
        self.assertEqual(row["display"], "iOS Shortcuts")
        # 二次刷新不回退（确定性）
        self._refresh()
        cards2 = self._word_cards()
        self.assertEqual(
            {c["id"]: c["term"] for c in cards2}.get("ios-shortcuts"),
            "iOS Shortcuts")


if __name__ == "__main__":
    unittest.main()
