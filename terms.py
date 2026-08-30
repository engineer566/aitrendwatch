"""
词粒度聚合层 —— 热词池归并 + 词维度三榜打分 + 周期快照 + 历史回填。

在 tracker.py（HF 模型）与 dims.py（新闻 pipeline）之上新增的一层：
- 热词来源：dims 新闻卡的 keywords（DeepSeek 抽取；无 key 降级为本模块词典匹配）
  + tracker HF 模型名，canonical 键碰撞归并成统一词池。
- 三榜口径（词维度）：
  - 热度 hot     = Σ 近 7 天关联报道 score + HF likes（score 已含时效衰减，不二次衰减）
  - 上升 rise    = 活动量环比增速 (m_cur - m_prev) / max(m_prev, 0.5)；新词冷启动 ln(1+m)
  - 最新 novelty = 新词/罕见词发现：fresh(first_seen) × rarity(total_mentions)，
                   不是按报道时间排序——词库里少见或首次出现的词优先。
- 周期快照 term_snapshots 支撑环比；news_cards.keywords 列支撑词-新闻关联。

设计原则（复刻 news_store.py）：
- 纯 stdlib（sqlite3），零新依赖。
- 任何失败都不阻塞服务：DB 缺失/不可写 → 降级，绝不抛异常。
- 词聚合/打分只在 dims 后台刷新锁内执行（refresh_words），请求路径只读
  cache/words.json + SQLite（秒回）。
"""

import os
import re
import sys
import json
import math
import sqlite3
import threading
import datetime

import config
from text_utils import decode_html_entities, decode_url_entities

try:
    import news_store  # 历史库读取（回填/聚合扫描）；失败自动降级
except Exception:
    news_store = None

_DB_OK = False
_db_lock = threading.Lock()
_now_iso = lambda: datetime.datetime.now().isoformat(timespec="seconds")

# ---------- 词池规模控制 ----------
MAX_NEW_TERMS_PER_CYCLE = 150   # 单轮最多新增的非词典词数（防 LLM 词爆炸）
WORD_CARDS_LIMIT = 200          # words.json 最多保留的词卡数（展示层再截 60）
HOT_WINDOW_DAYS = 7             # 热度聚合窗口（天）


def _word_card_identity(card):
    """返回词卡的稳定身份；同一 canonical 词只能出现在榜单一次。"""
    if not isinstance(card, dict):
        return ""
    return str(card.get("id") or card.get("term") or card.get("display") or "")


def _word_card_number(card, field):
    """读取排序数值，屏蔽缓存中偶发的空值/字符串/NaN。"""
    try:
        value = float(card.get(field, 0) or 0)
        return value if math.isfinite(value) else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _dedupe_word_cards(cards):
    """按词卡身份去重，保留缓存中首次出现的完整词卡。"""
    out = []
    seen = set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        identity = _word_card_identity(card)
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        out.append(card)
    return out


def _sort_word_cards(cards, key):
    """按榜单排序，先确定性 tie-break，再按榜单值倒序。"""
    # Python 的稳定排序让多级排序保持可预测：同榜单值时按 id 升序，
    # rise/new 再按 hot 作为次级排序。这样截断前后的顺序完全一致，
    # 前端只需保留服务端顺序，不会在 60 条展示上限后重新洗牌。
    cards.sort(key=lambda c: _word_card_identity(c))
    cards.sort(key=lambda c: _word_card_number(c, "hot"), reverse=True)
    cards.sort(key=lambda c: _word_card_number(c, key), reverse=True)
    return cards

# ---------- 文件缓存（words.json，模式复刻 dims.py）----------
WORDS_CACHE_FILE = os.path.join(config.CACHE_DIR, "words.json")
_file_cache = {}
_file_cache_lock = threading.Lock()
_file_cache_loaded = False
_file_cache_mtime = 0


def _load_file_cache(force=False):
    """加载 words.json 到内存；靠 mtime 感知其他 worker/线程的磁盘刷新。"""
    global _file_cache_loaded, _file_cache_mtime
    with _file_cache_lock:
        try:
            cur_mtime = os.path.getmtime(WORDS_CACHE_FILE)
        except OSError:
            cur_mtime = 0
        if _file_cache_loaded and not force and cur_mtime == _file_cache_mtime:
            return
        if cur_mtime:
            try:
                with open(WORDS_CACHE_FILE, "r", encoding="utf-8") as f:
                    _file_cache.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        _file_cache_loaded = True
        _file_cache_mtime = cur_mtime


def _save_file_cache():
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        tmp = WORDS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_file_cache, f, ensure_ascii=False)
        os.replace(tmp, WORDS_CACHE_FILE)
    except OSError:
        pass


def _file_cache_set(data, fetched_at):
    with _file_cache_lock:
        _file_cache["words"] = {"data": data, "fetched_at": fetched_at}
    _save_file_cache()
    global _file_cache_mtime
    try:
        with _file_cache_lock:
            _file_cache_mtime = os.path.getmtime(WORDS_CACHE_FILE)
    except OSError:
        pass


def _file_cache_get():
    _load_file_cache()
    with _file_cache_lock:
        ent = _file_cache.get("words")
        if ent:
            return ent.get("data"), ent.get("fetched_at", 0)
    return None, 0


# ---------- SQLite（news.db 内的词粒度表，与 news_store 同库不同表）----------
def init_db():
    """建 terms / term_snapshots 表 + WAL。失败置 _DB_OK=False，全部降级。"""
    global _DB_OK
    config.ensure_data_dir()
    try:
        conn = _conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS terms (
                term           TEXT PRIMARY KEY,  -- canonical 键（小写归一），如 "gpt-5"
                display        TEXT,              -- 最佳展示形（如 "GPT-5"）
                display_zh     TEXT DEFAULT '',   -- 中文别名（词典提供，可空）
                origin         TEXT DEFAULT 'news', -- news | hf | both
                first_seen_at  TEXT,              -- 首次进入词池（取关联报道最早 published 兜底）
                last_seen_at   TEXT,
                total_mentions INTEGER DEFAULT 0, -- 累计关联报道数（按 url 去重）
                hf_json        TEXT DEFAULT '',   -- HF 模型词快照 JSON
                cur_hot        INTEGER DEFAULT 0, -- 本周期热度（每轮重算）
                cur_rise       REAL DEFAULT 0,    -- 本周期环比增速
                cur_novelty    REAL DEFAULT 0     -- 本周期新奇度
            );
            CREATE INDEX IF NOT EXISTS idx_terms_hot ON terms(cur_hot DESC);
            CREATE TABLE IF NOT EXISTS term_snapshots (
                term       TEXT,
                cycle      TEXT,              -- "2026-08-28-13"（Asia/Shanghai 小时）
                news_cnt   INTEGER DEFAULT 0, -- 本周期关联报道数
                score_sum  INTEGER DEFAULT 0, -- 本周期 Σ score
                signal_sum REAL DEFAULT 0,    -- 本周期 Σ 社区信号
                PRIMARY KEY (term, cycle)
            );
        """)
        conn.commit()
        conn.close()
        _DB_OK = True
    except Exception:
        _DB_OK = False


def _conn():
    conn = sqlite3.connect(config.NEWS_DB_PATH, timeout=3.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 关键词词典（canonical → 表面形式列表）----------
# 词典是「归一锚点」而非唯一词源：LLM 抽出的新词不在词典里也收（canonical=自身
# 小写归一）。词典三大用途：① 无 LLM key 时的降级抽词；② 历史库零成本回填；
# ③ 常见异形归一并提供 display_zh。
# 表面形式约定：ASCII 小写（词边界匹配，大小写不敏感）；CJK 直接子串匹配。
_LEXICON = {
    # —— 头部模型/产品 ——
    "gpt-5":        ["gpt-5", "gpt5", "gpt 5"],
    "gpt-4o":       ["gpt-4o", "gpt4o", "gpt 4o"],
    "chatgpt":      ["chatgpt", "chat gpt"],
    "openai":       ["openai"],
    "claude":       ["claude"],
    "anthropic":    ["anthropic"],
    "gemini":       ["gemini", "双子星"],
    "google-deepmind": ["deepmind", "google deepmind"],
    "llama":        ["llama", "羊驼"],
    "qwen":         ["qwen", "通义千问", "千问"],
    "deepseek":     ["deepseek", "深度求索"],
    "kimi":         ["kimi", "月之暗面"],
    "doubao":       ["doubao", "豆包"],
    "wenxin":       ["wenxin", "文心一言", "文心"],
    "glm":          ["glm", "智谱", "chatglm", "智谱清言"],
    "hunyuan":      ["hunyuan", "混元"],
    "mistral":      ["mistral"],
    "grok":         ["grok"],
    "copilot":      ["copilot"],
    "sora":         ["sora"],
    "midjourney":   ["midjourney"],
    "stable-diffusion": ["stable diffusion", "stability ai"],
    "flux":         ["flux.1", "flux 1", "black forest labs"],
    "veo":          ["veo"],
    "suno":         ["suno"],
    "cursor":       ["cursor"],
    "devin":        ["devin"],
    "manus":        ["manus"],
    "perplexity":   ["perplexity"],
    "huggingface":  ["huggingface", "hugging face", "抱抱脸"],
    "ollama":       ["ollama"],
    "vllm":         ["vllm"],
    "nvidia":       ["nvidia", "英伟达"],
    "cuda":         ["cuda"],
    "amd":          ["amd"],
    "apple-intelligence": ["apple intelligence", "苹果智能"],
    "siri":         ["siri"],
    "meta-ai":      ["meta ai", "meta人工智能"],
    "xai":          ["xai"],
    "microsoft":    ["microsoft", "微软"],
    "google":       ["google", "谷歌"],
    "bytedance":    ["bytedance", "字节跳动", "字节"],
    "alibaba":      ["alibaba", "阿里"],
    "tencent":      ["tencent", "腾讯"],
    "baidu":        ["baidu", "百度"],
    "huawei":       ["huawei", "华为"],
    "tsinghua":     ["tsinghua", "清华"],
    # —— 技术概念 ——
    "llm":          ["llm", "llms", "大模型", "大语言模型"],
    "agent":        ["agent", "agents", "智能体", "ai agent"],
    "rag":          ["rag", "retrieval-augmented", "检索增强"],
    "mcp":          ["mcp", "model context protocol"],
    "multimodal":   ["multimodal", "多模态"],
    "diffusion":    ["diffusion", "扩散模型"],
    "transformer":  ["transformer"],
    "fine-tuning":  ["fine-tuning", "finetuning", "fine tuning", "微调"],
    "rlhf":         ["rlhf", "人类反馈强化学习"],
    "reinforcement-learning": ["reinforcement learning", "强化学习"],
    "reasoning":    ["reasoning", "推理模型", "思维链", "chain-of-thought", "cot"],
    "embedding":    ["embedding", "embeddings", "向量", "词向量"],
    "vector-db":    ["vector database", "向量数据库"],
    "prompt":       ["prompt engineering", "提示词", "提示工程"],
    "context-window": ["context window", "上下文窗口", "长上下文", "long context"],
    "kv-cache":     ["kv cache", "kv-cache"],
    "quantization": ["quantization", "量化"],
    "distillation": ["distillation", "蒸馏", "知识蒸馏"],
    "lora":         ["lora", "qlora"],
    "moe":          ["moe", "mixture of experts", "混合专家"],
    "benchmark":    ["benchmark", "benchmarks", "基准测试", "评测"],
    "agi":          ["agi", "通用人工智能"],
    "alignment":    ["alignment", "对齐", "价值对齐"],
    "hallucination": ["hallucination", "幻觉"],
    "token":        ["token", "tokens"],
    "inference":    ["inference", "推理加速", "推理优化"],
    "training":     ["pretraining", "pre-training", "预训练"],
    "open-source":  ["open source", "open-source", "开源"],
    "robotics":     ["robotics", "humanoid", "机器人", "具身智能", "人形机器人"],
    "autonomous-driving": ["autonomous driving", "self-driving", "自动驾驶", "智能驾驶"],
    "text-to-video": ["text-to-video", "text to video", "文生视频", "视频生成"],
    "text-to-image": ["text-to-image", "text to image", "文生图", "图像生成"],
    "voice":        ["voice ai", "语音", "语音合成", "tts"],
    "coding":       ["ai coding", "code generation", "代码生成", "ai 编程", "ai编程"],
    "search":       ["ai search", "ai 搜索", "ai搜索"],
    "wearable":     ["ai glasses", "智能眼镜"],
    "chip":         ["ai chip", "ai芯片", "芯片", "算力"],
    "regulation":   ["ai regulation", "ai act", "监管", "法案"],
    "safety":       ["ai safety", "ai 安全", "ai安全"],
    "copyright":    ["copyright", "版权", "侵权"],
    "funding":      ["funding", "融资", "ipo", "估值"],
    "workflow":     ["workflow", "工作流"],
    "edge-ai":      ["edge ai", "端侧", "端侧ai", "on-device"],
    "world-model":  ["world model", "世界模型"],
    "memory":       ["long-term memory", "记忆", "长期记忆"],
    "sandbox":      ["sandbox", "沙盒"],
}

# 由 _LEXICON 反查构建：表面形式（小写）→ canonical
_ALIAS = {}
for _canon, _forms in _LEXICON.items():
    for _f in _forms:
        _ALIAS.setdefault(_f.lower(), _canon)
# 少量手工别名（词典表面形式未覆盖的常见异形）
_ALIAS.update({
    "gpt5": "gpt-5", "gpt4o": "gpt-4o",
    "千问": "qwen", "通义": "qwen",
    "智谱ai": "glm", "智谱清言": "glm",
    "月之暗面": "kimi",
    "深度求索": "deepseek",
    "苹果智能": "apple-intelligence",
})

# ASCII 表面形式预编译词边界正则（复用 tracker.py 词边界模式）；
# CJK 表面形式走子串匹配，无需正则。
_ASCII_PATTERNS = {}
for _canon, _forms in _LEXICON.items():
    for _f in _forms:
        fl = _f.lower()
        if all(ord(c) < 128 for c in fl):
            # 词边界：前后不能是字母/数字；额外排除「版本后缀」误匹配——
            # "GPT-5.5" 不应命中 gpt-5（(?!\.\d) 挡掉 ".数字"），"GPT-5." 句点结尾仍匹配。
            _ASCII_PATTERNS[fl] = (
                re.compile(r"(?<![a-z0-9])" + re.escape(fl)
                           + r"(?!\.\d)(?![a-z0-9])", re.I),
                _canon,
            )


def normalize_term(s):
    """任意词形 → canonical 键。单点收口，抽词/查询/详情页都用它。

    规则：strip/lower → 空白与连字符归一为单 '-' → 查别名表 → 保守去复数
    （仅 ASCII 且长度>3）→ 长度<2 或纯数字丢弃（返回 ""）。
    """
    if not s:
        return ""
    t = re.sub(r"[\s_]+", "-", str(s).strip().lower())
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if not t:
        return ""
    if t in _ALIAS:
        return _ALIAS[t]
    # 保守去复数：仅纯 ASCII 词、长度>3、不以 ss 结尾
    if t.isascii() and len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        cand = t[:-1]
        if cand in _ALIAS:
            return _ALIAS[cand]
    if len(t) < 2 or t.isdigit():
        return ""
    return t


def extract_keywords_dict(title):
    """词典匹配抽词（零 LLM 成本）。无 API key 时的降级路径 + 历史回填用。

    对标题（可传多段拼接文本）做：ASCII 表面形式词边界匹配 + CJK 子串匹配，
    返回 canonical 词键列表，去重，上限 3 个。
    """
    if not title:
        return []
    text = str(title)
    hits = []
    # ASCII 词边界匹配
    for pat, canon in _ASCII_PATTERNS.values():
        if canon not in hits and pat.search(text):
            hits.append(canon)
    # CJK 子串匹配
    for canon, forms in _LEXICON.items():
        if canon in hits:
            continue
        for f in forms:
            if any(ord(c) >= 128 for c in f) and f in text:
                hits.append(canon)
                break
    return hits[:3]


def _term_surfaces(canon):
    """Return the known title/keyword spellings for a canonical term.

    ``news_cards.keywords`` is canonical for newly written rows, but rows
    created before the keywords migration (and some older LLM output) can
    contain a surface spelling such as ``GPT5`` or ``GPT 5``.  Keep the
    surface expansion in one place so title fallback and keyword matching
    use the same aliases.
    """
    surfaces = []
    seen = set()

    def _add(surface):
        surface = str(surface or "").strip()
        folded = surface.casefold()
        if surface and folded not in seen:
            seen.add(folded)
            surfaces.append(surface)

    for surface in [canon, *_LEXICON.get(canon, [])]:
        _add(surface)
    # Include hand-added aliases which are intentionally not all repeated in
    # the human-maintained lexicon surface list (for example 通义 → qwen).
    for alias, target in _ALIAS.items():
        if target == canon:
            _add(alias)
    # normalize_term turns spaces/underscores into hyphens.  The reversible
    # space spelling is useful for uncatalogued LLM terms as well.
    if "-" in canon:
        _add(canon.replace("-", " "))
    return surfaces


def _title_matches_term(text, surfaces):
    """Match a title against term surfaces without matching a later version.

    ASCII terms use the same word-boundary rule as dictionary extraction.
    In particular, ``gpt-5`` matches ``GPT-5``/``GPT5`` but not
    ``GPT-5.5`` or ``GPT50``.  CJK aliases are intentionally substring
    matches because they do not have word boundaries.
    """
    text = str(text or "")
    if not text:
        return False
    text_fold = text.casefold()
    for surface in surfaces:
        if not surface:
            continue
        if any(ord(c) >= 128 for c in surface):
            if surface.casefold() in text_fold:
                return True
            continue
        pat = re.compile(
            r"(?<![a-z0-9])" + re.escape(surface)
            + r"(?!\.\d)(?![a-z0-9])", re.I)
        if pat.search(text):
            return True
    return False


def _compile_surface_patterns(surfaces):
    """预编译表面匹配模式，供词聚合标题关联计数批量复用（避免逐卡逐词 re.compile）。"""
    pats = []
    for surface in surfaces:
        if not surface:
            continue
        if any(ord(c) >= 128 for c in surface):
            pats.append(("cjk", surface.casefold(), None))
        else:
            pats.append(("ascii", None,
                         re.compile(r"(?<![a-z0-9])" + re.escape(surface)
                                    + r"(?!\.\d)(?![a-z0-9])", re.I)))
    return pats


def _title_matches_patterns(titles, pats):
    """与 ``_title_matches_term`` 同口径：任一标题字段命中任一表面即 True。"""
    for t in titles:
        t = str(t or "")
        if not t:
            continue
        tf = t.casefold()
        for kind, cjk, pat in pats:
            if kind == "cjk":
                if cjk in tf:
                    return True
            elif pat.search(t):
                return True
    return False


def _keyword_canons(value):
    """Decode old/new ``news_cards.keywords`` values to canonical keys."""
    raw = value
    if isinstance(value, str):
        try:
            raw = json.loads(value or "[]")
        except (json.JSONDecodeError, ValueError):
            # A few hand-migrated databases used a comma-separated value
            # instead of the documented JSON array.  Reading it is cheap and
            # keeps the detail page useful; new writes still emit JSON.
            raw = re.split(r"[,|]", value)
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {canon for canon in (normalize_term(k) for k in raw) if canon}


def _news_row_canons(row):
    """Return canonical keywords, deriving them for legacy/empty rows.

    The explicit keyword list remains authoritative for current rows.  Empty
    or pre-migration rows get the same no-LLM dictionary extraction used by
    the fallback path, so historical cards contribute to both word counts
    and detail-page results consistently.
    """
    kws = _keyword_canons(row["keywords"])
    if kws:
        return kws
    text = " ".join(str(row[f] or "") for f in
                    ("title", "title_zh", "title_en"))
    return set(extract_keywords_dict(text))


def _display_of(term, surfaces):
    """从命中表面形式里挑展示名：优先含大写的最长形式，否则首字母大写化。"""
    best = ""
    for s in surfaces:
        if any(c.isupper() for c in s) and len(s) > len(best):
            best = s
    if best:
        return best
    # 词典 canonical 的常见美化：按 '-' 分词，已知缩写全大写
    UPPER = {"gpt", "llm", "rag", "mcp", "agi", "rlhf", "moe", "lora", "ai",
             "kv", "tts", "ipo", "cuda", "amd", "xai", "cot"}
    parts = term.split("-")
    pretty = " ".join(p.upper() if p in UPPER else p.capitalize() for p in parts)
    pretty = pretty.replace("Gpt ", "GPT-").replace("Gpt", "GPT")
    return pretty


def _display_zh_of(term):
    """词典里该词的第一个 CJK 表面形式作为中文别名，无则 ""。"""
    for f in _LEXICON.get(term, []):
        if any(ord(c) >= 128 for c in f):
            return f
    return ""


# ---------- 词聚合 + 三榜打分（仅 dims 后台刷新锁内调用）----------
def _match_hf_term(canon, display, card_titles_lower):
    """HF 模型词是否命中某新闻标题：词边界匹配归一化短名。"""
    if not display:
        return False
    d = display.lower()
    if all(ord(c) < 128 for c in d):
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(d)
                         + r"(?!\.\d)(?![a-z0-9])", re.I)
        return any(pat.search(t) for t in card_titles_lower)
    return any(d in t for t in card_titles_lower)


# 与 tracker.py:_base_model_key 同款的量化/变体后缀剥离（修改时需两边同步）。
# 让 HF 模型变体（GPT-5-Chat / Qwen3-27B-GGUF）与新闻关键词（gpt-5）归并到同一词。
_HF_SUFFIX_RE = re.compile(
    r"-(gguf|fp8|fp16|bf16|mlx|awq|gptq|int8|int4|uncensored"
    r"|obli?(terat)?ed|instruct|chat|base)(-.*)?$")


def _hf_canon(mc):
    """HF 模型卡 → canonical 词键：full_id 末段剥量化/变体后缀 → normalize_term。"""
    full_id = mc.get("full_id") or mc.get("id") or ""
    display = (mc.get("term") or "").strip()
    name = full_id.split("/")[-1].lower() if full_id else display.lower()
    name = _HF_SUFFIX_RE.sub("", name)
    return normalize_term(name or display)


def refresh_words(all_cards, model_cards, fetched_at=None):
    """一轮刷新的词聚合：关联 → 归并 → 打分 → 快照 → 写 words.json。

    输入：all_cards（dims 当轮全量新闻卡，含 keywords）、
          model_cards（tracker 当轮 HF 模型卡）。
    数据源：新闻关联以**历史库全量扫描**为准（跨周期累积 total_mentions /
    7 天热窗），当轮 all_cards 只用于本周期快照 news_cnt/score_sum。
    失败静默，绝不阻塞 dims 刷新主流程。
    """
    if not _DB_OK:
        return
    try:
        _refresh_words_inner(all_cards or [], model_cards or [],
                             fetched_at or int(datetime.datetime.now().timestamp()))
    except Exception:
        pass


def _refresh_words_inner(all_cards, model_cards, fetched_at):
    now = _now_iso()
    today = datetime.date.today()
    hot_cutoff = (today - datetime.timedelta(days=HOT_WINDOW_DAYS)).isoformat()
    cycle = datetime.datetime.now().strftime("%Y-%m-%d-%H")  # 容器 TZ=Asia/Shanghai

    # ---- 1. HF 模型词归一化 + 元数据（底模键归并变体，与新闻关键词碰撞合并）----
    hf_terms = {}  # canon → {display, hf_meta}
    for mc in model_cards:
        canon = _hf_canon(mc)
        if not canon:
            continue
        display = (mc.get("term") or "").strip()
        # 同底模多变体：保留 trending_score 最高的展示名与元数据
        prev = hf_terms.get(canon)
        if prev and (prev["hf"].get("trending_score", 0)
                     >= int(mc.get("trending_score", 0) or 0)):
            continue
        hf_terms[canon] = {
            "display": display or canon,
            "hf": {
                "full_id": mc.get("full_id") or mc.get("id") or "",
                "likes": int(mc.get("likes", 0) or 0),
                "trending_score": int(mc.get("trending_score", 0) or 0),
                "downloads": int(mc.get("downloads", 0) or 0),
                "official_url": mc.get("official_url", ""),
                "author": mc.get("author", ""),
                "tags": mc.get("tags") or [],
            },
        }

    # ---- 2. 全量历史库扫描：词 → 关联聚合（total_mentions / 7 天热窗 / dims / top news）----
    agg = {}  # canon → {mentions, hot_score, urls:set, dims:Counter, top:[...], latest_pub, earliest_pub, pubs:set, cur_cnt, cur_score, cur_signal}
    cur_urls = {c.get("official_url") or c.get("title", "") for c in all_cards}
    cur_signal_by_url = {}
    for c in all_cards:
        u = c.get("official_url") or c.get("title", "")
        cur_signal_by_url[u] = (c.get("hn_points", 0) or 0) * 10 + \
            (c.get("reddit_score", 0) or 0) + (c.get("reddit_comments", 0) or 0) * 0.5

    # 全量历史库扫描用流式游标（for r in cur 逐行取，不用 fetchall 物化整表）。
    # news_cards 随每轮刷新逐轮累积（只增不减），fetchall 会把整表压进内存，
    # 是词聚合阶段刷新峰值内存的最大单项——改成单趟流式扫描，且 top news 与
    # 聚合在同一趟完成（原实现要 fetchall 后扫两遍）。
    if news_store:
        try:
            conn = _conn()
            news_columns = {r[1] for r in
                            conn.execute("PRAGMA table_info(news_cards)")}
            keyword_expr = ("keywords" if "keywords" in news_columns
                            else "NULL AS keywords")
            query = (
                "SELECT url, title, title_zh, title_en, dimension, "
                "published, score, " + keyword_expr + " FROM news_cards"
            )
            for r in conn.execute(query):
                # 关键词派生词集：聚合 + top news 共用（保持原两趟扫描行为一致；
                # HF 仅靠模型名命中的词不进 top）。
                kw_canons = _news_row_canons(r)
                # HF 词标题命中（即使抽词没抽到）：只进聚合，不进 top news
                titles_lower = [decode_html_entities(r["title"] or "").lower(),
                                decode_html_entities(r["title_zh"] or "").lower(),
                                decode_html_entities(r["title_en"] or "").lower()]
                agg_canons = list(kw_canons)
                for canon, meta in hf_terms.items():
                    if canon in kw_canons:
                        continue
                    last_seg = (meta["hf"].get("full_id") or "").split("/")[-1]
                    if (_match_hf_term(canon, meta["display"], titles_lower)
                            or _match_hf_term(canon, last_seg, titles_lower)
                            or _match_hf_term(canon, canon, titles_lower)):
                        agg_canons.append(canon)
                # 聚合先跑（先建 agg 条目），top news 后追——保证首行命中时条目已存在
                for canon in agg_canons:
                    a = agg.setdefault(canon, {
                        "mentions": 0, "hot_score": 0, "urls": set(),
                        "dims": {}, "top": [], "latest_pub": "", "earliest_pub": "9999",
                        "pubs": set(), "cur_cnt": 0, "cur_score": 0, "cur_signal": 0.0,
                    })
                    url = r["url"] or ""
                    if url not in a["urls"]:
                        a["urls"].add(url)
                        a["mentions"] += 1
                    pub = r["published"] or ""
                    if pub:
                        a["pubs"].add(pub)
                    if pub >= hot_cutoff:
                        a["hot_score"] += int(r["score"] or 0)
                    d = r["dimension"] or "其他"
                    a["dims"][d] = a["dims"].get(d, 0) + 1
                    if pub > a["latest_pub"]:
                        a["latest_pub"] = pub
                    if pub and pub < a["earliest_pub"]:
                        a["earliest_pub"] = pub
                    if url in cur_urls:
                        a["cur_cnt"] += 1
                        a["cur_score"] += int(r["score"] or 0)
                        a["cur_signal"] += cur_signal_by_url.get(url, 0.0)
                # top news（每词按 score 取前 3，裁剪 6 字段；结束时统一排序截断）
                for canon in kw_canons:
                    a = agg.get(canon)
                    if a is None:
                        continue
                    a["top"].append({
                        "score": int(r["score"] or 0),
                        "card": {
                            "title_zh": decode_html_entities(
                                r["title_zh"] or r["title"] or ""),
                            "title_en": decode_html_entities(
                                r["title_en"] or r["title"] or ""),
                            "official_url": decode_url_entities(r["url"] or ""),
                            "source": "",
                            "published": r["published"] or "",
                            "hot": int(r["score"] or 0),
                        },
                    })
            conn.close()
        except Exception:
            pass
    for a in agg.values():
        # top news 与 get_term_news 同序（published 降序 + score 降序），
        # 保证卡片内嵌预览与「展开更多」列表顺序一致，展开时不重新排序。
        a["top"].sort(key=lambda x: -x["score"])
        a["top"].sort(key=lambda x: x["card"].get("published") or "", reverse=True)
        a["top"] = [t["card"] for t in a["top"][:3]]

    # ---- 3. 归并 HF 词（无新闻命中也入池，origin=hf）----
    for canon, meta in hf_terms.items():
        agg.setdefault(canon, {
            "mentions": 0, "hot_score": 0, "urls": set(), "dims": {},
            "top": [], "latest_pub": "", "earliest_pub": "9999",
            "pubs": set(), "cur_cnt": 0, "cur_score": 0, "cur_signal": 0.0,
        })

    # ---- 4. 读旧 terms 表（保留 first_seen_at / display 演进）----
    # 流式读取，不 fetchall 物化；用完后及时释放，避免与第 7 步 final_rows 双份驻留。
    old = {}
    try:
        conn = _conn()
        for r in conn.execute("SELECT * FROM terms"):
            old[r["term"]] = dict(r)
        conn.close()
    except Exception:
        old = {}

    # ---- 5. 噪词过滤 + 新增 cap ----
    kept = {}
    new_budget = MAX_NEW_TERMS_PER_CYCLE
    for canon, a in agg.items():
        in_lexicon = canon in _LEXICON
        is_hf = canon in hf_terms
        existed = canon in old
        if not in_lexicon and not is_hf and not existed:
            # 全新非词典词：单轮新增预算 + 噪词过滤（单次出现且 ASCII 短词丢弃）
            if a["mentions"] <= 1 and canon.isascii() and len(canon) < 4:
                continue
            if new_budget <= 0:
                continue
            new_budget -= 1
        kept[canon] = a

    # ---- 5.5 标题关联计数（与 get_term_news 标题兜底同口径）----
    # 词卡 news_cnt / 详情页「N 篇相关报道」需与 get_term_news 一致：除关键词命中外，
    # 标题表面命中（_term_surfaces）也算关联报道。只对 kept 词补算展示计数，
    # 不影响三榜打分与噪词过滤（词池与榜单保持稳定）。
    if news_store:
        try:
            kept_pats = {canon: _compile_surface_patterns(_term_surfaces(canon))
                         for canon in kept}
            conn = _conn()
            news_columns = {r[1] for r in
                            conn.execute("PRAGMA table_info(news_cards)")}
            keyword_expr = ("keywords" if "keywords" in news_columns
                            else "NULL AS keywords")
            query = ("SELECT url, title, title_zh, title_en, " + keyword_expr +
                     " FROM news_cards")
            for r in conn.execute(query):
                url = r["url"] or ""
                if not url:
                    continue
                titles = [str(r[f] or "")
                          for f in ("title", "title_zh", "title_en")]
                for canon, a in kept.items():
                    if url in a["urls"]:
                        continue
                    if _title_matches_patterns(titles, kept_pats[canon]):
                        a["urls"].add(url)
                        a["mentions"] += 1
            conn.close()
        except Exception:
            pass

    # ---- 6. 三榜打分 + 写 terms 主表 + 快照 ----
    with _db_lock:
        conn = _conn()
        for canon, a in kept.items():
            o = old.get(canon) or {}
            is_hf = canon in hf_terms
            hf_meta = {}
            if is_hf:
                hf_meta = hf_terms[canon]["hf"]
            elif o.get("hf_json"):
                try:
                    hf_meta = json.loads(o["hf_json"])
                except (json.JSONDecodeError, ValueError):
                    hf_meta = {}
            origin = ("both" if a["mentions"] > 0 and is_hf else
                      ("hf" if is_hf else "news"))
            hot = a["hot_score"] + int(hf_meta.get("likes", 0) or 0)

            # rise：活动量环比
            m_cur = a["cur_cnt"] + a["cur_score"] / 2000.0
            try:
                prev = conn.execute(
                    "SELECT news_cnt, score_sum FROM term_snapshots "
                    "WHERE term=? AND cycle<>? ORDER BY cycle DESC LIMIT 1",
                    (canon, cycle)).fetchone()
            except Exception:
                prev = None
            if prev:
                m_prev = prev["news_cnt"] + prev["score_sum"] / 2000.0
                rise = (m_cur - m_prev) / max(m_prev, 0.5)
            else:
                rise = math.log(1 + m_cur) if m_cur > 0 else 0.0
            rise = max(-1.0, min(10.0, rise))

            # novelty：fresh × rarity
            healed_first_seen = False
            first_seen = o.get("first_seen_at") or ""
            earliest = a["earliest_pub"] if a["earliest_pub"] != "9999" else ""
            # first_seen 自愈：存档日期若不再被任何关联卡锚定（词表被脏关联污染过），
            # 用当前最早报道回填；仍被锚定则保留（真·历史首现，immutable）。
            if first_seen and first_seen[:10] not in a["pubs"]:
                first_seen = ""
                healed_first_seen = True
            if earliest and (not first_seen or earliest < first_seen[:10]):
                first_seen = earliest
            if not first_seen:
                first_seen = now
            try:
                age_days = (datetime.datetime.now() -
                            datetime.datetime.fromisoformat(first_seen[:19])).days
            except (ValueError, TypeError):
                age_days = 0
            fresh = 2.0 if age_days <= 2 else math.exp(-age_days / 10.0)
            rarity = 1.0 / (1.0 + math.log(1 + a["mentions"]))
            novelty = round(fresh * rarity, 4)

            # display 演进：优先 HF 展示名/旧展示名/新构造
            display = (hf_terms.get(canon, {}).get("display")
                       or o.get("display") or _display_of(canon, []))
            display_zh = o.get("display_zh") or _display_zh_of(canon)

            conn.execute(
                """INSERT INTO terms (term, display, display_zh, origin,
                       first_seen_at, last_seen_at, total_mentions, hf_json,
                       cur_hot, cur_rise, cur_novelty)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(term) DO UPDATE SET
                       display=excluded.display, display_zh=excluded.display_zh,
                       origin=excluded.origin, last_seen_at=excluded.last_seen_at,
                       total_mentions=excluded.total_mentions,
                       hf_json=excluded.hf_json, cur_hot=excluded.cur_hot,
                       cur_rise=excluded.cur_rise, cur_novelty=excluded.cur_novelty""",
                (canon, display, display_zh, origin,
                 first_seen, now, a["mentions"],
                 json.dumps(hf_meta, ensure_ascii=False) if hf_meta else "",
                 hot, round(rise, 4), novelty),
            )
            conn.execute(
                """INSERT INTO term_snapshots (term, cycle, news_cnt, score_sum, signal_sum)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(term, cycle) DO UPDATE SET
                       news_cnt=excluded.news_cnt, score_sum=excluded.score_sum,
                       signal_sum=excluded.signal_sum""",
                (canon, cycle, a["cur_cnt"], a["cur_score"], round(a["cur_signal"], 1)),
            )
            if healed_first_seen:
                # ON CONFLICT 不更新 first_seen_at；自愈场景需显式回填
                conn.execute("UPDATE terms SET first_seen_at=? WHERE term=?",
                             (first_seen, canon))
        # 本轮未命中的词：三分清零（不出榜），历史字段保留
        if kept:
            placeholders = ",".join("?" * len(kept))
            conn.execute(
                f"UPDATE terms SET cur_hot=0, cur_rise=0, cur_novelty=0 "
                f"WHERE term NOT IN ({placeholders})", list(kept.keys()))
        conn.commit()
        conn.close()
        old = None  # 第 6 步结束即释放旧表，避免与 final_rows 双份驻留内存

    # ---- 7. 组装词卡写 words.json（只读回 kept 词，避免整表物化）----
    try:
        conn = _conn()
        final_rows = {}
        for r in conn.execute(
                "SELECT term, display, display_zh, origin, first_seen_at, "
                "last_seen_at, total_mentions, hf_json, cur_hot, cur_rise, "
                "cur_novelty FROM terms"):
            if r["term"] in kept:
                final_rows[r["term"]] = dict(r)
        conn.close()
    except Exception:
        final_rows = {}
    cards = []
    for canon, a in kept.items():
        row = final_rows.get(canon)
        if not row:
            continue
        dims_counter = a["dims"]
        dim_order = sorted(dims_counter,
                           key=lambda d: (-dims_counter[d], d))
        main_dim = dim_order[0] if dim_order else "其他"
        try:
            hf_meta = json.loads(row["hf_json"]) if row["hf_json"] else None
        except (json.JSONDecodeError, ValueError):
            hf_meta = None
        cards.append({
            "kind": "word",
            "id": canon,
            "term": row["display"] or canon,
            "display_zh": row["display_zh"] or "",
            "origin": row["origin"],
            "news_cnt": a["mentions"],
            "hot": row["cur_hot"],
            "score": row["cur_hot"],       # 兼容 hot 排序键
            "rise": row["cur_rise"],
            "trend": row["cur_rise"],      # 兼容 rise 排序键
            "novelty": row["cur_novelty"],
            "first_seen_at": row["first_seen_at"],
            "published": a["latest_pub"],  # 兼容 new 排序键/前端展示
            "dimension": main_dim,
            "dims": dim_order,
            "top_news": a["top"],
            "hf": hf_meta,
        })
    cards = _dedupe_word_cards(cards)
    _sort_word_cards(cards, "hot")
    _file_cache_set({"ok": True, "terms": cards[:WORD_CARDS_LIMIT],
                     "count": len(cards)}, fetched_at)


# ---------- 读：请求路径 ----------
_SORT_KEYS = {"rise": "rise", "hot": "hot", "new": "novelty"}


def get_word_cards(sort="rise", lang="zh", limit=60):
    """返回词卡列表（/api/stream?view=words 数据源）。只读 words.json，秒回。

    sort: rise（环比增速）/ hot（热度）/ new（新奇度，非时间序）。
    lang 投影：zh 优先 display_zh，en 用 display；top_news 标题投影对应语言。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    key = _SORT_KEYS.get(sort, "rise")
    data, fetched_at = _file_cache_get()
    # 同一份缓存先去重、按请求榜单完整排序，最后才截展示上限；
    # 排序和截断不能交给前端再次处理，否则边界处会出现顺序漂移。
    raw = _dedupe_word_cards(list((data or {}).get("terms", [])))
    _sort_word_cards(raw, key)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 60
    cards = []
    for c in raw[:limit]:
        wc = dict(c)
        for field in ("term", "display_zh"):
            if field in wc:
                wc[field] = decode_html_entities(wc[field])
        if lang == "zh" and wc.get("display_zh"):
            wc["term_display"] = wc["display_zh"]
        else:
            wc["term_display"] = wc.get("term", "")
        topn = []
        for n in wc.get("top_news", []):
            n2 = dict(n)
            for field in ("title", "title_zh", "title_en"):
                if field in n2:
                    n2[field] = decode_html_entities(n2[field])
            if "official_url" in n2:
                n2["official_url"] = decode_url_entities(n2["official_url"])
            n2["title"] = (n2.get("title_zh") if lang == "zh" else n2.get("title_en")) \
                or n2.get("title_zh") or n2.get("title") or ""
            topn.append(n2)
        wc["top_news"] = topn
        cards.append(wc)
    return cards, fetched_at


def get_term_row(term):
    """查 terms 主表（按 canonical 键）。未命中返回 None。"""
    if not _DB_OK:
        return None
    canon = normalize_term(term)
    if not canon:
        return None
    try:
        conn = _conn()
        r = conn.execute("SELECT * FROM terms WHERE term=?", (canon,)).fetchone()
        conn.close()
        return dict(r) if r else None
    except Exception:
        return None


def get_term_news(term, limit=50, lang="zh"):
    """词 → 关联报道（canonical keywords + 标题命中兜底）。

    新卡的 ``keywords`` 是 canonical JSON，历史卡可能没有该列、列值为
    ``[]``，或仍保存旧的表面形式。因此 SQL 只负责收集候选行，最终的
    关联判断统一在 Python 中做 canonical/别名归一和版本感知边界匹配。
    返回与 dims 卡同 schema 的投影卡列表，published 降序 + score 降序。
    """
    if not _DB_OK or not news_store:
        return []
    canon = normalize_term(term)
    if not canon:
        return []
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 50
    if not limit:
        return []

    surfaces = _term_surfaces(canon)

    def _like_literal(value):
        # SQL parameters prevent injection, but LIKE still treats %/_ as
        # wildcards.  Escape them so a custom term cannot broaden the scan.
        return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    try:
        conn = _conn()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(news_cards)")}
        has_keywords = "keywords" in columns
        title_fields = [f for f in ("title", "title_zh", "title_en")
                        if f in columns]

        # Keep the query LIKE-based for the normal SQLite path, but do not
        # apply LIMIT before Python verification: a newer-version false
        # positive must not crowd a valid historical card out of the result.
        clauses = []
        params = []
        if has_keywords:
            for surface in surfaces:
                clauses.append("keywords LIKE ? ESCAPE '\\'")
                # Do not require JSON quotes here: a few old/manual rows used
                # a plain or comma-separated keyword value.  Python verifies
                # the decoded value below, so broad candidates are safe.
                params.append("%" + _like_literal(surface) + "%")
        for field in title_fields:
            for surface in surfaces:
                clauses.append(f"{field} LIKE ? ESCAPE '\\'")
                params.append("%" + _like_literal(surface) + "%")
        if not clauses:
            conn.close()
            return []

        # ``NULL AS keywords`` lets the same row handling work even if a
        # production database predates the idempotent ALTER TABLE migration.
        select = ("SELECT * FROM news_cards" if has_keywords else
                  "SELECT *, NULL AS keywords FROM news_cards")
        rows = conn.execute(
            f"{select} WHERE ({' OR '.join(clauses)}) "
            "ORDER BY published DESC, score DESC",
            params).fetchall()
        conn.close()

        out = []
        for r in rows:
            # Known aliases (GPT5, GPT 5, 智能体, …) are handled by
            # _term_surfaces, including the canonical spelling itself.
            keywords_match = canon in _keyword_canons(r["keywords"])
            titles = [str(r[f] or "") for f in title_fields]
            title_match = any(_title_matches_term(t, surfaces) for t in titles)
            if keywords_match or title_match:
                out.append(r)
                if len(out) >= limit:
                    break
        return [news_store._row_to_card(r) for r in out[:limit]]
    except Exception:
        return []


def list_terms_for_sitemap(limit=200):
    """sitemap 用：按热度降序返回词 display 列表。"""
    if not _DB_OK:
        return []
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT display FROM terms ORDER BY cur_hot DESC, total_mentions DESC "
            "LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [r["display"] for r in rows if r["display"]]
    except Exception:
        return []


# ---------- 历史回填（零 LLM 成本）----------
def backfill_history(days=30, force=False):
    """用词典匹配回填 news_cards.keywords + 合成历史 term_snapshots。

    - 默认只处理 keywords 为空（'[]'/NULL）的行，重复跑零副作用；--force 全量重算。
    - 按 published 日期分桶合成快照（cycle=f"{published}-00"），让 rise 环比
      上线首日即有历史基数。
    返回处理行数（CLI 打印用）。
    """
    if not _DB_OK or not news_store:
        print("[backfill] DB 不可用，跳过")
        return 0
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    try:
        conn = _conn()
        if force:
            # --force：全量重算（不限日期，覆盖归档老卡），用于词典/正则升级后清理
            where, params = "WHERE 1=1", []
        else:
            where = "WHERE published >= ? AND (keywords IS NULL OR keywords = '[]')"
            params = [cutoff]
        rows = conn.execute(
            f"SELECT url, title, title_zh, title_en, published, score FROM news_cards "
            f"{where}", params).fetchall()
    except Exception as e:
        print(f"[backfill] 读取失败: {e}")
        return 0

    updated = 0
    snap = {}  # (term, date) → {cnt, score_sum}
    try:
        with _db_lock:
            for r in rows:
                text = " ".join([str(r["title"] or ""),
                                 str(r["title_zh"] or ""),
                                 str(r["title_en"] or "")])
                kws = extract_keywords_dict(text)
                if not kws:
                    # force 模式：抽不出词的行把残留 keywords 清空（词典/正则升级后清理脏数据）
                    if force:
                        conn.execute("UPDATE news_cards SET keywords='[]' WHERE url=?",
                                     (r["url"],))
                        updated += 1
                    continue
                conn.execute("UPDATE news_cards SET keywords=? WHERE url=?",
                             (json.dumps(kws, ensure_ascii=False), r["url"]))
                updated += 1
                pub = r["published"] or ""
                if pub:
                    for k in kws:
                        key = (k, pub)
                        s = snap.setdefault(key, {"cnt": 0, "score_sum": 0})
                        s["cnt"] += 1
                        s["score_sum"] += int(r["score"] or 0)
                if updated % 500 == 0:
                    conn.commit()
            # 合成历史快照（不覆盖真实刷新产生的快照）
            for (term, pub), s in snap.items():
                conn.execute(
                    """INSERT INTO term_snapshots (term, cycle, news_cnt, score_sum, signal_sum)
                       VALUES (?,?,?,?,0)
                       ON CONFLICT(term, cycle) DO NOTHING""",
                    (term, f"{pub}-00", s["cnt"], s["score_sum"]))
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[backfill] 写入失败: {e}")
    print(f"[backfill] 完成：回填 {updated}/{len(rows)} 行，合成快照 {len(snap)} 条")
    return updated


init_db()


if __name__ == "__main__":
    # CLI：python terms.py backfill [--days 30] [--force]
    args = sys.argv[1:]
    if args and args[0] == "backfill":
        days = 30
        force = "--force" in args
        for i, a in enumerate(args):
            if a == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    pass
        backfill_history(days=days, force=force)
    else:
        print("用法: python terms.py backfill [--days 30] [--force]")
