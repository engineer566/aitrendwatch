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
# 每轮解释批次上限（词数）：解释生成在 dims 刷新锁内执行，批次过大 + LLM 不稳会
# 长时间占锁阻塞 words.json 更新；按热度降序取前 N 个（最热优先），存量无解释词
# 随后续刷新逐轮回填。默认 60 词 ≈ 5 批，单轮最多约 5-10 分钟。
EXPLAIN_BATCH_MAX_WORDS = 60


def _hot_recency_weight(pub, today):
    """近 7 天热窗内的报道分按发布新鲜度加权——让「今日热词」不被存量累计分埋没。

    报道越新权重越高（≤1 天 ×3，≤3 天 ×1.5，更早 ×1.0）。与 score 自身的时间
    衰减正交：score 是发布时刻的绝对热度衰减，这里是热窗内的相对新鲜度加权。
    权重作用在单篇报道的 score 上（热窗聚合入口），越靠近今天的热点词排名越高；
    旧高分报道仍保留 ×1.0 基数，不彻底出局。
    """
    try:
        age_days = (today - datetime.date.fromisoformat(pub[:10])).days
    except (ValueError, TypeError):
        return 1.0
    if age_days < 0:
        age_days = 0
    if age_days <= 1:
        return 3.0
    if age_days <= 3:
        return 1.5
    return 1.0


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
                display_en     TEXT DEFAULT '',   -- 英文展示名（中文词的 LLM 翻译，可空）
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
                win7_cnt   INTEGER DEFAULT 0, -- 近 7 天滑动窗口内关联报道数（rise 环比口径）
                score_sum  INTEGER DEFAULT 0, -- 本周期 Σ score
                signal_sum REAL DEFAULT 0,    -- 本周期 Σ 社区信号
                PRIMARY KEY (term, cycle)
            );
        """)
        # 老库补列（幂等）：display_en 缺失时 ALTER 加上；动态词典（词池即词典）
        # 的解释列 explain_zh/explain_en + 新鲜度 explain_updated_at 同模式补列。
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(terms)")}
            for col, ddl in (("display_en", "TEXT DEFAULT ''"),
                             ("explain_zh", "TEXT DEFAULT ''"),
                             ("explain_en", "TEXT DEFAULT ''"),
                             ("explain_updated_at", "TEXT DEFAULT ''")):
                if col not in cols:
                    conn.execute(
                        f"ALTER TABLE terms ADD COLUMN {col} {ddl}")
            snap_cols = {r[1] for r in
                         conn.execute("PRAGMA table_info(term_snapshots)")}
            if "win7_cnt" not in snap_cols:
                conn.execute(
                    "ALTER TABLE term_snapshots ADD COLUMN win7_cnt "
                    "INTEGER DEFAULT 0")
                # 存量快照 win7_cnt 置 0 会让部署后首个刷新轮次所有词 rise 虚高
                # （m_prev=0 → 全部顶到 10 上限）。用旧口径 news_cnt 做基线，
                # 让首轮环比从「每轮报道数」平滑过渡到「7 天窗口值」。
                conn.execute(
                    "UPDATE term_snapshots SET win7_cnt = news_cnt")
        except Exception:
            pass
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
    "GLM":          ["glm", "智谱", "chatglm", "智谱清言"],
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
    "openclaw":     ["openclaw", "open claw"],
    "perplexity":   ["perplexity"],
    "huggingface":  ["huggingface", "hugging face", "抱抱脸"],
    "ollama":       ["ollama"],
    "vllm":         ["vllm"],
    "nvidia":       ["nvidia", "英伟达"],
    "CUDA":         ["cuda"],
    "AMD":          ["amd"],
    "apple-intelligence": ["apple intelligence", "苹果智能"],
    "siri":         ["siri"],
    "meta-ai":      ["meta ai", "meta人工智能"],
    "xAI":          ["xai"],
    "microsoft":    ["microsoft", "微软"],
    "google":       ["google", "谷歌"],
    "bytedance":    ["bytedance", "字节跳动", "字节"],
    "alibaba":      ["alibaba", "阿里"],
    "tencent":      ["tencent", "腾讯"],
    "baidu":        ["baidu", "百度"],
    "huawei":       ["huawei", "华为"],
    "tsinghua":     ["tsinghua", "清华"],
    # —— 技术概念 ——
    "LLM":          ["llm", "llms", "大模型", "大语言模型"],
    "agent":        ["agent", "agents", "智能体", "ai agent"],
    "RAG":          ["rag", "retrieval-augmented", "检索增强"],
    "MCP":          ["mcp", "model context protocol"],
    "multimodal":   ["multimodal", "多模态"],
    "diffusion":    ["diffusion", "扩散模型"],
    "transformer":  ["transformer"],
    "fine-tuning":  ["fine-tuning", "finetuning", "fine tuning", "微调"],
    "RLHF":         ["rlhf", "人类反馈强化学习"],
    "reinforcement-learning": ["reinforcement learning", "强化学习"],
    "reasoning":    ["reasoning", "推理模型", "思维链", "chain-of-thought", "cot"],
    "embedding":    ["embedding", "embeddings", "向量", "词向量"],
    "vector-db":    ["vector database", "向量数据库"],
    "prompt":       ["prompt engineering", "提示词", "提示工程"],
    "context-window": ["context window", "上下文窗口", "长上下文", "long context"],
    "kv-cache":     ["kv cache", "kv-cache"],
    "quantization": ["quantization", "量化"],
    "distillation": ["distillation", "蒸馏", "知识蒸馏"],
    "LoRA":         ["lora", "qlora"],
    "MoE":          ["moe", "mixture of experts", "混合专家"],
    "benchmark":    ["benchmark", "benchmarks", "基准测试", "评测"],
    "AGI":          ["agi", "通用人工智能"],
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

# ---------- 通用热词停用词表（canonical 键）----------
# 低价值通用词：即使被词典/LLM/HF 抽中，也不作为独立热词进入词池（如 "AI"、
# "模型" 这类词单独出现没有信息量）。键必须是 normalize_term() 输出的 canonical
# 形式（小写、空白/连字符归 '-');维护时先 normalize_term 再放入。
_TERM_STOPWORDS = {
    "ai",                       # AI
    "artificial-intelligence",  # 人工智能
    "machine-learning",         # 机器学习
    "deep-learning",            # 深度学习
    "LLM",                      # 大语言模型（通用概念，大写 canonical）
    "model",                    # 模型（通用词）
    "technology",               # 技术
    "tech",                     # 科技/技术
}

# ---------- 热词解释词典（canonical → 中/英解释）----------
# 供热词详情页展示「这是什么」的静态文案；canonical 键与 _LEXICON 对齐。
# 覆盖词典主要词条（头部模型/产品全量 + 技术概念尽量全），未收录词解释为空串。
# 文案要求客观、准确，不确定的事实用模糊但正确的表述，避免编造。
_EXPLANATIONS = {
    # —— 头部模型/产品 ——
    "gpt-5": {"zh": "OpenAI 于 2025 年发布的旗舰多模态大模型，GPT-4o 的下一代，原生支持文本、图像、语音与代码生成。", "en": "OpenAI's flagship multimodal model released in 2025, the successor to GPT-4o, natively supporting text, image, voice and code generation."},
    "gpt-4o": {"zh": "OpenAI 于 2024 年发布的「全模态」（omni）旗舰模型，原生融合文本、图像与语音处理。", "en": "OpenAI's omni flagship model released in 2024, natively combining text, image and voice processing."},
    "chatgpt": {"zh": "OpenAI 的消费级 AI 助手产品，基于 GPT 系列大模型，提供对话、写作与编程等能力。", "en": "OpenAI's consumer AI assistant built on the GPT models, offering conversation, writing and coding."},
    "openai": {"zh": "美国人工智能研究公司，GPT 系列模型与 ChatGPT 的开发商。", "en": "The American AI research company behind the GPT model family and ChatGPT."},
    "claude": {"zh": "Anthropic 开发的对话式 AI 模型与助手系列，以安全对齐与长上下文能力见长。", "en": "Anthropic's family of conversational AI models and assistants, known for safety alignment and long context."},
    "anthropic": {"zh": "美国 AI 安全公司，Claude 系列模型的开发商。", "en": "The American AI safety company that develops the Claude model family."},
    "gemini": {"zh": "Google DeepMind 开发的多模态 AI 模型系列，横跨文本、图像、音频与视频。", "en": "Google DeepMind's multimodal AI model family spanning text, image, audio and video."},
    "google-deepmind": {"zh": "Google 旗下 AI 研究机构，由 DeepMind 与 Google Brain 合并而成，Gemini 模型的开发者。", "en": "Google's AI research lab formed from the DeepMind and Google Brain merger; developer of the Gemini models."},
    "llama": {"zh": "Meta 发布的开源大语言模型系列，是开源模型生态的重要推动力量。", "en": "Meta's open-source large language model family, a major driver of the open model ecosystem."},
    "qwen": {"zh": "阿里巴巴（通义）开发的开源大语言模型系列，覆盖多种参数规模与多模态能力。", "en": "Alibaba's open-source large language model family, available in multiple sizes with multimodal abilities."},
    "deepseek": {"zh": "中国 AI 公司深度求索开发的高性能大模型系列，以开源权重与低成本推理著称。", "en": "The high-performance model family by Chinese AI company DeepSeek, known for open weights and cost-efficient inference."},
    "kimi": {"zh": "月之暗面（Moonshot AI）开发的 AI 助手与大模型系列，主打长文本处理能力。", "en": "The AI assistant and model family by Moonshot AI, focused on long-context processing."},
    "doubao": {"zh": "字节跳动旗下的 AI 助手产品，基于其自研大模型。", "en": "ByteDance's AI assistant product built on its in-house large models."},
    "wenxin": {"zh": "百度的大模型与 AI 助手品牌（文心一言），提供对话与内容生成能力。", "en": "Baidu's large model and AI assistant brand (ERNIE Bot) offering dialogue and content generation."},
    "GLM": {"zh": "智谱 AI 开发的开源大模型系列（GLM/ChatGLM），提供对话与多模态能力。", "en": "Zhipu AI's open-source GLM/ChatGLM model family with conversational and multimodal abilities."},
    "hunyuan": {"zh": "腾讯的大模型系列（混元），支撑其多款 AI 应用。", "en": "Tencent's Hunyuan large model family powering its AI applications."},
    "mistral": {"zh": "法国公司 Mistral AI 的开源大模型系列，以高效架构著称。", "en": "The open-weight model family by French company Mistral AI, known for efficient architectures."},
    "grok": {"zh": "xAI 开发的 AI 对话助手，整合 X 平台的实时信息。", "en": "xAI's AI conversation assistant, integrated with real-time X (Twitter) data."},
    "copilot": {"zh": "微软推出的 AI 助手品牌，覆盖代码补全（GitHub Copilot）与办公（Microsoft 365 Copilot）等场景。", "en": "Microsoft's AI assistant brand spanning code completion (GitHub Copilot) and productivity (Microsoft 365 Copilot)."},
    "sora": {"zh": "OpenAI 的文生视频模型，可根据文本提示生成连贯视频片段。", "en": "OpenAI's text-to-video model that generates coherent video clips from text prompts."},
    "midjourney": {"zh": "知名的 AI 文生图工具，以高质量艺术风格图像著称。", "en": "A leading AI text-to-image tool known for high-quality artistic images."},
    "stable-diffusion": {"zh": "由 Stability AI 与社区推动的开源文生图扩散模型系列。", "en": "The open-source text-to-image diffusion model family driven by Stability AI and the community."},
    "flux": {"zh": "Black Forest Labs 开发的开源图像生成模型系列。", "en": "The open-source image generation model family by Black Forest Labs."},
    "veo": {"zh": "Google 的文生视频模型系列（Veo），支持高分辨率视频生成。", "en": "Google's Veo text-to-video model family supporting high-resolution video generation."},
    "suno": {"zh": "AI 音乐生成平台，可依据文本提示生成带人声的歌曲。", "en": "An AI music generation platform that creates songs with vocals from text prompts."},
    "cursor": {"zh": "AI 代码编辑器，深度集成大模型辅助编程。", "en": "An AI-powered code editor with deeply integrated large-model assistance."},
    "devin": {"zh": "Cognition 推出的 AI 软件工程师（编码代理）产品，可自主完成编程任务。", "en": "Cognition's autonomous AI software engineer (coding agent) product."},
    "manus": {"zh": "中国团队推出的通用 AI 代理产品，可自主完成多步骤任务。", "en": "A general-purpose AI agent product by a Chinese team that autonomously completes multi-step tasks."},
    "openclaw": {"zh": "开源的自主 AI 智能体项目，可自主操控电脑完成浏览、操作等多步骤任务；曾在开发者社区意外走红，引发广泛讨论。", "en": "An open-source autonomous AI agent project that can operate a computer to complete multi-step tasks; it went unexpectedly viral in the developer community."},
    "perplexity": {"zh": "AI 搜索产品，用大模型对检索结果进行摘要式回答。", "en": "An AI search product that summarizes retrieved results with large language models."},
    "huggingface": {"zh": "机器学习社区与模型托管平台，是开源模型生态的中心。", "en": "The machine-learning community and model-hosting platform at the center of the open model ecosystem."},
    "ollama": {"zh": "本地运行开源大模型的工具，一条命令即可拉取并运行模型。", "en": "A tool for running open-source large models locally with one-command pulls."},
    "vllm": {"zh": "高性能大模型推理引擎，广泛用于模型服务部署。", "en": "A high-performance large-model inference engine widely used for model serving."},
    "nvidia": {"zh": "美国芯片公司，GPU 与 AI 加速卡市场的领导者。", "en": "The American chip company that leads the GPU and AI accelerator market."},
    "CUDA": {"zh": "NVIDIA 的并行计算平台与编程模型，是 AI 训练与推理的事实标准之一。", "en": "NVIDIA's parallel computing platform and programming model, a de facto standard for AI compute."},
    "AMD": {"zh": "美国芯片公司，NVIDIA 在 GPU 与加速卡市场的主要竞争对手。", "en": "The American chip company, NVIDIA's main rival in the GPU and accelerator market."},
    "apple-intelligence": {"zh": "苹果推出的 AI 能力体系，覆盖系统级智能与端侧模型。", "en": "Apple's AI feature stack spanning system-wide intelligence and on-device models."},
    "siri": {"zh": "苹果的语音助手，正逐步接入 Apple Intelligence 能力。", "en": "Apple's voice assistant, increasingly powered by Apple Intelligence."},
    "meta-ai": {"zh": "Meta 的 AI 助手，整合进 Facebook、WhatsApp、Instagram 等应用。", "en": "Meta's AI assistant integrated across Facebook, WhatsApp and Instagram."},
    "xAI": {"zh": "埃隆·马斯克创立、Grok 模型的开发商。", "en": "The AI company founded by Elon Musk; developer of the Grok models."},
    "microsoft": {"zh": "美国科技公司，OpenAI 的主要投资方，深度整合 Copilot 生态。", "en": "The American tech company, OpenAI's major investor, deeply integrating the Copilot ecosystem."},
    "google": {"zh": "美国科技公司，Google DeepMind 与 Gemini 模型的所有者。", "en": "The American tech company that owns Google DeepMind and the Gemini models."},
    "bytedance": {"zh": "中国科技公司（字节跳动），抖音/TikTok 与豆包大模型的母公司。", "en": "The Chinese tech company behind TikTok/Douyin and the Doubao large models."},
    "alibaba": {"zh": "中国科技公司，通义千问（Qwen）开源模型系列的开发者。", "en": "The Chinese tech company that develops the open-source Qwen (Tongyi Qianwen) model family."},
    "tencent": {"zh": "中国科技公司，混元大模型的开发者。", "en": "The Chinese tech company that develops the Hunyuan large model family."},
    "baidu": {"zh": "中国科技公司，文心一言（ERNIE）大模型的开发者。", "en": "The Chinese tech company that develops the ERNIE (Wenxin Yiyan) large model."},
    "huawei": {"zh": "中国科技公司，自研昇腾 AI 芯片与盘古大模型。", "en": "The Chinese tech company developing the Ascend AI chips and the Pangu large models."},
    "tsinghua": {"zh": "清华大学，GLM（智谱）等国产大模型的重要学术源头。", "en": "Tsinghua University, an important academic origin of Chinese large models such as GLM (Zhipu)."},
    # —— 技术概念 ——
    "LLM": {"zh": "大语言模型，在海量文本上预训练、可理解和生成自然语言的深度学习模型。", "en": "Large language models: deep-learning models pretrained on massive text to understand and generate natural language."},
    "agent": {"zh": "智能体，能感知环境、自主规划并调用工具完成多步骤任务的 AI 系统。", "en": "AI systems that perceive their environment, plan autonomously and use tools to complete multi-step tasks."},
    "RAG": {"zh": "检索增强生成，先从外部知识库检索相关内容再交给大模型生成，用于减少幻觉。", "en": "Retrieval-augmented generation: retrieving relevant content from an external knowledge base before generation to reduce hallucination."},
    "MCP": {"zh": "Model Context Protocol，Anthropic 提出的开放协议，标准化 AI 应用与外部数据/工具的连接。", "en": "Model Context Protocol: an open protocol by Anthropic standardizing how AI applications connect to external data and tools."},
    "multimodal": {"zh": "多模态，模型同时处理文本、图像、音频、视频等多种数据类型的能力。", "en": "The ability of models to process multiple data types such as text, image, audio and video."},
    "diffusion": {"zh": "扩散模型，通过逐步去噪从随机噪声生成图像的生成式模型。", "en": "Generative models that produce images by progressively denoising random noise."},
    "transformer": {"zh": "Transformer 架构，现代大模型的基石，通过自注意力机制处理序列数据。", "en": "The Transformer architecture, the foundation of modern large models, which processes sequences via self-attention."},
    "fine-tuning": {"zh": "微调，在预训练模型基础上用特定数据继续训练，使其适配下游任务。", "en": "Continuing to train a pretrained model on task-specific data to adapt it for downstream use."},
    "RLHF": {"zh": "基于人类反馈的强化学习，用人类偏好训练奖励模型来对齐模型行为。", "en": "Reinforcement learning from human feedback: aligning model behavior using a reward model trained on human preferences."},
    "reinforcement-learning": {"zh": "强化学习，智能体通过与环境的试错交互来学习最优策略。", "en": "Machine learning in which an agent learns optimal behavior through trial-and-error interaction with an environment."},
    "reasoning": {"zh": "推理能力，模型在回答前进行多步逻辑思考，常见实现为思维链（Chain-of-Thought）。", "en": "A model's ability to perform multi-step logical thinking before answering, often implemented via chain-of-thought."},
    "embedding": {"zh": "嵌入/向量化，把文本等数据映射为稠密数值向量，便于计算相似度。", "en": "Mapping data such as text into dense numeric vectors for similarity computation."},
    "vector-db": {"zh": "向量数据库，专为存储与检索高维向量（如文本嵌入）而设计的数据库。", "en": "Databases designed to store and retrieve high-dimensional vectors such as text embeddings."},
    "prompt": {"zh": "提示词/提示工程，通过设计输入指令引导大模型输出的技术。", "en": "The practice of crafting input instructions to steer large-model outputs."},
    "context-window": {"zh": "上下文窗口，模型单次可处理的输入 token 数量上限。", "en": "The maximum number of input tokens a model can process at once."},
    "kv-cache": {"zh": "键值缓存，推理时为避免重复计算而缓存注意力键值的技术。", "en": "A technique that caches attention keys and values during inference to avoid redundant computation."},
    "quantization": {"zh": "量化，压缩模型权重的数值精度，以减少内存占用并加速推理。", "en": "Compressing model weight precision to reduce memory usage and speed up inference."},
    "distillation": {"zh": "知识蒸馏，用大模型（教师）的输出训练小模型（学生）的技术。", "en": "Training a smaller student model on the outputs of a larger teacher model."},
    "LoRA": {"zh": "低秩适配，一种参数高效微调方法，只训练少量新增参数。", "en": "Low-Rank Adaptation: a parameter-efficient fine-tuning method that trains only a small set of new parameters."},
    "MoE": {"zh": "混合专家，把模型拆成多个专家子网络、按输入动态激活，以提升规模与效率。", "en": "Mixture of Experts: routing inputs through subsets of expert sub-networks to scale capacity efficiently."},
    "benchmark": {"zh": "基准测试/评测，用标准化数据集衡量并比较模型能力的方式。", "en": "Standardized datasets and tasks used to measure and compare model capabilities."},
    "AGI": {"zh": "通用人工智能，能在所有认知任务上达到或超越人类水平的假设性 AI。", "en": "Artificial general intelligence: hypothetical AI matching or exceeding human ability across all cognitive tasks."},
    "alignment": {"zh": "对齐，让模型行为符合人类意图与价值观的技术方向。", "en": "Techniques for making model behavior match human intentions and values."},
    "hallucination": {"zh": "幻觉，模型生成看似合理但事实错误或凭空捏造的内容。", "en": "Model outputs that sound plausible but are factually wrong or fabricated."},
    "token": {"zh": "token，模型处理文本的基本单元，约为子词或字符块。", "en": "The basic unit of text that models process, roughly a sub-word chunk."},
    "inference": {"zh": "推理（执行），模型部署后对输入产生输出的过程；也指推理加速相关技术。", "en": "The process of a deployed model producing outputs from inputs; also refers to inference acceleration techniques."},
    "training": {"zh": "预训练，在海量数据上从头训练模型参数的阶段。", "en": "The stage of training a model from scratch on massive data."},
    "open-source": {"zh": "开源，模型权重公开、可自由使用与二次开发的发布方式。", "en": "Publishing model weights openly for free use and further development."},
    "robotics": {"zh": "机器人/具身智能，结合 AI 与物理实体完成现实世界任务的研究方向。", "en": "Robotics/embodied AI: research combining AI with physical bodies to perform real-world tasks."},
    "autonomous-driving": {"zh": "自动驾驶/智能驾驶，让车辆自主感知环境并做出行驶决策的技术。", "en": "Technology enabling vehicles to perceive their surroundings and make driving decisions autonomously."},
    "text-to-video": {"zh": "文生视频，根据文本描述生成视频片段的生成式 AI 技术。", "en": "Generative AI that creates video clips from text descriptions."},
    "text-to-image": {"zh": "文生图，根据文本描述生成图像的生成式 AI 技术。", "en": "Generative AI that creates images from text descriptions."},
    "voice": {"zh": "语音 AI，涵盖语音合成、语音识别与语音克隆等技术。", "en": "Voice AI covering speech synthesis, recognition and voice cloning."},
    "coding": {"zh": "AI 编程，用大模型辅助写代码、补全与代码生成的产品与技术。", "en": "Products and techniques using large models to assist writing, completing and generating code."},
    "search": {"zh": "AI 搜索，用大模型理解查询并综合答案的新型搜索引擎。", "en": "Search engines that use large models to understand queries and synthesize answers."},
    "wearable": {"zh": "智能眼镜等可穿戴 AI 设备，提供第一视角的语音交互体验。", "en": "Wearable AI devices such as smart glasses offering hands-free first-person voice interaction."},
    "chip": {"zh": "AI 芯片/算力，为训练与推理专门设计的处理器（GPU、TPU、NPU 等）。", "en": "AI chips/compute: processors specialized for AI training and inference, such as GPUs, TPUs and NPUs."},
    "regulation": {"zh": "AI 监管，各国对 AI 开发与使用的法律与政策框架（如欧盟 AI 法案）。", "en": "Laws and policy frameworks governing AI development and use, such as the EU AI Act."},
    "safety": {"zh": "AI 安全，防止模型滥用、失控与有害输出的研究与工程实践。", "en": "Research and engineering to prevent misuse, loss of control and harmful model outputs."},
    "copyright": {"zh": "版权/侵权，围绕训练数据来源与生成内容归属的版权争议。", "en": "Copyright disputes around training data sources and the ownership of generated content."},
    "funding": {"zh": "融资/投融资，AI 公司的资金募集与估值动向。", "en": "Fundraising rounds and valuations of AI companies."},
    "workflow": {"zh": "工作流，把多步骤 AI 任务编排成可复用流程，智能体场景中常用。", "en": "Orchestrating multi-step AI tasks into reusable pipelines, common in agent scenarios."},
    "edge-ai": {"zh": "端侧 AI，在手机、PC 等设备本地运行模型，兼顾隐私与低延迟。", "en": "Running models locally on devices such as phones and PCs for privacy and low latency."},
    "world-model": {"zh": "世界模型，在内部模拟环境动态、用于规划与预测的模型。", "en": "Models that internally simulate environment dynamics for planning and prediction."},
    "memory": {"zh": "长期记忆，让 AI 跨会话记住用户偏好与上下文的能力。", "en": "Enabling AI to retain user preferences and context across sessions."},
    "sandbox": {"zh": "沙盒，隔离运行不可信代码或代理的安全机制。", "en": "An isolation mechanism for safely running untrusted code or agents."},
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
    "智谱ai": "GLM", "智谱清言": "GLM",
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


# 归一化用：ASCII 非字母数字字符集（去首尾标点噪音；CJK 词整词保留）
_ASCII_PUNCT = "".join(chr(i) for i in range(128) if not chr(i).isalnum())

# ---------- 必须大写的技术缩写（lowercase → uppercase canonical）----------
# 常见技术缩写/品牌名在归一化后应统一为大写形式；覆盖 LLM 抽词的大小写不一致
# （如 "Gpu"/"gpu" → "GPU"、"Ui"/"ui" → "UI"）。
# 包含已在 _LEXICON 中的词条（glm/llm/rag 等）和未收录的通用缩写。
# 归一化流程中在别名查找之后应用：先走词典归并，再对结果做大写校正。
_UPPER_ACRONYMS = {
    # 未收录的通用技术缩写
    "api": "API",
    "ar": "AR",
    "aws": "AWS",
    "cd": "CD",
    "ci": "CI",
    "cli": "CLI",
    "cnn": "CNN",
    "css": "CSS",
    "cv": "CV",
    "devops": "DevOps",
    "dl": "DL",
    "gan": "GAN",
    "gcp": "GCP",
    "gpu": "GPU",
    "grpc": "gRPC",
    "hf": "HF",
    "html": "HTML",
    "http": "HTTP",
    "https": "HTTPS",
    "iaas": "IaaS",
    "ide": "IDE",
    "iot": "IoT",
    "json": "JSON",
    "ml": "ML",
    "mr": "MR",
    "nlp": "NLP",
    "nosql": "NoSQL",
    "npu": "NPU",
    "oss": "OSS",
    "paas": "PaaS",
    "rest": "REST",
    "rl": "RL",
    "rnn": "RNN",
    "saas": "SaaS",
    "sdk": "SDK",
    "sql": "SQL",
    "tpu": "TPU",
    "ui": "UI",
    "url": "URL",
    "vr": "VR",
    "xr": "XR",
    "yaml": "YAML",
}



def normalize_term(s):
    """任意词形 → canonical 键。单点收口，抽词/查询/详情页都用它。

    规则：strip/lower → 空白与下划线归一为单 '-' → 去首尾 ASCII 标点
    （LLM 抽词偶发 "GPT-5." / "(gpt-5)" 等噪音，CJK 词整词保留）→
    查别名表 → 保守去复数（仅 ASCII 且长度>3）→ 长度<2 或纯数字丢弃 →
    缩写大写校正（_UPPER_ACRONYMS：gpu→GPU / ui→UI / glm→GLM 等）。
    大小写无关：GPT-5 / gpt-5 / Gpt-5 都归一到 gpt-5；版本感知边界保留
    （内部 '.' 不动，gpt-5 ≠ gpt-5.5）。
    """
    if not s:
        return ""
    t = re.sub(r"[\s_]+", "-", str(s).strip().lower())
    t = re.sub(r"-{2,}", "-", t).strip("-")
    if not t:
        return ""
    # 去首尾 ASCII 标点（CJK 词整词保留：只处理 ord<128 的非字母数字字符）
    t = t.strip(_ASCII_PUNCT)
    if not t:
        return ""
    if t in _ALIAS:
        t = _ALIAS[t]
    else:
        # 保守去复数：仅纯 ASCII 词、长度>3、不以 ss 结尾
        if t.isascii() and len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            cand = t[:-1]
            if cand in _ALIAS:
                t = _ALIAS[cand]
    if len(t) < 2 or t.isdigit():
        return ""
    # 缩写大写校正：已知技术缩写统一为大写 canonical 形式
    return _UPPER_ACRONYMS.get(t, t)


def is_stopword(term):
    """通用热词停用判断：词形归一化后是否落在低价值通用词停用表。

    入参可为任意词形（内部先 normalize_term），但调用方在 hot path 上应
    传 canonical 键以避免重复归一。空串/无效词形返回 False。
    """
    canon = normalize_term(term)
    return bool(canon) and canon in _TERM_STOPWORDS


def _ci_surface_in_text(surface, text):
    """大小写不敏感定位表面形式在原文中的确切片段（None=未命中）。

    ASCII 表面沿用词典抽词同口径词边界（前后不能是字母/数字）+ 版本后缀
    防误匹配（"GPT-5.5" 不命中 gpt-5）；CJK 表面子串匹配。命中返回原文
    中的确切大小写片段。
    """
    if not surface or not text:
        return None
    if any(ord(c) >= 128 for c in surface):
        idx = text.casefold().find(surface.casefold())
        return text[idx:idx + len(surface)] if idx >= 0 else None
    m = re.search(
        r"(?<![a-z0-9])" + re.escape(surface) + r"(?!\.\d)(?![a-z0-9])",
        text, re.I)
    return m.group(0) if m else None


def case_match_original(keyword, text):
    """硬编码校验（需求 5）：提取的关键词必须与原文大小写完全一致。

    在原文（报道标题等）中大小写不敏感地查找关键词（含词典表面形式与
    空格/连字符变体），命中则返回原文中的确切大小写片段；未命中返回原词
    （关键词不在原文中，无从推导大小写，保持 canonical 形式）。纯 CJK
    关键词无大小写概念，原样返回。作为 LLM 抽词与词典抽词的收口校验，
    避免 normalize_term 的小写化把 "OpenClaw"/"GPT-5" 等大小写抹平。
    """
    if not keyword or not text:
        return keyword
    kw = str(keyword).strip()
    text = str(text)
    if not kw or not any(c.isascii() and c.isalpha() for c in kw):
        return kw
    cands, seen = [kw], {kw.casefold()}
    for s in _term_surfaces(kw):
        s = str(s or "").strip()
        if s and all(ord(c) < 128 for c in s) and s.casefold() not in seen:
            seen.add(s.casefold())
            cands.append(s)
    for cand in cands:
        hit = _ci_surface_in_text(cand, text)
        if hit is not None:
            return hit
    return kw


def extract_keywords_dict(title):
    """词典匹配抽词（零 LLM 成本）。无 API key 时的降级路径 + 历史回填用。

    对标题（可传多段拼接文本）做：ASCII 表面形式词边界匹配 + CJK 子串匹配，
    返回与原文大小写一致的表面形式列表（canonical 词键经 case_match_original
    对齐原文大小写），去重，上限 3 个；停用词（_TERM_STOPWORDS）不返回。
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
    # 停用词不进词池（即使词典命中也过滤，如 "llm"）
    # 需求 5：硬编码大小写校验——返回与原文大小写一致的表面形式
    return [case_match_original(c, text) for c in hits if not is_stopword(c)][:3]


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


def _title_key(title):
    """标题归一化去重键：strip + casefold + 连续空白压缩。

    同标题转载/镜像（不同 URL 同一篇报道，如 Yahoo Finance / The Motley Fool
    两处镜像）在关联列表里会连续重复展示。归一化标题作为去重键，命中即只保留
    首条（调用方需保证输入已按 published DESC, score DESC 排序，首条即
    score 最高者）。空/缺失/纯空白标题返回 None（不去重，保持原行为）。
    """
    if title is None:
        return None
    norm = re.sub(r"\s+", " ", str(title).strip().casefold())
    return norm or None


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
    # 停用词（低价值通用词）在此统一剔除：LLM 抽出的 "AI"/"模型" 等即使已写入
    # news_cards.keywords，也不会进入词池聚合与词-新闻关联（refresh_words 与
    # get_term_news 共用本函数）。
    return {canon for canon in (normalize_term(k) for k in raw)
            if canon and not is_stopword(canon)}


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
    # extract_keywords_dict 现在返回与原文大小写一致的表面形式；
    # 词聚合键仍需 canonical（大小写无关归并），此处归一回 canonical 键。
    return {c for c in (normalize_term(k) for k in extract_keywords_dict(text)) if c}


def _display_of(term, surfaces):
    """从命中表面形式里挑展示名：优先含大写的最长形式，否则按规则美化。"""
    best = ""
    for s in surfaces:
        if any(c.isupper() for c in s) and len(s) > len(best):
            best = s
    if best:
        return best
    # canonical 已是规范缩写形式的直接返回（LLM/RAG/GLM/GPU/UI/LoRA/MoE/xAI 等）
    _upper_vals = set(_UPPER_ACRONYMS.values())
    if term in _upper_vals:
        return term
    # display_overrides（来自 terms_canonical.json，最高优先级）
    _OVERRIDES = {
        "agents.md": "AGENTS.md",
        "agi": "AGI",
        "amd": "AMD",
        "aqua": "AQuA",
        "chatgpt": "ChatGPT",
        "cot": "CoT",
        "cuda": "CUDA",
        "glm": "GLM",
        "gpt-4o": "GPT-4o",
        "gpt-5": "GPT-5",
        "ipo": "IPO",
        "llm": "LLM",
        "lora": "LoRA",
        "mcp": "MCP",
        "moe": "MoE",
        "openclaw": "OpenClaw",
        "rag": "RAG",
        "rlhf": "RLHF",
        "tts": "TTS",
        "xai": "xAI"
    }
    if term in _OVERRIDES:
        return _OVERRIDES[term]
    if term.lower() in _OVERRIDES:
        return _OVERRIDES[term.lower()]
    # lexicon_display（来自 terms_canonical.json，品牌名/产品名展示名）
    _LEXICON_DISPLAY = {
        "agents.md": "AGENTS.md",
        "alibaba": "Alibaba",
        "anthropic": "Anthropic",
        "apple-intelligence": "Apple Intelligence",
        "baidu": "Baidu",
        "bytedance": "ByteDance",
        "deepseek": "DeepSeek",
        "google": "Google",
        "google-deepmind": "Google DeepMind",
        "huawei": "Huawei",
        "huggingface": "Hugging Face",
        "meta-ai": "Meta AI",
        "microsoft": "Microsoft",
        "nvidia": "NVIDIA",
        "openai": "OpenAI",
        "ross-harness": "ROSS Harness",
        "tencent": "Tencent",
        "tsinghua": "Tsinghua"
    }
    if term in _LEXICON_DISPLAY:
        return _LEXICON_DISPLAY[term]
    if term.lower() in _LEXICON_DISPLAY:
        return _LEXICON_DISPLAY[term.lower()]

    # 词典 canonical 的常见美化：按 '-' 分词，已知缩写全大写
    UPPER = {"gpt", "ai", "kv", "tts"}
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


def refresh_words(all_cards, model_cards, fetched_at=None,
                  term_translator=None, term_explainer=None):
    """一轮刷新的词聚合：关联 → 归并 → 打分 → 快照 → 写 words.json。

    输入：all_cards（dims 当轮全量新闻卡，含 keywords）、
          model_cards（tracker 当轮 HF 模型卡）、
          term_translator（可选：中文热词 → 英文展示名 的批量翻译回调，
          入参为中文展示名列表，返回 {原文: 英文} 字典；无则英文页回退中文）、
          term_explainer（可选：热词 → 双语解释 的批量生成/优化回调，动态词典
          资产维护用。入参为 [{canon, display, titles, existing_zh, existing_en}]，
          返回 {canon: {"zh":..., "en":...}}；无则解释列不写，详情页模板兜底）。
    数据源：新闻关联以**历史库全量扫描**为准（跨周期累积 total_mentions /
    7 天热窗），当轮 all_cards 只用于本周期快照 news_cnt/score_sum。
    失败静默，绝不阻塞 dims 刷新主流程。
    """
    if not _DB_OK:
        return
    try:
        _refresh_words_inner(all_cards or [], model_cards or [],
                             fetched_at or int(datetime.datetime.now().timestamp()),
                             term_translator, term_explainer)
    except Exception:
        pass


def _refresh_words_inner(all_cards, model_cards, fetched_at,
                         term_translator=None, term_explainer=None):
    now = _now_iso()
    today = datetime.date.today()
    hot_cutoff = (today - datetime.timedelta(days=HOT_WINDOW_DAYS)).isoformat()
    cycle = datetime.datetime.now().strftime("%Y-%m-%d-%H")  # 容器 TZ=Asia/Shanghai

    # ---- 1. HF 模型词归一化 + 元数据（底模键归并变体，与新闻关键词碰撞合并）----
    hf_terms = {}  # canon → {display, hf_meta}
    for mc in model_cards:
        canon = _hf_canon(mc)
        # 停用词不进池：HF 模型名归一后若落在低价值通用词（如 "model"），
        # 不占词池名额（停止表只含极通用词，不会误伤真实模型名）。
        if not canon or is_stopword(canon):
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
                        "win7_cnt": 0,
                    })
                    url = r["url"] or ""
                    if url not in a["urls"]:
                        a["urls"].add(url)
                        a["mentions"] += 1
                    pub = r["published"] or ""
                    if pub:
                        a["pubs"].add(pub)
                    if pub >= hot_cutoff:
                        # 热窗内按报道新鲜度加权：今日热词（最近 1-3 天）不被
                        # 存量累计分埋没（hot 展示与排序同口径，见 _hot_recency_weight）
                        a["hot_score"] += int(r["score"] or 0) * _hot_recency_weight(pub, today)
                        # 近 7 天滑动窗口报道数：rise 环比口径——
                        # 「近一周声量」相对上一刷新时刻是否增长，避免用单个刷新
                        # 轮次的瞬时 cur_cnt 环比把「发布日已进池」的词误判为降温。
                        # （url 已在前一行加入 urls，这里直接计数即可）
                        a["win7_cnt"] += 1
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
        # top news 与 get_term_news 同序（hot 降序，hot 缺失回退 score；
        # 本路径只投影 score，故按 score 降序 + published 降序），保证卡片
        # 内嵌预览与「展开更多」列表顺序一致，展开时不重新排序。
        a["top"].sort(key=lambda x: x["card"].get("published") or "", reverse=True)
        a["top"].sort(key=lambda x: -x["score"])
        # 同标题转载/镜像（不同 URL 同一篇报道）按归一化标题去重：保留排序后
        # 首条（即 score 最高者），与详情页 get_term_news 同口径，词卡 top_news
        # 不出现同标题两条。title_zh or title_en（title_zh 构造时已回退原始
        # title），空标题不去重。去重须在 [:3] 截断前做，重复项不占展示位。
        deduped = []
        seen_titles = set()
        for t in a["top"]:
            tkey = (_title_key(t["card"].get("title_zh"))
                    or _title_key(t["card"].get("title_en")))
            if tkey is not None:
                if tkey in seen_titles:
                    continue
                seen_titles.add(tkey)
            deduped.append(t)
        a["top"] = [t["card"] for t in deduped[:3]]

    # ---- 3. 归并 HF 词（无新闻命中也入池，origin=hf）----
    for canon, meta in hf_terms.items():
        agg.setdefault(canon, {
            "mentions": 0, "hot_score": 0, "urls": set(), "dims": {},
            "top": [], "latest_pub": "", "earliest_pub": "9999",
            "pubs": set(), "cur_cnt": 0, "cur_score": 0, "cur_signal": 0.0,
            "win7_cnt": 0,
        })

    # ---- 4. 读旧 terms 表（保留 first_seen_at / display 演进）----
    # 流式读取，不 fetchall 物化；用完后及时释放，避免与第 7 步 final_rows 双份驻留。
    # 键按 canonical 归一：早期版本可能落过混合大小写行（"GPT-5"），归并后
    # 与当前 canonical 键同一条目，避免被误判为全新词。
    old = {}
    try:
        conn = _conn()
        for r in conn.execute("SELECT * FROM terms"):
            old[normalize_term(r["term"]) or r["term"]] = dict(r)
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

    # ---- 5.6 英文展示名（display_en）----
    # 词典外 LLM 抽取的中文词（债务融资/并购/自动驾驶卡车等）没有英文形态，
    # 英文页会显示中文热词。有 term_translator 时批量翻译；失败/无回调降级为空，
    # 前端回退 term（英文页仍有中文，但不阻塞）。
    if term_translator:
        try:
            _needs = {}
            for canon in kept:
                _disp = (hf_terms.get(canon, {}).get("display")
                         or _display_of(canon, [canon]))
                if _disp and re.search(r"[\u4e00-\u9fff]", _disp):
                    _needs[canon] = _disp
            if _needs:
                _translated = term_translator(list(_needs.values())) or {}
                for canon, disp in _needs.items():
                    _en = _translated.get(disp)
                    if isinstance(_en, str) and _en.strip():
                        kept[canon]["display_en"] = _en.strip()[:80]
        except Exception:
            pass

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

            # rise：近 7 天滑动窗口报道数环比（口径 2026-09-01）
            # 之前用单刷新轮次 cur_cnt 环比：发布日当天进池的词（如 Openclaw
            # 8-31 发布），下一轮 cur_cnt 从 2→1 就被误判为「降温」（-0.5），
            # 排 rise 榜 188/200；而 Token/Apple 等「本轮从 0→1」的词却靠冷启动
            # 霸榜。改用 win7_cnt（近 7 天窗口内报道数，随刷新滑动）：语义变成
            # 「近一周声量相对上一刷新时刻是否增长」，发布日进池的词窗口值稳定
            # 不为负，新增报道持续进入窗口的词自然上升。
            m_cur = a["win7_cnt"]
            try:
                prev = conn.execute(
                    "SELECT win7_cnt FROM term_snapshots "
                    "WHERE term=? AND cycle<>? ORDER BY cycle DESC LIMIT 1",
                    (canon, cycle)).fetchone()
            except Exception:
                prev = None
            if prev:
                m_prev = prev["win7_cnt"]
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

            # display_en：本轮翻译有结果才覆盖；翻译失败/未命中时保留旧值，
            # 否则英文页热词会因某轮 LLM 限流（429/1305）集体回退中文（churn）。
            display_en = (kept[canon].get("display_en")
                          or o.get("display_en") or "")

            conn.execute(
                """INSERT INTO terms (term, display, display_zh, display_en, origin,
                       first_seen_at, last_seen_at, total_mentions, hf_json,
                       cur_hot, cur_rise, cur_novelty)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(term) DO UPDATE SET
                       display=excluded.display, display_zh=excluded.display_zh,
                       display_en=excluded.display_en,
                       origin=excluded.origin, last_seen_at=excluded.last_seen_at,
                       total_mentions=excluded.total_mentions,
                       hf_json=excluded.hf_json, cur_hot=excluded.cur_hot,
                       cur_rise=excluded.cur_rise, cur_novelty=excluded.cur_novelty""",
                (canon, display, display_zh, display_en, origin,
                 first_seen, now, a["mentions"],
                 json.dumps(hf_meta, ensure_ascii=False) if hf_meta else "",
                 hot, round(rise, 4), novelty),
            )
            conn.execute(
                """INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, score_sum, signal_sum)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(term, cycle) DO UPDATE SET
                       news_cnt=excluded.news_cnt, win7_cnt=excluded.win7_cnt,
                       score_sum=excluded.score_sum, signal_sum=excluded.signal_sum""",
                (canon, cycle, a["cur_cnt"], a["win7_cnt"],
                 a["cur_score"], round(a["cur_signal"], 1)),
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

    # ---- 6.5 解释批次（动态词典资产：新词生成 + 存量解释 >24h 低频优化）----
    # 静态 _EXPLANATIONS 词不进批次（人工精编，存量不改）；无 term_explainer
    # （无 LLM key）或任一步失败静默跳过，详情页由模板兜底解释保证有内容。
    # 已有解释 >24h 才进优化批次（附现有解释 + 最新代表报道标题作上下文）；
    # 返回文本未变化 → 仅刷新 explain_updated_at（标记已检查，≤1 次/天/词）。
    if term_explainer and kept:
        try:
            now_dt = datetime.datetime.now()
            needs = []  # (canon, display, titles, existing_zh, existing_en, hot)
            with _db_lock:
                conn = _conn()
                for canon in kept:
                    if canon in _EXPLANATIONS:
                        continue
                    row = conn.execute(
                        "SELECT display, explain_zh, explain_en, "
                        "explain_updated_at, cur_hot FROM terms WHERE term=?",
                        (canon,)).fetchone()
                    if not row:
                        continue
                    existing_zh = row["explain_zh"] or ""
                    existing_en = row["explain_en"] or ""
                    if existing_zh and existing_en:
                        updated_at = row["explain_updated_at"] or ""
                        try:
                            ts = datetime.datetime.fromisoformat(
                                updated_at[:19])
                            if (now_dt - ts).total_seconds() <= 24 * 3600:
                                continue  # 24h 内刚检查/生成过，不重复
                        except (ValueError, TypeError):
                            pass  # 时间异常视为待检查
                    titles = [n.get("title_zh") or n.get("title_en") or ""
                              for n in (kept[canon].get("top") or [])[:3]]
                    needs.append((canon, row["display"] or canon,
                                  [t for t in titles if t],
                                  existing_zh, existing_en,
                                  row["cur_hot"] or 0))
                conn.close()
            # 每轮解释批次上限：按热度降序取前 N 个（最热优先），
            # 控制 LLM 批次规模与刷新锁占用时间；存量无解释词后续轮次回填。
            needs.sort(key=lambda x: x[5], reverse=True)
            needs = needs[:EXPLAIN_BATCH_MAX_WORDS]
            if needs:
                results = term_explainer([
                    {"canon": c, "display": d, "titles": ts,
                     "existing_zh": ez, "existing_en": ee}
                    for c, d, ts, ez, ee, _h in needs
                ]) or {}
                if results:
                    now_iso = now_dt.isoformat(timespec="seconds")
                    with _db_lock:
                        conn = _conn()
                        for canon, _d, _ts, ez, ee, _h in needs:
                            res = results.get(canon) or {}
                            new_zh = (res.get("zh") or "").strip()
                            new_en = (res.get("en") or "").strip()
                            if not (new_zh and new_en):
                                continue
                            if new_zh != ez or new_en != ee:
                                # 内容有改进才写文本；原样返回仅刷新检查时间
                                conn.execute(
                                    "UPDATE terms SET explain_zh=?, explain_en=?, "
                                    "explain_updated_at=? WHERE term=?",
                                    (new_zh[:200], new_en[:300], now_iso, canon))
                            else:
                                conn.execute(
                                    "UPDATE terms SET explain_updated_at=? "
                                    "WHERE term=?", (now_iso, canon))
                        conn.commit()
                        conn.close()
        except Exception as e:
            print(f"[terms][explain] 解释批次失败: {type(e).__name__}: {e}",
                  flush=True)

    # ---- 7. 组装词卡写 words.json（只读回 kept 词，避免整表物化）----
    try:
        conn = _conn()
        final_rows = {}
        for r in conn.execute(
                "SELECT term, display, display_zh, display_en, origin, "
                "first_seen_at, last_seen_at, total_mentions, hf_json, "
                "cur_hot, cur_rise, cur_novelty FROM terms"):
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
            "display_en": row["display_en"] or "",
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
        elif lang == "en" and wc.get("display_en"):
            # 中文热词的英文展示名（词典外词由刷新期 LLM 翻译，缺失回退 term）
            wc["term_display"] = wc["display_en"]
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


def get_term_explanation(term, lang="zh"):
    """按 canonical 键返回热词解释，详情页「这是什么」。三级取词：

    1. 静态 `_EXPLANATIONS` 词典（人工精编，优先，LLM 不覆盖）；
    2. `terms` 表 `explain_zh/explain_en`（动态词典资产，LLM 每轮刷新生成/优化）；
    3. 都未命中返回空串（调用方模板兜底，保证每个热词页都有解释块）。

    lang 为 "zh"/"en"，其他取值回退 zh；词形会先归一（别名/大小写均可命中）；
    不抛异常。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    canon = normalize_term(term)
    if not canon:
        return ""
    entry = _EXPLANATIONS.get(canon)
    if entry:
        return entry.get(lang) or entry.get("zh") or ""
    if _DB_OK:
        try:
            conn = _conn()
            r = conn.execute(
                "SELECT explain_zh, explain_en FROM terms WHERE term=?",
                (canon,)).fetchone()
            conn.close()
            if r:
                return ((r["explain_zh"] if lang == "zh" else r["explain_en"])
                        or r["explain_zh"] or r["explain_en"] or "")
        except Exception:
            pass
    return ""


def get_term_news(term, limit=50, lang="zh"):
    """词 → 关联报道（canonical keywords + 标题命中兜底）。

    新卡的 ``keywords`` 是 canonical JSON，历史卡可能没有该列、列值为
    ``[]``，或仍保存旧的表面形式。因此 SQL 只负责收集候选行，最终的
    关联判断统一在 Python 中做 canonical/别名归一和版本感知边界匹配。
    返回与 dims 卡同 schema 的投影卡列表：去重后按 hot 降序（hot 缺失或
    为 0 回退 score，与 ``_row_to_card`` 兜底同口径），同 hot 按 published
    降序稳定排序；排序先于 limit 截断。
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
    # HF 模型词：补充 full_id 末段作为表面形式（与 refresh_words 中 _match_hf_term 同口径）
    try:
        conn_hf = _conn()
        hf_row = conn_hf.execute(
            "SELECT hf_json FROM terms WHERE term=?", (canon,)).fetchone()
        conn_hf.close()
        if hf_row and hf_row["hf_json"]:
            hf_meta = json.loads(hf_row["hf_json"])
            full_id = hf_meta.get("full_id", "")
            if full_id:
                # full_id 形如 "Qwen/Qwen3.8-27B"，末段是模型 display 名
                last_seg = full_id.split("/")[-1]
                if last_seg and last_seg.lower() not in [s.lower() for s in surfaces]:
                    surfaces.append(last_seg)
    except Exception:
        pass

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
        seen_titles = set()
        for r in rows:
            # Known aliases (GPT5, GPT 5, 智能体, …) are handled by
            # _term_surfaces, including the canonical spelling itself.
            keywords_match = canon in _keyword_canons(r["keywords"])
            titles = [str(r[f] or "") for f in title_fields]
            title_match = any(_title_matches_term(t, surfaces) for t in titles)
            if keywords_match or title_match:
                # 同标题转载/镜像（不同 URL 同一篇报道）按归一化标题去重：
                # rows 已按 published DESC, score DESC 排序，保留首条（同日
                # 期下即 score 最高者）；title_zh/title_en/原始 title 取首个
                # 非空（先解码再归一，与展示卡同口径）。去重在排序与 limit
                # 截断之前做，同标题第二份不会挤掉有效卡。空标题不去重（保持
                # 原行为）。
                tkey = None
                for field in ("title_zh", "title_en", "title"):
                    if field in title_fields:
                        k = _title_key(decode_html_entities(r[field]))
                        if k:
                            tkey = k
                            break
                if tkey is not None:
                    if tkey in seen_titles:
                        continue
                    seen_titles.add(tkey)
                out.append(r)

        def _card_hot(r):
            # 与 _row_to_card 的 hot 兜底同口径：hot 缺失/为 0 → score。
            try:
                hot = r["hot"]
            except (KeyError, IndexError):
                hot = 0
            return hot or r["score"] or 0

        # 详情页/展开列表按热度排序：hot 降序（hot 缺失回退 score），同 hot
        # 按 published 降序稳定排序；排序在 limit 截断之前（去重也已先行，
        # 因此 limit 只统计去重后的有效卡）。
        out.sort(key=lambda r: (_card_hot(r), r["published"] or ""), reverse=True)
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
                    """INSERT INTO term_snapshots (term, cycle, news_cnt, win7_cnt, score_sum, signal_sum)
                       VALUES (?,?,?,?,?,0)
                       ON CONFLICT(term, cycle) DO NOTHING""",
                    (term, f"{pub}-00", s["cnt"], s["cnt"], s["score_sum"]))
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
