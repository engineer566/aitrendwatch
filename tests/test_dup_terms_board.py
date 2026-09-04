"""需求 2：排行榜重复词条（同词同展示名两行）根因与修复回归。

背景：canonical 键 = normalize_term(词形)——空白/下划线→'-'、小写化。于是标题拼写
"Hugging Face"（带空格）→ canonical 'hugging-face'；拼写 "HuggingFace"（无空格）
→ canonical 'huggingface'；两个 canonical 的展示名都美化成 "Hugging Face"，
词池出现视觉重复的两行（如测试机 hugging-face news_cnt=18 / huggingface=21）。
"AIAgent" / "AI-Agent" / "AI Agent" 是同类自由孪生。

修复两层：
1. normalize_term：词典治理的紧凑孪生折叠——ASCII canonical 去 '-' 后的 compact
   若是受词典治理的 canonical（'huggingface' 在 _LEXICON），返回 compact
   （'hugging-face'→'huggingface'）；非治理词的紧凑孪生（ai-agent/aiagent）不动。
2. refresh_words：聚合层按「去 '-' 紧凑形式」分组归并，组内选代表键（治理 >
   旧词池已存在 > 本轮 mentions > 字典序），聚合/display/HF 全部并到代表键；
   旧 terms 表孪生行合并（first_seen 取最早）并删除残留物理行、迁移 term_snapshots，
   保证下一轮不再分裂。

覆盖：
① normalize_term('Hugging Face')==normalize_term('HuggingFace')=='huggingface'；
② 聚合后 hugging-face/huggingface 不产生两行（refresh_words 确定性验证）；
③ 展示名/榜单无重复；/term/hugging-face 与 /term/huggingface 解析到同一词条；
④ AI Agent 类自由孪生（ai-agent 与 aiagent）在词池层归并到单一词键；
⑤ 回归：gpt-5 与 gpt-5.5 仍不同词；gpt-5 的连字符拼写不受折叠影响。
"""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest


class DupTermsBoardTests(unittest.TestCase):
    """用隔离临时库走真实 terms/news_store 路径，全程零 LLM（无 key 降级）。"""

    FIXED_TS = 1750000000

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-dupterms-")
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

    def _insert_card(self, url, title, keywords, published="2026-08-29",
                     score=100):
        """直接写库模拟历史/旁路落库的 keywords（含 legacy 非 canonical 键）。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, "
            "published, score, keywords) VALUES (?,?,?,?,?,?,?)",
            (url, title, title, title, published, score,
             json.dumps(keywords, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def _insert_terms_row(self, term, display, first_seen_at="2026-08-25",
                          total_mentions=1):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, origin, "
            "first_seen_at, total_mentions) VALUES (?,?,?,?,?,?)",
            (term, display, "", "news", first_seen_at, total_mentions))
        conn.commit()
        conn.close()

    def _insert_snapshot(self, term, cycle, win7_cnt, news_cnt=0,
                         score_sum=0, signal_sum=0.0):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, "
            "score_sum, signal_sum) VALUES (?,?,?,?,?,?)",
            (term, cycle, news_cnt, win7_cnt, score_sum, signal_sum))
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
            "SELECT term, cycle, win7_cnt FROM term_snapshots "
            "WHERE term=? ORDER BY cycle", (term,)).fetchall()
        conn.close()
        return rows

    def _refresh(self):
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)

    def _word_cards(self):
        cards, _ = self.terms.get_word_cards(sort="hot", lang="en", limit=200)
        return cards

    # ---- ① normalize_term 折叠 ----

    def test_normalize_term_folds_hugging_face_twin_spellings(self):
        t = self.terms
        # 带空格 / 连字符 / 紧凑三种拼写 → 同一 canonical 键
        self.assertEqual(t.normalize_term("Hugging Face"), "huggingface")
        self.assertEqual(t.normalize_term("HuggingFace"), "huggingface")
        self.assertEqual(t.normalize_term("Hugging-Face"), "huggingface")
        self.assertEqual(t.normalize_term("hugging face"), "huggingface")
        # 首尾标点噪音同样折叠
        self.assertEqual(t.normalize_term("(Hugging Face)"), "huggingface")
        # 词典内其他治理词的分隔符孪生同样折叠（openclaw 词典收录 "open claw"）
        self.assertEqual(t.normalize_term("Open-Claw"), "openclaw")

    def test_normalize_term_keeps_free_twin_and_version_boundary(self):
        t = self.terms
        # 非治理词的紧凑孪生（ai-agent/aiagent）在 normalize 层保持分离，
        # 交由 refresh_words 词池层归并（本函数无状态、不知道词池）。
        self.assertEqual(t.normalize_term("AI Agent"), "ai-agent")
        self.assertEqual(t.normalize_term("AI-Agent"), "ai-agent")
        self.assertEqual(t.normalize_term("AIAgent"), "aiagent")
        self.assertNotEqual(t.normalize_term("AI Agent"),
                            t.normalize_term("AIAgent"))
        # 回归：gpt-5 ≠ gpt-5.5 ≠ gpt-50（版本感知边界不折叠）
        self.assertEqual(t.normalize_term("GPT-5"), "gpt-5")
        self.assertNotEqual(t.normalize_term("GPT-5"), t.normalize_term("GPT-5.5"))
        self.assertNotEqual(t.normalize_term("GPT-5"), t.normalize_term("GPT-50"))

    # ---- ②③ 聚合：hugging-face / huggingface 单行 + 榜单无重复 + 词条页同源 ----

    def test_refresh_merges_hugging_face_legacy_twin_rows(self):
        # 模拟测试机现场：历史卡 keywords 以两种 canonical/表面落库
        # （hugging-face 18 篇形态 + huggingface 21 篇形态）。
        for i in range(3):
            self._insert_card(f"https://hf.example/legacy-hyphen-{i}",
                              f"Hyphen report {i}: Hugging Face model milestone",
                              ["hugging-face"])
        for i in range(4):
            self._insert_card(f"https://hf.example/canon-{i}",
                              f"Canon report {i}: HuggingFace release day",
                              ["huggingface"])
        self._insert_card("https://hf.example/surface",
                          "Hugging Face launches leaderboard",
                          ["Hugging Face"])   # 词典抽词保留的原文表面
        # 旧 terms 表孪生行都存在（不同 first_seen 历史）；最早首见日
        # （2026-08-20）用一篇同日报道锚定，避免 first_seen 自愈覆盖。
        self._insert_card("https://hf.example/early",
                          "Hugging Face early days", ["hugging-face"],
                          published="2026-08-20")
        self._insert_terms_row("hugging-face", "Hugging Face",
                               first_seen_at="2026-08-20")
        self._insert_terms_row("huggingface", "Hugging Face",
                               first_seen_at="2026-08-25")
        # 孪生键快照（含同 cycle 需相加、仅孪生键持有的 cycle 需迁移）
        self._insert_snapshot("hugging-face", "2026-08-25-00", win7_cnt=5)
        self._insert_snapshot("huggingface", "2026-08-25-00", win7_cnt=3)
        self._insert_snapshot("hugging-face", "2026-08-10-00", win7_cnt=2)

        self._refresh()
        cards = self._word_cards()

        # ② 词池只出 huggingface 一行（news_cnt = 3+4+1+1 = 9），无 hugging-face 卡
        by_id = {c["id"]: c for c in cards}
        self.assertIn("huggingface", by_id)
        self.assertNotIn("hugging-face", by_id)
        hf_cards = [c for c in cards if c["id"] in ("huggingface", "hugging-face")]
        self.assertEqual(len(hf_cards), 1)
        self.assertEqual(hf_cards[0]["news_cnt"], 9)
        # ③ 榜单上只有一个 "Hugging Face" 展示名
        displays = [c.get("term") for c in cards if c.get("term")]
        self.assertEqual(displays.count("Hugging Face"), 1)
        # ③ /term/hugging-face 与 /term/huggingface 解析到同一词条
        row_h = self.terms.get_term_row("hugging-face")
        row_c = self.terms.get_term_row("huggingface")
        self.assertIsNotNone(row_h)
        self.assertEqual(row_h["term"], "huggingface")
        self.assertEqual(row_c["term"], "huggingface")
        self.assertEqual(row_c["display"], "Hugging Face")
        # 详情关联报道口径一致（get_term_news 内部同归一），且 legacy 连字符
        # 拼写卡（keywords '"hugging-face"'）能经孪生表面 LIKE 候选命中（非空）
        news_h = self.terms.get_term_news("hugging-face", limit=50)
        news_c = self.terms.get_term_news("huggingface", limit=50)
        self.assertEqual(len(news_h), len(news_c))
        self.assertEqual(len(news_c), 9)
        # first_seen 取孪生最早（2026-08-20 被 2026-08-20 报道锚定，不被自愈覆盖）
        self.assertEqual(row_c["first_seen_at"], "2026-08-20")

        # 物理残留行清理：terms 表只剩 huggingface；快照已迁移/相加
        self.assertEqual(self._db_terms(), ["huggingface"])
        snaps = {s[1]: s[2] for s in self._db_snapshots("huggingface")}
        self.assertEqual(snaps.get("2026-08-25-00"), 8)   # 5+3 相加
        self.assertEqual(snaps.get("2026-08-10-00"), 2)   # 孪生独有 cycle 迁移
        self.assertEqual(self._db_snapshots("hugging-face"), [])

        # 第二轮刷新（确定性）：仍单行，不复活孪生
        self._refresh()
        cards2 = self._word_cards()
        by_id2 = {c["id"]: c for c in cards2}
        self.assertIn("huggingface", by_id2)
        self.assertNotIn("hugging-face", by_id2)
        self.assertEqual(self._db_terms(), ["huggingface"])

    def test_refresh_single_spelling_round_does_not_resurrect_twin(self):
        # 本轮只有 huggingface 拼写的报道：legacy hugging-face 旧行折叠进
        # huggingface，无当轮报道也不分裂成两行。
        self._insert_terms_row("hugging-face", "Hugging Face",
                               first_seen_at="2026-08-28")
        self._insert_card("https://hf.example/new",
                          "HuggingFace new dataset", ["huggingface"],
                          published="2026-08-28")
        self._refresh()
        cards = self._word_cards()
        by_id = {c["id"]: c for c in cards}
        self.assertIn("huggingface", by_id)
        self.assertNotIn("hugging-face", by_id)
        self.assertEqual(self._db_terms(), ["huggingface"])
        # 详情页仍能解析 legacy 拼写（归一到 huggingface 词条）
        row = self.terms.get_term_row("hugging-face")
        self.assertIsNotNone(row)
        self.assertEqual(row["term"], "huggingface")

    # ---- ④ AI Agent 类自由孪生：词池层归并到单一词键 ----

    def test_free_twin_ai_agent_merged_at_pool_layer(self):
        # ai-agent（"AI Agent"/"AI-Agent" 拼写）与 aiagent（"AIAgent" 拼写）
        # normalize 不折叠（非治理词），词池层必须按紧凑形式归并。
        self._insert_card("https://ag.example/space", "AI Agent demos boom",
                          ["ai-agent"], published="2026-08-29")
        self._insert_card("https://ag.example/compact", "AIAgent demos boom",
                          ["aiagent"], published="2026-08-29")
        self._refresh()

        cards = self._word_cards()
        twin_cards = [c for c in cards
                      if c["id"] in ("ai-agent", "aiagent")]
        # 单一词键（双方均无治理/旧历史 → mentions 平手 → 字典序 'ai-agent'）
        self.assertEqual(len(twin_cards), 1)
        self.assertEqual(twin_cards[0]["id"], "ai-agent")
        self.assertEqual(twin_cards[0]["news_cnt"], 2)
        # 旧 terms 表不留孪生键
        self.assertEqual(self._db_terms(), ["ai-agent"])

        # 下一轮加入更多 aiagent 拼写报道：仍归并到既有代表键 ai-agent
        self._insert_card("https://ag.example/compact2",
                          "AIAgent roundup", ["aiagent"],
                          published="2026-08-30")
        self._refresh()
        cards2 = self._word_cards()
        twin2 = [c for c in cards2 if c["id"] in ("ai-agent", "aiagent")]
        self.assertEqual(len(twin2), 1)
        self.assertEqual(twin2[0]["id"], "ai-agent")
        self.assertEqual(twin2[0]["news_cnt"], 3)
        self.assertEqual(self._db_terms(), ["ai-agent"])

    # ---- ⑤ 回归：gpt-5 / gpt-5.5 保持不同词 ----

    def test_gpt5_and_gpt55_stay_distinct(self):
        self._insert_card("https://g.example/v5", "GPT-5 release",
                          ["gpt-5"], published="2026-08-29")
        self._insert_card("https://g.example/v55", "GPT-5.5 arrives",
                          ["gpt-5.5"], published="2026-08-29")
        self._refresh()
        cards = self._word_cards()
        ids = {c["id"] for c in cards}
        self.assertIn("gpt-5", ids)
        self.assertIn("gpt-5.5", ids)
        # 版本边界命中不串：gpt-5 的 news_cnt 不含 GPT-5.5 那篇
        by_id = {c["id"]: c for c in cards}
        self.assertEqual(by_id["gpt-5"]["news_cnt"], 1)
        self.assertEqual(by_id["gpt-5.5"]["news_cnt"], 1)
        # 词典治理词 gpt-5 折叠路径不误伤连字符 canonical 本身
        self.assertEqual(self.terms.normalize_term("gpt-5"), "gpt-5")


if __name__ == "__main__":
    unittest.main()
