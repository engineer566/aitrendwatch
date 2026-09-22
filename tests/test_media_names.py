"""媒体/出处名称排除（2026-09-12 需求）。

生产事故：Mit Technology Review / 少数派 高居热词榜前列——LLM 抽词输入行带
「| 源名」后缀、summary 引用出处，媒体名被抽成关键词进入词池。媒体名称不是
AI 趋势本身，出处位的媒体名没有检索价值。

规则：媒体名称不进词池/词-新闻关联，除非它出现在报道标题里（标题例外——
标题里的媒体名通常是报道主题本身，如「MIT Technology Review 发布年度 AI 报告」）。

覆盖点：
1. _MEDIA_NAMES 归一命中：任意大小写/拼写变体 → is_media_name True；
   公司/产品名（OpenAI）与真实概念同形词（latent space）不在表内；
2. filter_media_keywords（dims LLM 抽词回填收口）：出处名剔除、标题例外保留；
3. _keyword_canons / _news_row_canons：无标题上下文一律剔除，标题命中保留；
4. refresh_words 聚合：summary 出处词不入池；标题例外词入池；
   存量媒体词行（含快照）在未获标题例外时整行清除；
5. get_term_news：媒体名关键词只在标题命中时构成词-新闻关联；
6. dims._llm_apply_output：LLM 回填的出处名关键词被剔除（标题例外保留）；
   _USER_PREFIX 含媒体名禁抽规则文案。

全部走零 key 降级环境（不设 DEEPSEEK_API_KEY/GLM_API_KEY），零 token 消耗。
"""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest


class MediaNameTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises the real terms path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-media-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        import config
        import news_store
        import terms
        import dims

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        importlib.reload(dims)
        terms.init_db()
        news_store.init_db()
        cls.news_store = news_store
        cls.terms = terms
        cls.dims = dims

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

    def _card(self, url, title, keywords):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": "2026-09-12", "score": 100,
                "keywords": keywords}

    def _term_exists(self, canon):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT 1 FROM terms WHERE term=?", (canon,)).fetchone()
        conn.close()
        return r is not None

    # ---- 1. is_media_name 归一命中 ----

    def test_is_media_name_normalizes_any_spelling(self):
        t = self.terms
        for media in ("MIT Technology Review", "Mit Technology Review",
                      "mit-technology-review", "MIT TechReview",
                      "少数派", "sspai", "The Verge", "TechCrunch",
                      "wired", "量子位", "QbitAI", "机器之心"):
            self.assertTrue(t.is_media_name(media), media)
        for real in ("openai", "GPT-5", "快捷指令", "latent space",
                     "DeepMind", "", "ai"):
            self.assertFalse(t.is_media_name(real), real)

    # ---- 2. filter_media_keywords（dims 回填收口） ----

    def test_filter_media_keywords_drops_source_names(self):
        t = self.terms
        # 出处位：标题里没有媒体名 → 剔除
        self.assertEqual(
            t.filter_media_keywords(["少数派", "快捷指令"],
                                    "三条快捷指令玩法：效率翻倍"),
            ["快捷指令"])
        self.assertEqual(
            t.filter_media_keywords(["mit-technology-review", "gpt-5"],
                                    "OpenAI releases GPT-5"),
            ["gpt-5"])

    def test_filter_media_keywords_title_exception(self):
        t = self.terms
        # 标题例外：媒体名本身是标题主题 → 保留（保留原词形）
        self.assertEqual(
            t.filter_media_keywords(
                ["mit-technology-review"],
                "MIT Technology Review 发布 2026 AI 现状报告"),
            ["mit-technology-review"])
        self.assertEqual(
            t.filter_media_keywords(["少数派"], "少数派发布年度征文榜单"),
            ["少数派"])
        # 多标题上下文：任一命中即保留
        self.assertEqual(
            t.filter_media_keywords(["少数派"], "", "少数派征文", ""),
            ["少数派"])
        # 全空标题上下文 → 一律剔除
        self.assertEqual(t.filter_media_keywords(["少数派"], "", ""), [])

    # ---- 3. 聚合入口：_keyword_canons / _news_row_canons ----

    def test_keyword_canons_media_gate(self):
        t = self.terms
        # 无标题上下文（titles=None）：媒体名一律剔除
        self.assertEqual(t._keyword_canons('["少数派"]'), set())
        # 标题未命中 → 剔除；标题命中 → 保留
        self.assertEqual(
            t._keyword_canons('["少数派", "快捷指令"]', ["三条快捷指令玩法"]),
            {"快捷指令"})
        self.assertEqual(
            t._keyword_canons('["少数派"]', ["少数派发布年度征文榜单"]),
            {"少数派"})

    def test_news_row_canons_media_gate(self):
        t = self.terms
        row = {"title": "三条快捷指令玩法", "title_zh": "", "title_en": "",
               "keywords": '["少数派", "快捷指令"]'}
        self.assertEqual(t._news_row_canons(row), {"快捷指令"})
        row_hit = {"title": "少数派发布年度征文榜单", "title_zh": "", "title_en": "",
                   "keywords": '["少数派"]'}
        self.assertEqual(t._news_row_canons(row_hit), {"少数派"})

    # ---- 4. refresh_words 聚合 + 存量僵尸行清除 ----

    def test_refresh_words_excludes_source_name_keywords(self):
        t = self.terms
        self.news_store.upsert_cards([
            # sspai 源卡：keywords 带「少数派」（出处位），标题没有 → 不入池
            self._card("https://sspai.example/1", "三条快捷指令玩法",
                       ["少数派", "快捷指令"]),
            self._card("https://s.example/2", "OpenAI releases GPT-5",
                       ["gpt-5"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertFalse(self._term_exists("少数派"))
        self.assertTrue(self._term_exists("快捷指令"))
        self.assertTrue(self._term_exists("gpt-5"))

    def test_refresh_words_title_exception_enters_pool(self):
        t = self.terms
        self.news_store.upsert_cards([
            self._card("https://mit.example/1",
                       "MIT Technology Review 发布 2026 AI 现状报告",
                       ["mit-technology-review", "ai-report"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertTrue(self._term_exists("mit-technology-review"))

    def test_refresh_words_purges_stale_media_rows(self):
        t = self.terms
        # 复刻生产现场：存量媒体词行（历史轮次聚合进池）+ 快照
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, total_mentions, cur_hot) "
            "VALUES ('少数派', '少数派', 40, 500)")
        conn.execute(
            "INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt) "
            "VALUES ('少数派', '2026-09-11-13', 40, 12)")
        conn.commit()
        conn.close()
        # 当轮没有任何标题命中「少数派」的报道 → 整行清除（含快照）
        self.news_store.upsert_cards([
            self._card("https://sspai.example/2", "三条快捷指令玩法",
                       ["少数派"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertFalse(self._term_exists("少数派"))
        conn = sqlite3.connect(self.db_path)
        snaps = conn.execute(
            "SELECT 1 FROM term_snapshots WHERE term=?", ("少数派",)).fetchone()
        conn.close()
        self.assertIsNone(snaps)

    def test_refresh_words_keeps_title_qualified_media_row(self):
        t = self.terms
        # 已获标题例外的媒体词行保留，不被清除分支误删
        self.news_store.upsert_cards([
            self._card("https://mit.example/2",
                       "MIT Technology Review 发布 2026 AI 现状报告",
                       ["mit-technology-review"]),
        ])
        t.refresh_words([], [], fetched_at=1750000000)
        self.assertTrue(self._term_exists("mit-technology-review"))
        # 词卡榜单里也保留（标题例外语境）
        cards, _fetched = t.get_word_cards(sort="hot", lang="zh", limit=200)
        self.assertIn("mit-technology-review", {c["id"] for c in cards})

    # ---- 5. get_term_news 词-新闻关联同口径 ----

    def test_get_term_news_media_keyword_needs_title(self):
        t = self.terms
        self.news_store.upsert_cards([
            self._card("https://sspai.example/3", "三条快捷指令玩法",
                       ["少数派"]),
            self._card("https://sspai.example/4", "少数派发布年度征文榜单",
                       ["少数派"]),
        ])
        # 出处位（标题没有）→ 不构成关联；标题命中 → 关联
        news = t.get_term_news("少数派", limit=50)
        urls = [n["official_url"] for n in news]
        self.assertNotIn("https://sspai.example/3", urls)
        self.assertIn("https://sspai.example/4", urls)

    # ---- 6. dims LLM 回填过滤 + 提示词规则 ----

    def test_llm_apply_output_filters_media_keywords(self):
        d = self.dims
        it = {"title": "三条快捷指令玩法：效率翻倍", "lang": "zh",
              "source": "少数派", "default_dim": "产品与应用"}
        pby = {0: {"dimension": "产品与应用",
                   "title_zh": "三条快捷指令玩法：效率翻倍",
                   "title_en": "Three shortcut workflows that double efficiency",
                   "summary_zh": "三条实用快捷指令玩法汇总。",
                   "summary_en": "A roundup of three handy shortcuts.",
                   "keywords": ["少数派", "快捷指令"]}}
        d._llm_apply_output([it], pby)
        self.assertEqual(it["keywords"], ["快捷指令"])
        self.assertNotIn("_llm_fail", it)

    def test_llm_apply_output_media_title_exception(self):
        d = self.dims
        it = {"title": "少数派发布年度征文榜单", "lang": "zh",
              "source": "少数派", "default_dim": "产品与应用"}
        pby = {0: {"dimension": "产品与应用",
                   "title_zh": "少数派发布年度征文榜单",
                   "title_en": "Sspaishupai releases annual essay list",
                   "summary_zh": "少数派公布年度征文入选名单。",
                   "summary_en": "The annual essay winners were announced.",
                   "keywords": ["少数派"]}}
        d._llm_apply_output([it], pby)
        self.assertEqual(it["keywords"], ["少数派"])

    def test_user_prefix_contains_media_name_rule(self):
        # _USER_PREFIX 是模块常量：直接断言规则文案（不触发任何 LLM 调用）。
        prefix = self.dims._USER_PREFIX
        self.assertIn("媒体/出处名称不是热词", prefix)
        self.assertIn("MIT Technology Review", prefix)
        self.assertIn("少数派", prefix)
        self.assertIn("「|」后是报道来源名", prefix)


if __name__ == "__main__":
    unittest.main()
