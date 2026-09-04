"""需求 4：中文公司名英译优化——官方英文名词典优先 + 未知专名不拼音化。

覆盖四层：
1. ``terms._COMPANY_EN_GLOSSARY`` 常量 sanity：CJK 键、非空纯英文值、
   QA 点名案例必收、无冲突重复英文（同英文仅允许同公司别名对，如 智谱/智谱AI）；
2. ``refresh_words`` 5.6 段：glossary 词确定性写入 display_en 且**不进 LLM
   翻译批次**（可控 fake translator 只应看到非 glossary 中文词）；
3. 词典优先的确定性：存量拼音/自译脏 display_en 随刷新回归官方英文名；
   无 key 降级（无 term_translator）环境下 glossary 仍生效、词典外词不写；
   词典外中文公司词仍走 LLM 翻译兜底（规则下保留中文原词）；
4. dims 提示词规则文案（keywords / 热词翻译）：公司专名官方英文名优先、
   无官方名保留中文原词、禁拼音化/自造英文——零 key、零网络断言。

全测试不设任何 API key、不发任何网络请求（降级/确定性路径断言）。
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import types
import unittest


def _fcntl_stub():
    if "fcntl" not in sys.modules:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8
        stub.flock = lambda *args: None
        sys.modules["fcntl"] = stub


class CompanyEnGlossaryTests(unittest.TestCase):
    """isolated temp DB + zero-key env；走真实 terms 路径 + dims 提示词常量断言。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                        "DEEPSEEK_API_KEY", "GLM_API_KEY")
        }
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-cn-gloss-")
        cls.db_path = os.path.join(cls._tmp.name, "news.db")
        cls.cache_dir = os.path.join(cls._tmp.name, "cache")
        os.environ["DATA_DIR"] = cls._tmp.name
        os.environ["NEWS_DB_PATH"] = cls.db_path
        os.environ["CACHE_DIR"] = cls.cache_dir
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["GLM_API_KEY"] = ""

        _fcntl_stub()
        import config
        import news_store
        import terms

        importlib.reload(config)
        importlib.reload(news_store)
        importlib.reload(terms)
        terms.init_db()
        cls.news_store = news_store
        cls.terms = terms
        import dims  # 仅读取模块级提示词常量，不触发任何 LLM 调用
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
                "published": "2026-08-31", "score": 100,
                "keywords": keywords}

    def _display_en(self, canon):
        conn = sqlite3.connect(self.db_path)
        r = conn.execute("SELECT display_en FROM terms WHERE term=?",
                         (canon,)).fetchone()
        conn.close()
        return r[0] if r else None

    # ---- ① 常量 sanity ----

    def test_glossary_constant_sanity(self):
        g = self.terms._COMPANY_EN_GLOSSARY
        self.assertGreaterEqual(len(g), 30)
        for key, val in g.items():
            # 键是中文公司名（display 形态）；值是非空官方英文名（不含中文）
            self.assertTrue(any(ord(c) >= 128 for c in key), f"键非中文: {key}")
            self.assertTrue(val and val.strip(), f"空英文值: {key}")
            self.assertFalse(any(ord(c) >= 128 for c in val),
                             f"值含中文（应为官方英文名）: {key}={val}")
        # QA 点名案例必收
        self.assertEqual(g["创通联达"], "Thundercomm")
        self.assertEqual(g["中科创达"], "ThunderSoft")
        # 无冲突重复英文：同一官方名只允许出现在同公司别名键上（智谱/智谱AI）
        rev = {}
        for k, v in g.items():
            rev.setdefault(v, []).append(k)
        for val, keys in rev.items():
            if len(keys) > 1:
                self.assertEqual(set(keys), {"智谱", "智谱AI"},
                                 f"英文冲突: {val} -> {keys}")

    # ---- ② 5.6 段：glossary 词确定性写入 + 不进 LLM 翻译批次 ----

    def test_glossary_word_written_and_skips_translator(self):
        # 词典词（创通联达→Thundercomm）确定性写官方英文名且不进 LLM 批次；
        # 非词典中文词（债务融资）才走 term_translator 兜底。
        cards = [
            self._card("https://g.example/1", "创通联达发布端侧智能体方案",
                       ["创通联达"]),
            self._card("https://g.example/2", "债务融资新规出台", ["债务融资"]),
        ]
        self.news_store.upsert_cards(cards)
        seen = []

        def fake_translator(chinese_terms):
            seen.extend(chinese_terms)
            return {t: ("Debt Financing" if t == "债务融资"
                        else "Guessed Pinyin") for t in chinese_terms}

        self.terms.refresh_words(cards, [], fetched_at=1750000000,
                                 term_translator=fake_translator)
        self.assertEqual(self._display_en("创通联达"), "Thundercomm")
        self.assertEqual(self._display_en("债务融资"), "Debt Financing")
        # 词典词不进 LLM 翻译批次（fake translator 只看到非 glossary 词）
        self.assertEqual(seen, ["债务融资"])
        # EN 视图投影用官方英文名
        en_words, _ = self.terms.get_word_cards("hot", "en", limit=60)
        en_by = {w.get("id"): w.get("term_display") for w in en_words}
        self.assertEqual(en_by.get("创通联达"), "Thundercomm")

    # ---- ③ 词典优先的确定性（脏值回归 / 降级环境 / LLM 兜底） ----

    def test_glossary_heals_stale_pinyin_display_en(self):
        # 模拟存量脏值：早期 LLM 曾把 创通联达 译成拼音 "Chuangtong Lianda"。
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO terms (term, display, display_zh, display_en, origin, "
            "total_mentions) VALUES (?,?,?,?,?,?)",
            ("创通联达", "创通联达", "", "Chuangtong Lianda", "news", 1))
        conn.commit()
        conn.close()
        cards = [self._card("https://g.example/3", "创通联达完成新一轮融资",
                            ["创通联达"])]
        self.news_store.upsert_cards(cards)
        # 无 translator（等价无 LLM key 降级）：词典词仍确定性回归官方名
        self.terms.refresh_words(cards, [], fetched_at=1750000000)
        self.assertEqual(self._display_en("创通联达"), "Thundercomm")

    def test_degraded_no_translator_glossary_still_applies(self):
        # 无 key 降级环境：glossary 词典（确定性知识）依然写官方英文名；
        # 词典外中文词不写 display_en（EN 页回退中文，不产生拼音/自造英文）。
        cards = [
            self._card("https://g.example/4", "中科创达与高通深化合作",
                       ["中科创达"]),
            self._card("https://g.example/5", "云岭智驾获新一轮融资",
                       ["云岭智驾"]),
        ]
        self.news_store.upsert_cards(cards)
        self.terms.refresh_words(cards, [], fetched_at=1750000000)
        self.assertEqual(self._display_en("中科创达"), "ThunderSoft")
        self.assertEqual(self._display_en("云岭智驾"), "")

    def test_unknown_company_still_goes_through_translator_fallback(self):
        # 词典未收录的公司专名仍走 LLM 兜底翻译（提示词规则下应保留中文原词，
        # 不拼音化）——保证 glossary 只是「词典优先」，未覆盖词不丢翻译通道。
        cards = [self._card("https://g.example/6", "云岭智驾获新一轮融资",
                            ["云岭智驾"])]
        self.news_store.upsert_cards(cards)
        seen = []

        def fake_translator(chinese_terms):
            seen.extend(chinese_terms)
            return {t: t for t in chinese_terms}  # 模拟 LLM 按规则保留中文原词

        self.terms.refresh_words(cards, [], fetched_at=1750000000,
                                 term_translator=fake_translator)
        self.assertEqual(seen, ["云岭智驾"])
        self.assertEqual(self._display_en("云岭智驾"), "云岭智驾")

    # ---- ④ 辅助函数（display/display_zh 两种形态兜底） ----

    def test_company_glossary_en_helper(self):
        f = self.terms._company_glossary_en
        self.assertEqual(f("创通联达"), "Thundercomm")
        self.assertEqual(f("创通联达", "创通联达"), "Thundercomm")
        # display 未收录、display_zh 命中 → 兜底命中
        self.assertEqual(f("另一种展示名", "创通联达"), "Thundercomm")
        # 未收录 → ""
        self.assertEqual(f("云岭智驾"), "")
        self.assertEqual(f("债务融资"), "")
        # display 为空 → 无展示形态可映射，直接 ""（5.6 调用方仅对中文 display 词调用）
        self.assertEqual(f("", "创通联达"), "")
        self.assertEqual(f("", ""), "")
        self.assertEqual(f("", None), "")

    # ---- ⑤ dims 提示词规则（公司专名保留中文/禁拼音化，零 key 断言） ----

    def test_dims_keywords_rule_keeps_chinese_company_names(self):
        # _USER_PREFIX 是模块常量：直接断言规则文案（不触发任何 LLM 调用）。
        prefix = self.dims._USER_PREFIX
        self.assertIn("中文标题里的公司/机构/产品专名保持中文原词", prefix)
        self.assertIn("创通联达、中科创达", prefix)
        self.assertIn("仅当标题原文本身含官方英文名/英文拼写时才用英文", prefix)
        self.assertIn("严禁把中文专名拼音化或自译成英文关键词", prefix)

    def test_dims_translate_sys_msg_uses_official_names_no_pinyin(self):
        msg = self.dims._TRANSLATE_SYS_MSG
        self.assertIn("公司/机构/产品专名必须使用官方英文名", msg)
        self.assertIn("创通联达→Thundercomm", msg)
        self.assertIn("中科创达→ThunderSoft", msg)
        self.assertIn("没有官方英文名的中文专名保留中文原词", msg)
        self.assertIn("严禁拼音音译或自造英文", msg)
        self.assertIn("Qujing Tech", msg)  # QA 反例必须钉死在提示词里


if __name__ == "__main__":
    unittest.main()
