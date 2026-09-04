"""需求 5 改进：词典外词展示名保留原文大小写（terms.py display 计算）。

覆盖：
1. 新词典外词（workbuddy）无当轮卡时，display 从 top news 标题大小写不敏感命中的
   原文片段修复："Workbuddy" 兜底 → "WorkBuddy"（表面来源② top 标题）。
2. 当轮卡 keywords 表面（来源①，如 LLM 抽词 "WorkBuddy"）优先于美化兜底——
   DB 标题全小写也能给出正确大小写。
3. 存量旧 display（"Workbuddy"）被原文大小写表面升级为 "WorkBuddy"。
4. 词典权威词（OpenAI/Hugging Face 等）不被标题表面偶然大小写污染（仍 "OpenAI" /
   "Hugging Face"）。
5. 整条全大写的标题党标题（"IROBOT LAUNCHES NEW ROBOT…"）中的全大写形态不入选
   display；但标题非整体全大写时的全大写缩写（中文标题 "GOAI复赛评审正式启动"、
   英文标题 "GOAI Announces…"）是真实写法，display 保留 "GOAI"。
6. _is_dictionary_governed 判定覆盖：词典词 True / 词典外词 False。
"""

import importlib
import os
import sqlite3
import tempfile
import unittest


class DisplaySurfaceCaseTests(unittest.TestCase):
    """用隔离临时库走真实 terms/news_store 路径，全程零 LLM（无 key 降级）。"""

    FIXED_TS = 1780000000

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-surf-case-")
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
        terms.init_db()
        news_store.init_db()
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

    def _card(self, url, title, keywords, published="2026-09-04", score=100):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": published, "score": score,
                "keywords": keywords}

    def _term_display(self, term):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT display FROM terms WHERE term=?",
                         (term,)).fetchone()
        conn.close()
        return r[0] if r else None

    # ---- 1. 新词 display 用 top 标题原文片段（来源②）----

    def test_new_word_display_matches_original_case_from_db_title(self):
        # keywords 落库 canonical（workbuddy），DB 标题含原文大小写 "WorkBuddy"；
        # all_cards 传空 → 聚合走 DB，display 大小写来源是 top 标题命中片段。
        self.news_store.upsert_cards([self._card(
            "https://wb.example/1",
            "Tencent WorkBuddy Co-branded Hardware Arrives! "
            "First Batch of Over 100 Partners Onboard",
            ["workbuddy", "tencent"])])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("workbuddy"), "WorkBuddy")

    # ---- 2. 当轮卡 keywords 表面（来源①）----

    def test_new_word_display_prefers_card_keyword_surface(self):
        # DB 标题全小写：top 标题命中只会给出全小写表面（不入选），
        # display 的 "WorkBuddy" 只能来自当轮卡 keywords 表面（来源①）。
        url = "https://wb.example/2"
        card = self._card(url, "tencent workbuddy co-branded hardware arrives",
                          ["WorkBuddy"])
        self.news_store.upsert_cards([card])
        self.terms.refresh_words([card], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("workbuddy"), "WorkBuddy")

    # ---- 3. 存量旧 display 升级 ----

    def test_legacy_pretty_display_upgraded_to_original_case(self):
        # 老词行 display 是 capitalize 美化兜底 "Workbuddy"（需求 5 上线前的形态）；
        # 本轮有当轮 WorkBuddy 报道 → display 升级为原文大小写。
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO terms (term, display, origin) "
                     "VALUES (?,?,?)", ("workbuddy", "Workbuddy", "news"))
        conn.commit()
        conn.close()
        self.news_store.upsert_cards([self._card(
            "https://wb.example/3",
            "Tencent WorkBuddy Co-branded Hardware Arrives! "
            "First Batch of Over 100 Partners Onboard",
            ["workbuddy"])])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("workbuddy"), "WorkBuddy")

    # ---- 4. 词典权威词不被表面污染 ----

    def test_dictionary_governed_word_keeps_lexicon_display(self):
        self.assertTrue(self.terms._is_dictionary_governed("openai"))
        self.assertFalse(self.terms._is_dictionary_governed("workbuddy"))
        # 当轮卡 keywords 全小写（模拟标题全小写），display 仍由词典决定。
        card = self._card("https://openai.example/1",
                          "openai announces new frontier model for developers",
                          ["openai"])
        self.news_store.upsert_cards([card])
        self.terms.refresh_words([card], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("openai"), "OpenAI")

    def test_huggingface_display_not_polluted_by_title_surface(self):
        # 标题里的 "Huggingface"（漏空格拼写）大小写表面合法，但词典权威词
        # display 恒为 "Hugging Face"，不被标题表面污染。
        card = self._card(
            "https://hf.example/1",
            "Huggingface partners with open source community on new dataset",
            ["Huggingface"])
        self.news_store.upsert_cards([card])
        self.terms.refresh_words([card], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("huggingface"), "Hugging Face")

    # ---- 5. 标题党整条全大写不入选；真实全大写缩写（GOAI）保留 ----

    def test_all_caps_headline_surface_not_used_as_display(self):
        # 整条标题全大写 "IROBOT LAUNCHES NEW ROBOT" 是标题党形态（标题无小写/无
        # CJK），其中的全大写 "IROBOT" 不得作为 display（回落美化兜底 Irobot）。
        self.news_store.upsert_cards([self._card(
            "https://irobot.example/1", "IROBOT LAUNCHES NEW ROBOT",
            ["irobot"])])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertIsNotNone(self._term_display("irobot"))
        self.assertNotIn(self._term_display("irobot"), ("IROBOT",))

    def test_all_caps_acronym_kept_when_title_has_lowercase_or_cjk(self):
        # GOAI 是赛事全大写缩写：中文标题 "GOAI复赛评审正式启动" 非整体全大写
        # （含 CJK）→ "GOAI" 是原文真实写法，display 必须保留，而不是美化 "Goai"。
        self.news_store.upsert_cards([self._card(
            "https://goai.example/1", "GOAI复赛评审正式启动", ["goai"])])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("goai"), "GOAI")

    def test_all_caps_acronym_kept_in_mixed_case_english_title(self):
        # 英文标题含小写字母（非整条全大写）时的全大写缩写同样可信：
        # "GOAI Announces Finalists…" → display "GOAI"。
        self.news_store.upsert_cards([self._card(
            "https://goai.example/2",
            "GOAI Announces Finalists for World AI Open Source Contest",
            ["goai"])])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("goai"), "GOAI")

    # ---- 6. _is_dictionary_governed 判定 ----

    def test_is_dictionary_governed_coverage(self):
        governed_true = ("gpt-5", "openai", "GLM", "deepseek",
                         "huggingface", "rag")
        governed_false = ("workbuddy", "irobot", "qwen3", "veo3")
        for canon in governed_true:
            self.assertTrue(self.terms._is_dictionary_governed(canon), canon)
        for canon in governed_false:
            self.assertFalse(self.terms._is_dictionary_governed(canon), canon)

    # ---- 7. 词典权威词存量脏 display 修正（SaaS→Saas / DevOps→Devops）----

    def test_dict_governed_legacy_dirty_display_fixed_by_lexicon(self):
        # 早期 pretty 兜底曾把 SaaS/DevOps 顶成 Saas/Devops 并落库存量；
        # 词典权威词 display 以词典规则（_UPPER_ACRONYMS）为准，随刷新修正。
        conn = sqlite3.connect(self.db_path)
        for term, dirty in (("SaaS", "Saas"), ("DevOps", "Devops")):
            conn.execute("INSERT INTO terms (term, display, origin) "
                         "VALUES (?,?,?)", (term, dirty, "news"))
        conn.commit()
        conn.close()
        self.news_store.upsert_cards([
            self._card("https://saas.example/1",
                       "SaaS Platform Raises Funding Round",
                       ["saas"]),
            self._card("https://devops.example/1",
                       "DevOps Pipeline Automation Trends",
                       ["devops"]),
        ])
        self.terms.refresh_words([], [], fetched_at=self.FIXED_TS)
        self.assertEqual(self._term_display("SaaS"), "SaaS")
        self.assertEqual(self._term_display("DevOps"), "DevOps")


if __name__ == "__main__":
    unittest.main()
