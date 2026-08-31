"""Tests for the dynamic LLM-maintained lexicon (word pool as dictionary).

Covers the explanation batch in refresh_words:
- new non-lexicon words get LLM-generated explanations written to terms table;
- static _EXPLANATIONS words are never sent to the explainer;
- existing explanations within 24h are not re-examined, older ones are;
- unchanged returned text keeps content but bumps explain_updated_at;
- no explainer (no LLM key) degrades gracefully;
- the classify prompt forbids generic/low-value keywords, and explain_terms
  carries value context + existing explanations.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


def _fcntl_stub():
    if "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8
        stub.flock = lambda *args: None
        sys.modules["fcntl"] = stub


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Env:
    """LLM 相关环境变量管理（fake key + 全 mock，零真实 token）。"""

    def __init__(self, **env):
        self._old = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            os.environ[k] = v

    def cleanup(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import config
        importlib.reload(config)


class DynamicLexiconTests(unittest.TestCase):
    """isolated temp DB + zero-token env; exercises real terms/app path."""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-dynlex-")
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

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        terms.init_db()
        cls.news_store = news_store
        cls.terms = terms

        _fcntl_stub()
        if "requests" not in sys.modules:
            requests_stub = types.ModuleType("requests")
            requests_stub.get = lambda *a, **k: None
            requests_stub.post = lambda *a, **k: None
            requests_stub.exceptions = types.SimpleNamespace(
                ChunkedEncodingError=Exception, ConnectionError=Exception,
                ReadTimeout=Exception, JSONDecodeError=Exception,
                HTTPError=Exception)
            sys.modules["requests"] = requests_stub
        import dims
        import tracker
        with patch.object(tracker, "start_background_refresher"), \
                patch.object(dims, "start_background_dims_refresher"):
            import app as app_module
            importlib.reload(app_module)
        cls.app = app_module

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
        self.app._detail_cache.clear()

    def _insert_card(self, url, title, keywords):
        self.news_store.upsert_cards([{
            "official_url": url, "title": title,
            "title_zh": title, "title_en": title,
            "published": "2026-08-31", "score": 100,
            "keywords": keywords,
        }])

    def _card(self, url, title, keywords):
        return {"official_url": url, "title": title,
                "title_zh": title, "title_en": title,
                "published": "2026-08-31", "score": 100,
                "keywords": keywords}

    def _term_row(self, canon):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute(
            "SELECT explain_zh, explain_en, explain_updated_at FROM terms "
            "WHERE term=?", (canon,)).fetchone()
        conn.close()
        return r

    def _insert_term_explain(self, canon, zh, en, updated_at):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "total_mentions, explain_zh, explain_en, explain_updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (canon, canon, "", "", "news", 1, zh, en, updated_at))
        conn.commit()
        conn.close()

    def test_new_word_gets_llm_explanation(self):
        self._insert_card("https://x.example/1", "ABot-Recon demo released",
                          ["abot-recon"])
        seen = []

        def explainer(contexts):
            seen.extend(c["canon"] for c in contexts)
            return {c["canon"]: {"zh": f"「{c['display']}」的中文解释。",
                                 "en": f"{c['display']} English explanation."}
                    for c in contexts}

        self.terms.refresh_words(
            [self._card("https://x.example/1", "ABot-Recon demo released",
                        ["abot-recon"])], [],
            fetched_at=1750000000, term_explainer=explainer)

        self.assertIn("abot-recon", seen)
        zh, en, ts = self._term_row("abot-recon")
        self.assertTrue(zh)
        self.assertTrue(en)
        self.assertTrue(ts)
        # tier-2 lookup returns the LLM explanation, not the template fallback
        # (display 由 _display_of 格式化，如 "abot-recon" → "Abot Recon")
        self.assertTrue(
            self.terms.get_term_explanation("abot-recon", "zh")
            .endswith("的中文解释。"))
        detail = self.app._word_detail("abot-recon", lang="zh")
        self.assertIn("的中文解释", detail["term"]["explain"])
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "")
        self.assertEqual(os.environ["GLM_API_KEY"], "")

    def test_static_lexicon_words_not_sent_to_explainer(self):
        self._insert_card("https://g.example/1", "GPT-5 release notes",
                          ["gpt-5"])
        seen = []

        def explainer(contexts):
            seen.extend(c["canon"] for c in contexts)
            return {}

        self.terms.refresh_words(
            [self._card("https://g.example/1", "GPT-5 release notes",
                        ["gpt-5"])], [],
            fetched_at=1750000000, term_explainer=explainer)
        self.assertNotIn("gpt-5", seen)
        # static explanation still wins
        self.assertTrue(self.terms.get_term_explanation("gpt-5", "zh"))

    def test_existing_explanation_within_24h_not_resent(self):
        self._insert_card("https://a.example/1", "ABot-Recon news",
                          ["abot-recon"])
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        self._insert_term_explain("abot-recon", "已有解释。", "Existing.",
                                  now)
        seen = []

        def explainer(contexts):
            seen.extend(c["canon"] for c in contexts)
            return {}

        self.terms.refresh_words(
            [self._card("https://a.example/1", "ABot-Recon news",
                        ["abot-recon"])], [],
            fetched_at=1750000000, term_explainer=explainer)
        self.assertNotIn("abot-recon", seen)

    def test_existing_explanation_older_than_24h_resent_and_kept_if_unchanged(self):
        self._insert_card("https://a.example/2", "ABot-Recon news 2",
                          ["abot-recon"])
        import datetime
        old = (datetime.datetime.now()
               - datetime.timedelta(hours=25)).isoformat(timespec="seconds")
        self._insert_term_explain("abot-recon", "旧解释。", "Old explain.",
                                  old)
        seen = []

        def explainer(contexts):
            seen.extend(c["canon"] for c in contexts)
            ctx = next(c for c in contexts if c["canon"] == "abot-recon")
            # LLM 判定已足够优：原样返回现有文本
            return {"abot-recon": {"zh": ctx["existing_zh"],
                                   "en": ctx["existing_en"]}}

        self.terms.refresh_words(
            [self._card("https://a.example/2", "ABot-Recon news 2",
                        ["abot-recon"])], [],
            fetched_at=1750000000, term_explainer=explainer)
        self.assertIn("abot-recon", seen)
        zh, en, ts = self._term_row("abot-recon")
        self.assertEqual(zh, "旧解释。")          # 内容未变
        self.assertEqual(en, "Old explain.")
        self.assertTrue(ts)                        # 检查时间已刷新

    def test_explainer_improves_existing_explanation(self):
        self._insert_card("https://a.example/3", "ABot-Recon news 3",
                          ["abot-recon"])
        import datetime
        old = (datetime.datetime.now()
               - datetime.timedelta(hours=25)).isoformat(timespec="seconds")
        self._insert_term_explain("abot-recon", "旧解释。", "Old explain.",
                                  old)

        def explainer(contexts):
            return {"abot-recon": {"zh": "明显更好的新解释。",
                                   "en": "A clearly better explanation."}}

        self.terms.refresh_words(
            [self._card("https://a.example/3", "ABot-Recon news 3",
                        ["abot-recon"])], [],
            fetched_at=1750000000, term_explainer=explainer)
        zh, en, _ = self._term_row("abot-recon")
        self.assertEqual(zh, "明显更好的新解释。")
        self.assertEqual(en, "A clearly better explanation.")

    def test_no_explainer_degrades_gracefully(self):
        self._insert_card("https://n.example/1", "ABot-Recon news no-key",
                          ["abot-recon"])
        # 无 term_explainer（等价无 LLM key）：不抛异常、解释列保持空
        self.terms.refresh_words(
            [self._card("https://n.example/1", "ABot-Recon news no-key",
                        ["abot-recon"])], [],
            fetched_at=1750000000)
        zh, en, ts = self._term_row("abot-recon")
        self.assertEqual((zh, en, ts), ("", "", ""))
        self.assertEqual(self.terms.get_term_explanation("abot-recon", "zh"),
                         "")
        # 详情页仍有模板兜底解释
        detail = self.app._word_detail("abot-recon", lang="zh")
        self.assertTrue(detail["term"]["explain"])
        self.assertIn("近期 AI 热点词", detail["term"]["explain"])

    def test_explain_batch_capped_by_hot(self):
        # 超过上限的词：只发最热的 EXPLAIN_BATCH_MAX_WORDS 个（防刷新锁占用过长）
        old_cap = self.terms.EXPLAIN_BATCH_MAX_WORDS
        self.terms.EXPLAIN_BATCH_MAX_WORDS = 2
        try:
            for i in range(3):
                kw = f"capword{i}"
                self._insert_card(f"https://c.example/{i}",
                                  f"Cap word {i} news", [kw])
            # 三张卡 score 不同 → cur_hot 不同；hot 最高的两个应被选中
            seen = []

            def explainer(contexts):
                seen.extend(c["canon"] for c in contexts)
                return {}

            self.terms.refresh_words(
                [self._card(f"https://c.example/{i}", f"Cap word {i} news",
                            [f"capword{i}"]) for i in range(3)], [],
                fetched_at=1750000000, term_explainer=explainer)
            self.assertEqual(len(seen), 2)   # 只发上限 2 个
            self.assertTrue(set(seen) <= {"capword0", "capword1", "capword2"})
            # 本轮未发的词留在待解释集合（后续轮次回填），库里无解释
            for kw in seen:
                zh, _en, _ts = self._term_row(kw)
                self.assertEqual(zh, "")
        finally:
            self.terms.EXPLAIN_BATCH_MAX_WORDS = old_cap

    def test_explain_terms_consecutive_fail_fast(self):
        # 连续 EXPLAIN_CONSECUTIVE_FAIL_LIMIT 块失败 → 熔断停止剩余批次
        import dims as dims_mod
        old_limit = dims_mod.EXPLAIN_CONSECUTIVE_FAIL_LIMIT
        dims_mod.EXPLAIN_CONSECUTIVE_FAIL_LIMIT = 2
        try:
            calls = {"n": 0}
            contexts = [{"canon": f"w{i}", "display": f"W{i}",
                         "titles": [], "existing_zh": "", "existing_en": ""}
                        for i in range(36)]  # 3 块

            def fake_post(url, headers=None, json=None, timeout=None):
                calls["n"] += 1
                raise Exception("simulated 429")

            with patch.object(dims_mod, "_active_llm",
                              return_value=("glm-5.3-flash",
                                            "https://llm.example",
                                            "fake-key", 1)), \
                    patch.object(dims_mod.requests, "post",
                                 side_effect=fake_post):
                out = dims_mod.explain_terms(contexts)
            # 连续 2 块失败即停：只发 2 次 post，而不是 3 块
            self.assertEqual(calls["n"], 2)
            self.assertEqual(out, {})
        finally:
            dims_mod.EXPLAIN_CONSECUTIVE_FAIL_LIMIT = old_limit


class ExplainPromptTests(unittest.TestCase):
    """提示词契约（fake key + 全 mock，零真实 token）。"""

    @classmethod
    def setUpClass(cls):
        _fcntl_stub()
        cls._env = _Env(GLM_API_KEY="fake-glm-key", DEEPSEEK_API_KEY="")
        import config
        import dims
        importlib.reload(config)
        importlib.reload(dims)
        cls.dims = dims

    @classmethod
    def tearDownClass(cls):
        cls._env.cleanup()

    def test_keyword_prompt_forbids_generic_words(self):
        dims = self.dims
        dims._LLM_ACTIVE_IDX = 0
        dims._LLM_FAILS = 0
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["user"] = json["messages"][1]["content"]
            body = {"choices": [{"message": {
                "content": '[{"idx":0,"dimension":"模型与技术","title_zh":"标题",'
                           '"title_en":"Title","summary_zh":"摘要。",'
                           '"summary_en":"Summary.","keywords":[]}]'},
                "finish_reason": "stop"}]}
            return _FakeResp(body)

        with patch.object(dims.requests, "post", side_effect=fake_post):
            dims._llm_classify_batch(
                [{"title": "Anthropic releases Claude 5", "source": "S",
                  "lang": "en", "published": "2026-08-31"}])
        self.assertIn("高价值AI实体", captured["user"])
        self.assertIn("禁止抽取泛化词", captured["user"])
        self.assertIn("无检索价值的碎片词", captured["user"])

    def test_explain_terms_carries_value_context_and_existing(self):
        dims = self.dims
        captured = {}
        body = {"choices": [{"message": {
            "content": '{"abc": {"zh": "中文解释。", "en": "English explain."}}'},
            "finish_reason": "stop"}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["system"] = json["messages"][0]["content"]
            captured["user"] = json["messages"][1]["content"]
            return _FakeResp(body)

        with patch.object(dims.requests, "post", side_effect=fake_post):
            out = dims.explain_terms([{
                "canon": "abc", "display": "Abc",
                "titles": ["代表报道标题一"],
                "existing_zh": "旧解释。", "existing_en": "Old explain.",
            }])
        self.assertIn("为什么值得关注", captured["system"])
        self.assertIn("明显改进", captured["system"])
        self.assertIn("代表报道标题一", captured["user"])
        self.assertIn("旧解释。", captured["user"])
        self.assertEqual(out["abc"], {"zh": "中文解释。",
                                      "en": "English explain."})

    def test_explain_terms_empty_input_returns_empty(self):
        self.assertEqual(self.dims.explain_terms([]), {})


if __name__ == "__main__":
    unittest.main()
