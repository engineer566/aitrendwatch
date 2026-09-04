"""需求 1 回归测试：同一词条下相同的报道（&amp;/&、utm 孪生行 + 标点镜像标题）。

覆盖三个契约：
① 同一篇文章以 &amp; 与 &（或带/不带 utm_*）两种 url 入库 → 只留一行、
   计数不翻倍（含存量孪生行自愈）；
② 全角/半角标点差异的镜像标题在 get_term_news 与词卡 top_news 里去重
   （真实不同的标题不被误压）；
③ 逐条新闻流（dims.get_news_cards，/api/stream?view=news 数据源）里
   同标题镜像只出现一次（words 视图不受影响，stream_utils 未改动）。

全程无 LLM key，走词典匹配降级路径（title_zh==原标题、dimension==default_dim）。
"""

import datetime
import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

# ---- 模块级环境：在任何项目模块 import 之前指向本套件的临时目录 ----
_TEST_DIR = tempfile.mkdtemp(prefix="aitw-task1-dup-")
os.environ["DATA_DIR"] = os.path.join(_TEST_DIR, "data")
os.environ["NEWS_DB_PATH"] = os.path.join(_TEST_DIR, "data", "news.db")
os.environ["CACHE_DIR"] = os.path.join(_TEST_DIR, "cache")
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("GLM_API_KEY", None)

# Windows 开发环境没有 stdlib fcntl / 最小测试镜像可能没有 requests：
# 提供 import 期 stub（与其它套件一致），保证 dims 模块可加载且零网络。
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    _fcntl_stub = types.ModuleType("fcntl")
    _fcntl_stub.LOCK_EX = 2
    _fcntl_stub.LOCK_NB = 4
    _fcntl_stub.LOCK_UN = 8
    _fcntl_stub.flock = lambda *args: None
    sys.modules["fcntl"] = _fcntl_stub
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    _requests_stub = types.ModuleType("requests")
    _requests_stub.exceptions = types.SimpleNamespace(
        ChunkedEncodingError=Exception,
        ConnectionError=Exception,
        ReadTimeout=Exception,
        JSONDecodeError=Exception,
        HTTPError=Exception,
    )
    _requests_stub.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("network disabled in unit tests"))
    _requests_stub.post = _requests_stub.get
    sys.modules["requests"] = _requests_stub

from text_utils import normalize_url_key, normalized_title_key  # noqa: E402


class TitleUrlKeyTests(unittest.TestCase):
    """归一化函数契约（不依赖 DB）。"""

    def test_url_key_collapses_entity_utm_fragment_variants(self):
        base = "https://www.infoq.cn/article/x"
        self.assertEqual(
            normalize_url_key(base + "?utm_source=rss&amp;utm_medium=feed"),
            base)
        self.assertEqual(
            normalize_url_key(base + "?utm_medium=feed"), base)
        self.assertEqual(
            normalize_url_key(base + "?id=1&amp;b=2"),
            base + "?id=1&b=2")
        self.assertEqual(
            normalize_url_key(base + "#section"), base)
        self.assertEqual(
            normalize_url_key(base + "?utm_source=rss#top"), base)
        # utm 只去键不去值：普通参数原样保留、顺序不变
        self.assertEqual(
            normalize_url_key(base + "?a=1&utm_source=x&b=2"),
            base + "?a=1&b=2")
        # 全 utm → 无查询串
        self.assertEqual(
            normalize_url_key(base + "?utm_source=x&UTM_CAMPAIGN=y"), base)
        # 大小写不敏感（UTM_ 前缀也算跟踪参数）

    def test_url_key_leaves_pseudo_keys_untouched(self):
        # 标题兜底/测试自造键不是真实 URL：不做片段/utm 处理，避免误伤文本
        # （实体单层解码仍保留——与旧 _card_url 行为一致）。
        self.assertEqual(normalize_url_key("old-gpt5"), "old-gpt5")
        self.assertEqual(normalize_url_key("C# 语言学习"), "C# 语言学习")
        self.assertEqual(
            normalize_url_key("story?id=1&amp;b=2"), "story?id=1&b=2")
        self.assertEqual(
            normalize_url_key("story?a=1&utm_source=x#frag"),
            "story?a=1&utm_source=x#frag")

    def test_url_key_rejects_dangerous_scheme(self):
        self.assertEqual(normalize_url_key("javascript&#58;alert(1)"), "")

    def test_title_key_strips_punctuation_variants(self):
        key = normalized_title_key("AI Agent 生态周报：自主智能体新进展！")
        self.assertEqual(
            key, normalized_title_key("AI Agent 生态周报:自主智能体新进展."))
        self.assertEqual(key, "aiagent生态周报自主智能体新进展")
        # 全角/半角逗号、顿号、·、引号、括号都算写法差异（空白残差一并去除）
        self.assertEqual(
            normalized_title_key("「AI·Agent」，开源！"),
            normalized_title_key('(AI Agent) 开源!!'))
        self.assertEqual(
            normalized_title_key("AI·Agent，重塑软件工程：OpenAI 观点"),
            normalized_title_key("AI Agent, 重塑软件工程:OpenAI 观点"))
        # 大小写 + 连续空白仍归一
        self.assertEqual(
            normalized_title_key("  Meta   Just Open-Sourced  "),
            normalized_title_key("meta just open-sourced"))

    def test_title_key_keeps_semantic_chars_and_none_cases(self):
        # 连字符有语义（GPT-5 ≠ GPT5）：保留，不把两个真实标题误压
        self.assertNotEqual(normalized_title_key("GPT-5 发布"),
                            normalized_title_key("GPT5 发布"))
        # & 不是被剥离的标点（R&D 是单词的一部分）
        self.assertNotEqual(normalized_title_key("OpenAI R&D 报告"),
                            normalized_title_key("OpenAI RD 报告"))
        # 不同词汇的标题即使共享前缀也不合并
        self.assertNotEqual(normalized_title_key("Nvidia 发布新芯片！"),
                            normalized_title_key("AMD 发布新芯片！"))
        # 空/纯标点/纯空白 → None
        self.assertIsNone(normalized_title_key(None))
        self.assertIsNone(normalized_title_key(""))
        self.assertIsNone(normalized_title_key("   "))
        self.assertIsNone(normalized_title_key("！!？…"))


class UpsertDedupeTests(unittest.TestCase):
    """① 写库去重：&amp;/&、utm 孪生 url 只留一行 + 存量自愈。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        os.environ["DATA_DIR"] = os.path.join(_TEST_DIR, "data")
        os.environ["NEWS_DB_PATH"] = os.path.join(_TEST_DIR, "data", "news.db")
        os.environ["CACHE_DIR"] = os.path.join(_TEST_DIR, "cache")
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("GLM_API_KEY", None)
        import config
        import news_store
        import terms
        importlib.reload(config)
        importlib.reload(news_store)   # 底部 init_db() 在本套件库建表 + 自愈
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

    def setUp(self):
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        for table in ("news_cards", "terms", "term_snapshots"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    @staticmethod
    def _row(url, title, score=100, keywords=None, first_seen=None):
        today = datetime.date.today().isoformat()
        return {
            "url": url, "title": title, "title_zh": title, "title_en": title,
            "summary_zh": "", "summary_en": "",
            "dimension": "产品与应用", "source": "InfoQ", "region": "国际",
            "published": today, "score": score,
            "keywords": keywords if keywords is not None else [],
            "first_seen_at": first_seen or today,
        }

    def test_upsert_entity_and_utm_twin_urls_leave_one_row(self):
        # 同一次入库出现两种 url 形态（&amp; vs &、带/不带 utm）→ 只留一行
        title = "InfoQ 深度：AI Agent 落地实践"
        self.news_store.upsert_cards([
            self._row("https://www.infoq.cn/article/x?utm_source=rss&amp;utm_medium=feed",
                      title, score=100, keywords=["ai-agent"]),
            self._row("https://www.infoq.cn/article/x?utm_medium=feed",
                      title, score=60),
        ])
        self.assertEqual(self.news_store.count_history(), 1)
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        rows = conn.execute("SELECT url, keywords FROM news_cards").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "https://www.infoq.cn/article/x")
        # 数据更全者的 keywords 被保留（另一份为空也不丢）
        self.assertIn("ai-agent", rows[0][1])

    def test_reupsert_with_other_spelling_keeps_single_row(self):
        # 已入库 & 形态后，下一轮以 &amp; 形态再来 → 冲突同一行，不翻倍
        self.news_store.upsert_cards([self._row(
            "https://www.infoq.cn/article/y?utm_medium=feed",
            "GPT-5 发布会定档", score=90, keywords=["gpt-5"])])
        self.news_store.upsert_cards([self._row(
            "https://www.infoq.cn/article/y?utm_source=rss&amp;utm_medium=feed",
            "GPT-5 发布会定档", score=100, keywords=["gpt-5"])])
        self.assertEqual(self.news_store.count_history(), 1)

    def test_heal_merges_legacy_twin_rows_keeping_fullest(self):
        # 旧版本代码写出的物理孪生行：同归一 url 两行（&amp; 与 &、utm 变体）
        title = "OpenClaw 发布：开源 AI 智能体框架"
        today = datetime.date.today().isoformat()
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, published, "
            "score, keywords, first_seen_at, last_refresh_at, active) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            ("https://example.test/story?utm_source=rss&amp;utm_medium=feed",
             title, title, title, today, 10, '["openclaw"]', "2026-08-01", "2026-08-01"))
        conn.execute(
            "INSERT INTO news_cards (url, title, title_zh, title_en, published, "
            "score, keywords, first_seen_at, last_refresh_at, active) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            ("https://example.test/story?utm_medium=feed",
             title, title, title, today, 200, "[]", "2026-09-01", "2026-09-01"))
        conn.commit()
        conn.close()
        self.assertEqual(self.news_store.count_history(), 2)

        # 触发一次 upsert（任意一张新卡）→ 自愈合并孪生行
        self.news_store.upsert_cards([self._row(
            "https://unrelated.example/other", "无关新闻", score=1)])
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        rows = conn.execute(
            "SELECT url, keywords, first_seen_at FROM news_cards "
            "WHERE url LIKE '%example.test/story%'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "https://example.test/story")  # 归一键
        self.assertIn("openclaw", rows[0][1])   # 旧行 keywords 并入保留行
        self.assertEqual(rows[0][2], "2026-08-01")  # first_seen 取更早

    def test_upserted_twin_does_not_double_counts_after_refresh(self):
        # 计数不翻倍：入库后词聚合 mentions / 详情关联都只看到 1 篇
        title = "GPT-5 发布会定档"
        self.news_store.upsert_cards([
            self._row("https://www.infoq.cn/article/z?utm_source=rss&amp;utm_medium=feed",
                      title, score=100, keywords=["gpt-5"]),
            self._row("https://www.infoq.cn/article/z?utm_medium=feed",
                      title, score=80, keywords=["gpt-5"]),
        ])
        self.assertEqual(self.news_store.count_history(), 1)
        self.terms._refresh_words_inner([], [], fetched_at=1750000000)
        words, _ = self.terms.get_word_cards(sort="hot", lang="zh", limit=60)
        gpt = next(w for w in words if w.get("id") == "gpt-5")
        self.assertEqual(gpt["news_cnt"], 1)
        self.assertEqual(len(self.terms.get_term_news("GPT-5", limit=50)), 1)


class TitleMirrorDedupeTests(unittest.TestCase):
    """② 标点差异镜像标题在词条关联列表与词卡 top_news 里去重。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        os.environ["DATA_DIR"] = os.path.join(_TEST_DIR, "data")
        os.environ["NEWS_DB_PATH"] = os.path.join(_TEST_DIR, "data", "news.db")
        os.environ["CACHE_DIR"] = os.path.join(_TEST_DIR, "cache")
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("GLM_API_KEY", None)
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

    def setUp(self):
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        for table in ("news_cards", "terms", "term_snapshots"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def _insert(self, url, title_zh, title_en=None, score=100, keywords=None,
                published=None):
        published = published or datetime.date.today().isoformat()
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        conn.execute(
            """INSERT INTO news_cards
               (url, title, title_zh, title_en, summary_zh, summary_en,
                dimension, source, region, published, score, keywords, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (url, title_zh, title_zh, title_en or title_zh, "", "",
             "产品与应用", "镜源", "国际", published, score,
             keywords if keywords is not None else "[]"))
        conn.commit()
        conn.close()

    def test_get_term_news_dedupes_punctuation_mirrors(self):
        self._insert("https://a.example/1",
                     "GPT-5 发布会倒计时！", score=100, keywords='["gpt-5"]')
        # 全角感叹号 vs 半角感叹号 vs 末尾标点有无：同一篇的镜像标题
        self._insert("https://b.example/1",
                     "GPT-5 发布会倒计时!", score=90, keywords='["gpt-5"]')
        self._insert("https://c.example/1",
                     "GPT-5 发布会倒计时", score=80, keywords='["gpt-5"]')
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["official_url"], "https://a.example/1")

    def test_get_term_news_dedupes_fullwidth_mark_mirrors(self):
        # 全角逗号/顿号/· 与半角写法的镜像标题
        self._insert("https://a.example/2",
                     "AI·Agent，重塑软件工程：OpenAI 观点", score=100,
                     keywords='["ai-agent"]')
        self._insert("https://b.example/2",
                     "AI Agent, 重塑软件工程:OpenAI 观点", score=50,
                     keywords='["ai-agent"]')
        cards = self.terms.get_term_news("ai-agent", limit=50)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["official_url"], "https://a.example/2")

    def test_get_term_news_keeps_distinct_titles(self):
        # 真实不同的标题不被误压：词汇不同、连字符不同、'&' 单词不同
        self._insert("https://d.example/1", "GPT-5 发布会倒计时！",
                     keywords='["gpt-5"]')
        self._insert("https://d.example/2", "GPT-5 发布会倒计时地点公布",
                     keywords='["gpt-5"]')
        self._insert("https://d.example/3", "GPT5 发布会倒计时",
                     keywords='["gpt-5"]')          # 连字符语义保留
        cards = self.terms.get_term_news("GPT-5", limit=50)
        self.assertEqual(len(cards), 3)

    def test_top_news_dedupes_punctuation_mirrors(self):
        self._insert("https://e.example/1", "Meta 开源新模型！",
                     score=100, keywords='["meta"]')
        self._insert("https://e.example/2", "Meta 开源新模型!",
                     score=90, keywords='["meta"]')
        self._insert("https://e.example/3", "Meta 开源新模型·性能实测",
                     score=80, keywords='["meta"]')
        self.terms.refresh_words([], [], fetched_at=123)
        cards, _ = self.terms.get_word_cards(sort="hot", lang="zh", limit=60)
        meta = next(card for card in cards if card["id"] == "meta")
        top = meta["top_news"]
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["title"], "Meta 开源新模型！")
        self.assertNotEqual(top[0]["official_url"], "https://e.example/2")


class NewsStreamDedupeTests(unittest.TestCase):
    """③ 逐条新闻流（dims.get_news_cards 装配源）里同标题镜像只出现一次。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        os.environ["DATA_DIR"] = os.path.join(_TEST_DIR, "data")
        os.environ["NEWS_DB_PATH"] = os.path.join(_TEST_DIR, "data", "news.db")
        os.environ["CACHE_DIR"] = os.path.join(_TEST_DIR, "cache")
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("GLM_API_KEY", None)
        import config
        import news_store
        import terms
        import dims
        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        importlib.reload(dims)
        cls.news_store = news_store
        cls.dims = dims

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def setUp(self):
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        for table in ("news_cards", "terms", "term_snapshots"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def _insert(self, url, title_zh, score=100):
        today = datetime.date.today().isoformat()
        conn = sqlite3.connect(os.environ["NEWS_DB_PATH"])
        conn.execute(
            """INSERT INTO news_cards
               (url, title, title_zh, title_en, summary_zh, summary_en,
                dimension, source, region, published, score, keywords, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (url, title_zh, title_zh, title_zh, "", "",
             "产品与应用", "源", "国际", today, score, "[]"))
        conn.commit()
        conn.close()

    def test_news_stream_shows_mirror_once(self):
        # 两个不同 url 的同标题镜像（标点写法不同）+ 一篇无关报道
        self._insert("https://m.example/story?utm_source=rss&amp;utm_medium=feed",
                     "AI Agent 生态周报：自主智能体新进展！", score=300)
        self._insert("https://m2.example/story",
                     "AI Agent 生态周报：自主智能体新进展!", score=200)
        self._insert("https://m3.example/other",
                     "智能体工具链评测：MCP 服务器横向对比", score=100)
        with mock.patch.object(self.dims, "_file_cache_get",
                               return_value=(None, 0)):
            cards, _ = self.dims.get_news_cards("zh")
        self.assertEqual(len(cards), 2)
        titles = [c["title_zh"] for c in cards]
        self.assertEqual(len({t for t in titles}), len(titles))
        # 镜像（不同 url 同标题）只保留首条（score 高者 = m.example 那份）
        ids = {c["id"] for c in cards}
        self.assertIn("https://m.example/story", ids)
        self.assertNotIn("https://m2.example/story", ids)
        # 当轮为空时历史卡是唯一来源：全部来自历史库
        self.assertTrue(all(c.get("from_history") for c in cards))
        # 直接插库的历史行展示 url 保留原文（实体已解码）；去重身份用归一 id
        self.assertEqual(
            {c["id"] for c in cards},
            {"https://m.example/story", "https://m3.example/other"})

    def test_news_stream_url_twin_and_title_mirror_both_collapse(self):
        # 同 url 孪生（&amp;/&）+ 跨 url 标点镜像 → 全流只剩 2 篇
        self._insert("https://n.example/1?a=1&amp;b=2",
                     "OpenClaw 发布：开源智能体框架！", score=120)
        self._insert("https://n.example/1?a=1&b=2",
                     "OpenClaw 发布：开源智能体框架!", score=110)
        self._insert("https://n2.example/1",
                     "OpenClaw 发布：开源智能体框架！", score=100)
        self._insert("https://n3.example/1",
                     "MCP 协议迎来 1.0 正式版", score=90)
        with mock.patch.object(self.dims, "_file_cache_get",
                               return_value=(None, 0)):
            cards, _ = self.dims.get_news_cards("zh")
        self.assertEqual(len(cards), 2)
        self.assertEqual(
            {c["id"] for c in cards},
            {"https://n.example/1?a=1&b=2", "https://n3.example/1"})

    def test_words_view_is_unaffected_by_news_title_dedupe(self):
        # stream_utils.dedupe_cards（words 视图共用）仍是原 url/身份语义
        import stream_utils
        cards = [
            {"kind": "news", "id": "u1",
             "title_zh": "AI Agent 生态周报：自主智能体新进展！"},
            {"kind": "news", "id": "u2",
             "title_zh": "AI Agent 生态周报：自主智能体新进展!"},
        ]
        unique = stream_utils.dedupe_cards(cards)
        self.assertEqual(len(unique), 2)   # 视图层去重身份不变


if __name__ == "__main__":
    unittest.main()
