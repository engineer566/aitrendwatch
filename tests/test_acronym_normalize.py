"""技术缩写大小写归一：normalize_term 对常见缩写统一为大写 canonical。

覆盖：
1. 未收录缩写（GPU/UI/API/NLP/HF/CNN 等）任意大小写 → 大写 canonical。
2. 已收录缩写（GLM/LLM/RAG/MCP/RLHF/LoRA/MoE/AGI/CUDA/AMD/xAI）
   任意大小写 → 规范形式 canonical。
3. 非缩写词条不受影响（gpt-5/openai/claude 等仍为小写 canonical）。
4. _display_of 对大写 canonical 返回正确展示名。
5. is_stopword 对大写 canonical 正确判断（LLM 仍为停用词）。
6. _EXPLANATIONS 可用大写 canonical 查到解释。
7. extract_keywords_dict 抽出的缩写为大写 canonical。
"""

import importlib
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class AcronymNormalizeTests(unittest.TestCase):
    """隔离临时库，零 LLM（无 key 降级）。"""

    @classmethod
    def setUpClass(cls):
        cls._old_env = {k: os.environ.get(k)
                        for k in ("DATA_DIR", "NEWS_DB_PATH", "CACHE_DIR",
                                  "DEEPSEEK_API_KEY", "GLM_API_KEY")}
        cls._tmp = tempfile.TemporaryDirectory(prefix="aitw-acronym-")
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
        cls.terms = terms

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    # ---- 1. 未收录缩写 → 大写 ----
    def test_unregistered_acronyms_uppercase(self):
        t = self.terms
        # GPU
        self.assertEqual(t.normalize_term("gpu"), "GPU")
        self.assertEqual(t.normalize_term("Gpu"), "GPU")
        self.assertEqual(t.normalize_term("GPU"), "GPU")
        # UI
        self.assertEqual(t.normalize_term("ui"), "UI")
        self.assertEqual(t.normalize_term("Ui"), "UI")
        self.assertEqual(t.normalize_term("UI"), "UI")
        # API
        self.assertEqual(t.normalize_term("api"), "API")
        self.assertEqual(t.normalize_term("Api"), "API")
        # NLP
        self.assertEqual(t.normalize_term("nlp"), "NLP")
        self.assertEqual(t.normalize_term("Nlp"), "NLP")
        # HF
        self.assertEqual(t.normalize_term("hf"), "HF")
        # CNN / RNN / GAN
        self.assertEqual(t.normalize_term("cnn"), "CNN")
        self.assertEqual(t.normalize_term("rnn"), "RNN")
        self.assertEqual(t.normalize_term("gan"), "GAN")
        # TPU / NPU
        self.assertEqual(t.normalize_term("tpu"), "TPU")
        self.assertEqual(t.normalize_term("npu"), "NPU")

    # ---- 2. 已收录缩写 → 规范形式 ----
    def test_registered_acronyms_canonical(self):
        t = self.terms
        self.assertEqual(t.normalize_term("glm"), "GLM")
        self.assertEqual(t.normalize_term("Glm"), "GLM")
        self.assertEqual(t.normalize_term("GLM"), "GLM")
        self.assertEqual(t.normalize_term("llm"), "LLM")
        self.assertEqual(t.normalize_term("Llm"), "LLM")
        self.assertEqual(t.normalize_term("LLM"), "LLM")
        self.assertEqual(t.normalize_term("rag"), "RAG")
        self.assertEqual(t.normalize_term("mcp"), "MCP")
        self.assertEqual(t.normalize_term("rlhf"), "RLHF")
        self.assertEqual(t.normalize_term("lora"), "LoRA")
        self.assertEqual(t.normalize_term("moe"), "MoE")
        self.assertEqual(t.normalize_term("agi"), "AGI")
        self.assertEqual(t.normalize_term("cuda"), "CUDA")
        self.assertEqual(t.normalize_term("amd"), "AMD")
        self.assertEqual(t.normalize_term("xai"), "xAI")

    # ---- 3. 非缩写词条不受影响 ----
    def test_non_acronyms_unchanged(self):
        t = self.terms
        self.assertEqual(t.normalize_term("gpt-5"), "gpt-5")
        self.assertEqual(t.normalize_term("GPT-5"), "gpt-5")
        self.assertEqual(t.normalize_term("openai"), "openai")
        self.assertEqual(t.normalize_term("OpenAI"), "openai")
        self.assertEqual(t.normalize_term("claude"), "claude")
        self.assertEqual(t.normalize_term("deepseek"), "deepseek")
        self.assertEqual(t.normalize_term("agent"), "agent")

    # ---- 4. _display_of 对大写 canonical 返回正确展示名 ----
    def test_display_of_uppercase_acronyms(self):
        t = self.terms
        self.assertEqual(t._display_of("GPU", []), "GPU")
        self.assertEqual(t._display_of("UI", []), "UI")
        self.assertEqual(t._display_of("GLM", []), "GLM")
        self.assertEqual(t._display_of("LLM", []), "LLM")
        self.assertEqual(t._display_of("RAG", []), "RAG")
        self.assertEqual(t._display_of("LoRA", []), "LoRA")
        self.assertEqual(t._display_of("MoE", []), "MoE")
        self.assertEqual(t._display_of("xAI", []), "xAI")

    # ---- 5. is_stopword 对大写 canonical 正确判断 ----
    def test_stopword_with_uppercase(self):
        t = self.terms
        # LLM 是停用词
        self.assertTrue(t.is_stopword("LLM"))
        self.assertTrue(t.is_stopword("llm"))
        self.assertTrue(t.is_stopword("Llm"))
        # AI 是停用词
        self.assertTrue(t.is_stopword("AI"))
        self.assertTrue(t.is_stopword("ai"))
        # GPU 不是停用词
        self.assertFalse(t.is_stopword("GPU"))
        self.assertFalse(t.is_stopword("gpu"))
        # GLM 不是停用词
        self.assertFalse(t.is_stopword("GLM"))

    # ---- 6. _EXPLANATIONS 可用大写 canonical 查到解释 ----
    def test_explanations_uppercase_keys(self):
        t = self.terms
        self.assertIn("GLM", t._EXPLANATIONS)
        self.assertIn("LLM", t._EXPLANATIONS)
        self.assertIn("RAG", t._EXPLANATIONS)
        self.assertIn("MCP", t._EXPLANATIONS)
        self.assertIn("RLHF", t._EXPLANATIONS)
        self.assertIn("LoRA", t._EXPLANATIONS)
        self.assertIn("MoE", t._EXPLANATIONS)
        self.assertIn("AGI", t._EXPLANATIONS)
        self.assertIn("CUDA", t._EXPLANATIONS)
        self.assertIn("AMD", t._EXPLANATIONS)
        self.assertIn("xAI", t._EXPLANATIONS)
        # 解释内容非空
        self.assertTrue(t._EXPLANATIONS["GLM"]["zh"])
        self.assertTrue(t._EXPLANATIONS["LLM"]["en"])

    # ---- 7. extract_keywords_dict 抽出缩写为大写 canonical ----
    def test_extract_keywords_dict_acronyms(self):
        t = self.terms
        # GLM 在词典中，标题匹配应返回大写 canonical
        kws = t.extract_keywords_dict("GLM-5 model achieves new benchmark record")
        self.assertIn("GLM", kws)
        # LLM 是停用词，不应被抽出
        kws = t.extract_keywords_dict("New LLM training technique published")
        self.assertNotIn("LLM", kws)
        # RAG 在词典中且非停用词
        kws = t.extract_keywords_dict("RAG pipeline improves retrieval accuracy")
        self.assertIn("RAG", kws)


if __name__ == "__main__":
    unittest.main()
