"""跨语言/同词异形词条归并回归（2026-09-10 生产事故：两个 Apple 词条）。

背景：生产 rise 榜同时出现 #1「苹果」（display_en=Apple）与 #37「apple」，
英文页渲染成两个 "Apple"。根因：`_LEXICON`/`_ALIAS` 只收了 apple-intelligence，
没收苹果公司本身——跨语言归并完全依赖词典人工收录（大小写、分隔符孪生有自动
机制，中英文互译没有）。中文卡抽「苹果」、英文卡抽 "Apple"，各成一词，
报道数/热度被摊薄。

修复：
1. `_LEXICON` 收录 `"apple": ["apple", "苹果"]`——normalize_term 把「苹果」
   折叠进 apple canonical，存量 terms 表 苹果 行经第 4 步同键合并 + 第 6 步
   「折叠残留行」清理（快照迁移、物理行删除）自愈；
2. `_LEXICON_DISPLAY["apple"]="Apple"` + `_EXPLANATIONS["apple"]`；
3. `_ALIAS` 手工别名把「可折叠iphone」（及分隔形态）归并到「折叠iphone」——
   同一产品概念的两种中文措辞（CJK 词无分隔符紧凑归并机制）。

覆盖：
① normalize_term：苹果/Apple/APPLE 同键 apple；可折叠iphone 各形态 → 折叠iphone；
② refresh_words 后词池只出 apple 单行（news_cnt 合并、display=Apple、
   display_zh=苹果），terms 表 苹果 残留行删除、快照迁移；
③ /term/苹果 与 /term/apple 解析到同一词条；zh 投影显示 苹果、en 投影显示 Apple；
④ 可折叠iphone/折叠iphone 合并为单行；
⑤ 回归：apple 与 apple-intelligence 仍是两个词（版本/产品边界不折叠）。
"""

import importlib
import json
import os
import sqlite3
import tempfile
import unittest


class CrossLanguageAliasTests(unittest.TestCase):
    """用隔离临时库走真实 terms/news_store 路径，全程零 LLM（无 key 降级）。"""

    FIXED_TS = 1789000000

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-xlang-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        # 必须留在零 token 降级路径（LLM 调用纪律）。
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

    # ---- 工具 ----

    def _insert_card(self, url, title, keywords, published="2026-09-09",
                     score=100):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, "
            "published, score, keywords) VALUES (?,?,?,?,?,?,?)",
            (url, title, title, title, published, score,
             json.dumps(keywords, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def _insert_terms_row(self, term, display, display_zh="",
                          display_en="", first_seen_at="2026-09-01",
                          total_mentions=1):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "first_seen_at, total_mentions) VALUES (?,?,?,?,?,?,?)",
            (term, display, display_zh, display_en, "news",
             first_seen_at, total_mentions))
        conn.commit()
        conn.close()

    def _insert_snapshot(self, term, cycle, win7_cnt):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, "
            "score_sum, signal_sum) VALUES (?,?,?,?,?,?)",
            (term, cycle, 0, win7_cnt, 0, 0.0))
        conn.commit()
        conn.close()

    def _db_terms(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT term FROM terms ORDER BY term").fetchall()
        conn.close()
        return [r[0] for r in rows]

    def _db_snapshots(self, term):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT cycle, win7_cnt FROM term_snapshots "
            "WHERE term=? ORDER BY cycle", (term,)).fetchall()
        conn.close()
        return rows

    def _refresh(self):
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)

    # ---- ① normalize_term 折叠 ----

    def test_normalize_term_folds_apple_zh_en(self):
        t = self.terms
        self.assertEqual(t.normalize_term("苹果"), "apple")
        self.assertEqual(t.normalize_term("Apple"), "apple")
        self.assertEqual(t.normalize_term("APPLE"), "apple")
        self.assertEqual(t.normalize_term("apple"), "apple")

    def test_normalize_term_folds_foldable_iphone_variants(self):
        t = self.terms
        self.assertEqual(t.normalize_term("可折叠iphone"), "折叠iphone")
        self.assertEqual(t.normalize_term("可折叠 iPhone"), "折叠iphone")
        self.assertEqual(t.normalize_term("可折叠-iPhone"), "折叠iphone")
        self.assertEqual(t.normalize_term("折叠 iPhone"), "折叠iphone")
        self.assertEqual(t.normalize_term("折叠iphone"), "折叠iphone")
        # 测试机全词池扫描发现的漏网孪生（折叠屏-iphone/折叠屏iphone）
        self.assertEqual(t.normalize_term("折叠屏iphone"), "折叠iphone")
        self.assertEqual(t.normalize_term("折叠屏 iPhone"), "折叠iphone")
        self.assertEqual(t.normalize_term("折叠屏-iPhone"), "折叠iphone")
        # 回归：通用概念词 折叠屏 不被误并入折叠iphone
        self.assertEqual(t.normalize_term("折叠屏"), "折叠屏")

    def test_normalize_term_keeps_apple_intelligence_distinct(self):
        t = self.terms
        # 回归：apple ≠ apple-intelligence（产品边界不折叠）
        self.assertEqual(t.normalize_term("苹果智能"), "apple-intelligence")
        self.assertEqual(t.normalize_term("Apple Intelligence"),
                         "apple-intelligence")
        self.assertNotEqual(t.normalize_term("苹果"),
                            t.normalize_term("苹果智能"))

    # ---- ②③ 苹果/apple 跨语言孪生：聚合单行 + 存量自愈 + 双语投影 ----

    def test_refresh_merges_apple_zh_en_twin_rows(self):
        # 模拟生产现场：中文卡抽「苹果」、英文卡抽 "Apple"，terms 表两行并存
        for i in range(2):
            self._insert_card(f"https://zh.example/apple-{i}",
                              f"苹果折叠 iPhone 快讯 {i}", ["苹果"])
        for i in range(3):
            self._insert_card(f"https://en.example/apple-{i}",
                              f"Apple foldable iPhone report {i}", ["Apple"])
        # 最早首见日（2026-08-28，苹果行）用同日报道锚定，避免 first_seen 自愈覆盖
        self._insert_card("https://zh.example/apple-early",
                          "苹果早期报道", ["苹果"], published="2026-08-28")
        self._insert_terms_row("苹果", "苹果", display_en="Apple",
                               first_seen_at="2026-08-28")
        self._insert_terms_row("apple", "Apple", first_seen_at="2026-08-31")
        self._insert_snapshot("苹果", "2026-09-09-13", win7_cnt=4)
        self._insert_snapshot("apple", "2026-09-09-13", win7_cnt=6)
        self._insert_snapshot("苹果", "2026-09-01-00", win7_cnt=2)

        self._refresh()

        # ② 词池只出 apple 一行（news_cnt = 2+3+1 = 6），无 苹果 卡
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en", limit=200)
        apple_cards = [c for c in cards if c["id"] in ("apple", "苹果")]
        self.assertEqual(len(apple_cards), 1)
        self.assertEqual(apple_cards[0]["id"], "apple")
        self.assertEqual(apple_cards[0]["news_cnt"], 6)
        # 英文页只有一个 "Apple" 展示名（事故原状是两行都是 Apple）
        en_displays = [c["term_display"] for c in cards]
        self.assertEqual(en_displays.count("Apple"), 1)

        # ③ 词条解析同源：/term/苹果 与 /term/apple 同一行
        row_zh = self.terms.get_term_row("苹果")
        row_en = self.terms.get_term_row("apple")
        self.assertIsNotNone(row_zh)
        self.assertEqual(row_zh["term"], "apple")
        self.assertEqual(row_en["term"], "apple")
        self.assertEqual(row_en["display"], "Apple")
        self.assertEqual(row_en["display_zh"], "苹果")
        # first_seen 取两键最早（2026-08-28）
        self.assertEqual(row_en["first_seen_at"], "2026-08-28")

        # 双语投影：zh 显示中文别名 苹果，en 显示 Apple
        cards_zh, _ = self.terms.get_word_cards(sort="hot", lang="zh",
                                                limit=200)
        zh_map = {c["id"]: c["term_display"] for c in cards_zh}
        self.assertEqual(zh_map.get("apple"), "苹果")
        en_map = {c["id"]: c["term_display"] for c in cards}
        self.assertEqual(en_map.get("apple"), "Apple")

        # 存量自愈：terms 表只剩 apple 物理行；快照迁移/同 cycle 相加
        self.assertEqual(self._db_terms(), ["apple"])
        snaps = {c: n for c, n in self._db_snapshots("apple")}
        self.assertEqual(snaps.get("2026-09-09-13"), 10)   # 4+6 相加
        self.assertEqual(snaps.get("2026-09-01-00"), 2)    # 苹果独有 cycle 迁移
        self.assertEqual(self._db_snapshots("苹果"), [])

        # 词条解释：静态词典命中（中英均有）
        self.assertTrue(self.terms.get_term_explanation("苹果", "zh"))
        self.assertTrue(self.terms.get_term_explanation("apple", "en"))

        # 第二轮刷新（确定性）：仍单行，不复活 苹果
        self._refresh()
        cards2, _ = self.terms.get_word_cards(sort="hot", lang="en", limit=200)
        apple_cards2 = [c for c in cards2 if c["id"] in ("apple", "苹果")]
        self.assertEqual(len(apple_cards2), 1)
        self.assertEqual(self._db_terms(), ["apple"])

    def test_apple_and_apple_intelligence_coexist(self):
        # ⑤ 回归：两词同池共存、互不吞并
        self._insert_card("https://en.example/apple-ev",
                          "Apple event recap", ["Apple"])
        self._insert_card("https://zh.example/apple-ai",
                          "苹果智能新功能上线", ["苹果智能"])
        self._refresh()
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en", limit=200)
        ids = {c["id"] for c in cards}
        self.assertIn("apple", ids)
        self.assertIn("apple-intelligence", ids)

    # ---- ④ 可折叠iphone → 折叠iphone 异形归并 ----

    def test_refresh_merges_foldable_iphone_variant_rows(self):
        self._insert_card("https://zh.example/fold-1",
                          "苹果第一台折叠 iPhone 发布", ["折叠iphone"])
        # 2026-09-08 报道锚定 可折叠iphone 旧行的最早首见日（否则 first_seen
        # 自愈机制会用最早报道回填）
        self._insert_card("https://zh.example/fold-2",
                          "可折叠 iPhone 上手体验", ["可折叠iphone"],
                          published="2026-09-08")
        self._insert_card("https://zh.example/fold-3",
                          "可折叠iPhone 价格分析", ["可折叠iPhone"])
        self._insert_card("https://zh.example/fold-4",
                          "折叠屏 iPhone 拆解报告", ["折叠屏iphone"])
        self._insert_terms_row("折叠iphone", "折叠iphone",
                               first_seen_at="2026-09-09")
        self._insert_terms_row("可折叠iphone", "可折叠iphone",
                               first_seen_at="2026-09-08")
        self._insert_terms_row("折叠屏-iphone", "折叠屏-iPhone",
                               first_seen_at="2026-09-09")
        self._insert_snapshot("可折叠iphone", "2026-09-09-13", win7_cnt=3)

        self._refresh()

        cards, _ = self.terms.get_word_cards(sort="hot", lang="zh", limit=200)
        fold_cards = [c for c in cards
                      if c["id"] in ("折叠iphone", "可折叠iphone",
                                     "折叠屏iphone", "折叠屏-iphone")]
        self.assertEqual(len(fold_cards), 1)
        self.assertEqual(fold_cards[0]["id"], "折叠iphone")
        self.assertEqual(fold_cards[0]["news_cnt"], 4)
        # 残留行删除 + 快照迁移 + first_seen 取最早
        self.assertEqual(self._db_terms(), ["折叠iphone"])
        self.assertEqual(self._db_snapshots("可折叠iphone"), [])
        self.assertEqual(self._db_snapshots("折叠屏-iphone"), [])
        row = self.terms.get_term_row("折叠屏 iPhone")
        self.assertIsNotNone(row)
        self.assertEqual(row["term"], "折叠iphone")
        row2 = self.terms.get_term_row("可折叠iphone")
        self.assertIsNotNone(row2)
        self.assertEqual(row2["term"], "折叠iphone")
        self.assertEqual(row2["first_seen_at"], "2026-09-08")
        self.assertEqual(row["first_seen_at"], "2026-09-08")


if __name__ == "__main__":
    unittest.main()
