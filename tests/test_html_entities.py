import json
import html
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


# Keep module initialization (news_store/terms create their SQLite tables) in
# a disposable directory, and make the no-LLM path explicit for this suite.
_TEST_DIR = tempfile.mkdtemp(prefix="aitrendwatch-entities-")
os.environ["DATA_DIR"] = os.path.join(_TEST_DIR, "data")
os.environ["NEWS_DB_PATH"] = os.path.join(_TEST_DIR, "data", "news.db")
os.environ["CACHE_DIR"] = os.path.join(_TEST_DIR, "cache")
os.environ.pop("GLM_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)

# The application uses fcntl for its Linux multi-worker refresh lock.  The
# parser/projection tests do not exercise that lock, so provide a tiny import
# stub on Windows rather than making the test suite platform-dependent.
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    _fcntl_stub = types.ModuleType("fcntl")
    _fcntl_stub.LOCK_EX = 0
    _fcntl_stub.LOCK_NB = 0
    _fcntl_stub.LOCK_UN = 0
    _fcntl_stub.flock = lambda *_args: None
    sys.modules["fcntl"] = _fcntl_stub

# Keep this suite runnable in the minimal test image too.  The stub has no
# transport implementation, so it cannot accidentally turn a unit test into
# a network test.
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

import dims  # noqa: E402
import news_store  # noqa: E402
import terms  # noqa: E402
from text_utils import decode_html_entities, decode_url_entities  # noqa: E402


class HtmlEntityBoundaryTests(unittest.TestCase):
    def test_text_decoder_repairs_double_encoded_common_entities(self):
        self.assertEqual(
            decode_html_entities("OpenAI&amp;#8217;s executive exodus"),
            "OpenAI’s executive exodus",
        )
        self.assertEqual(decode_html_entities("R&amp;amp;D"), "R&D")
        self.assertEqual(decode_html_entities("&amp;apos; &amp;quot; &amp;mdash;"),
                         "' \" —")
        self.assertEqual(decode_html_entities("R&D and x%26amp%3B"),
                         "R&D and x%26amp%3B")
        self.assertEqual(decode_html_entities("literal &notanentity;"),
                         "literal &notanentity;")

    def test_url_decoder_only_removes_one_xml_layer(self):
        url = "https://example.test/story?a=1&amp;b=%26amp%3B"
        self.assertEqual(
            decode_url_entities(url),
            "https://example.test/story?a=1&b=%26amp%3B",
        )
        self.assertEqual(
            decode_url_entities("https://example.test/?q=&amp;amp;"),
            "https://example.test/?q=&amp;",
        )
        self.assertEqual(decode_url_entities("javascript&#58;alert(1)"), "")

    def test_rss_parser_decodes_title_and_url_at_ingest(self):
        xml = """
        <rss><channel><item>
          <title>OpenAI&amp;#8217;s executive exodus has one big winner &amp;amp; more</title>
          <link>https://example.test/story?id=1&amp;src=rss</link>
          <pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate>
        </item></channel></rss>
        """
        cards = dims._parse_rss(xml, {
            "name": "OpenAI", "region": "国际", "default_dim": "产品与应用",
            "lang": "en",
        })
        self.assertEqual(len(cards), 1)
        self.assertEqual(
            cards[0]["title"],
            "OpenAI’s executive exodus has one big winner & more",
        )
        self.assertEqual(cards[0]["url"],
                         "https://example.test/story?id=1&src=rss")

    def test_bilingual_projection_decodes_legacy_cache_fields(self):
        raw = {
            "title": "Original &amp;#8217; title",
            "title_zh": "OpenAI &amp;amp; 中文",
            "title_en": "OpenAI&amp;#8217;s executive exodus",
            "summary_zh": "中文摘要 &amp;mdash; 兼容旧缓存",
            "summary_en": "English summary &amp;amp; legacy cache",
            "official_url": "https://example.test/?a=1&amp;b=2",
            "source": "Feed &amp; source",
        }
        zh = dims._project_card(raw, "zh")
        en = dims._project_card(raw, "en")
        self.assertEqual(zh["title"], "OpenAI & 中文")
        self.assertEqual(zh["summary"], "中文摘要 — 兼容旧缓存")
        self.assertEqual(en["title"], "OpenAI’s executive exodus")
        self.assertEqual(en["summary"], "English summary & legacy cache")
        self.assertEqual(en["official_url"], "https://example.test/?a=1&b=2")
        self.assertEqual(raw["title_en"], "OpenAI&amp;#8217;s executive exodus")

    def test_no_key_fallback_normalizes_both_language_slots(self):
        items = [{
            "title": "OpenAI&amp;#8217;s executive exodus",
            "url": "https://example.test/story",
            "source": "OpenAI",
            "region": "国际",
            "published": "2026-08-26",
            "default_dim": "产品与应用",
            "lang": "en",
        }]
        with mock.patch.object(dims, "_llm_classify_batch",
                               side_effect=RuntimeError("no LLM in unit test")):
            dims.enrich_with_llm(items)
        self.assertEqual(items[0]["dimension"], "产品与应用")
        self.assertEqual(items[0]["title"], "OpenAI’s executive exodus")
        self.assertEqual(items[0]["title_zh"], items[0]["title_en"])
        self.assertEqual(items[0]["summary_zh"], "OpenAI’s executive exodus")
        self.assertEqual(items[0]["summary_en"], "OpenAI’s executive exodus")

    def test_historical_sqlite_rows_are_decoded_on_read(self):
        raw_url = "https://example.test/history?id=1&amp;src=rss"
        conn = news_store._conn()
        conn.execute(
            """INSERT INTO news_cards
               (url, title, title_zh, title_en, summary_zh, summary_en,
                dimension, source, region, published, score, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (raw_url, "OpenAI&amp;#8217;s old title",
             "OpenAI&amp;amp; 中文", "OpenAI&amp;#8217;s old title",
             "摘要 &amp;mdash; 旧库", "Summary &amp;amp; old DB",
             "产品与应用", "Feed &amp; source", "国际", "2026-08-26", 1),
        )
        conn.commit()
        conn.close()

        cards = news_store.list_history_cards(limit=10, days=None)
        card = next(c for c in cards if c["official_url"] ==
                    "https://example.test/history?id=1&src=rss")
        self.assertEqual(card["title"], "OpenAI’s old title")
        self.assertEqual(card["title_zh"], "OpenAI& 中文")
        self.assertEqual(card["summary_zh"], "摘要 — 旧库")
        self.assertEqual(card["summary_en"], "Summary & old DB")

    def test_legacy_words_cache_is_decoded_before_bilingual_projection(self):
        old_file = terms.WORDS_CACHE_FILE
        old_state = (terms._file_cache.copy(), terms._file_cache_loaded,
                     terms._file_cache_mtime)
        try:
            with tempfile.TemporaryDirectory() as cache_dir:
                terms.WORDS_CACHE_FILE = os.path.join(cache_dir, "words.json")
                with open(terms.WORDS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"words": {"fetched_at": 1, "data": {
                        "terms": [{
                            "id": "openai",
                            "term": "OpenAI",
                            "top_news": [{
                                "title_zh": "OpenAI &amp;amp; 中文",
                                "title_en": "OpenAI&amp;#8217;s news",
                                "official_url": "https://example.test/?a=1&amp;b=2",
                            }],
                        }],
                    }}}, f)
                terms._file_cache.clear()
                terms._file_cache_loaded = False
                terms._file_cache_mtime = 0
                cards, _ = terms.get_word_cards("hot", "zh", limit=1)
                self.assertEqual(cards[0]["top_news"][0]["title"],
                                 "OpenAI & 中文")
                self.assertEqual(cards[0]["top_news"][0]["official_url"],
                                 "https://example.test/?a=1&b=2")
                cards, _ = terms.get_word_cards("hot", "en", limit=1)
                self.assertEqual(cards[0]["top_news"][0]["title"],
                                 "OpenAI’s news")
        finally:
            terms.WORDS_CACHE_FILE = old_file
            terms._file_cache.clear()
            terms._file_cache.update(old_state[0])
            terms._file_cache_loaded = old_state[1]
            terms._file_cache_mtime = old_state[2]

    def test_decoded_text_remains_escaped_when_rendered(self):
        value = decode_html_entities("&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;")
        # Jinja's autoescape uses the same HTML escaping contract.  Keep this
        # test stdlib-only so the suite never needs an install or network.
        rendered = html.escape(value, quote=True)
        self.assertEqual(rendered, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotIn("<script>", rendered)


if __name__ == "__main__":
    unittest.main()
