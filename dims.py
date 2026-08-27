"""
维度热词层 —— 聚合多源 RSS + Algolia HN，产出「维度热词卡」。

与 tracker.py 的区别：
- tracker.py 只覆盖「开源模型」一个维度（靠 HF trendingScore）。
- dims.py 覆盖 AI 科技圈全维度：模型发布 / 产品发布 / 研究论文 / 投融资 /
  行业动态 / 其他。数据源是各家官方 RSS + arXiv + Algolia HN + Reddit，
  天然带官方原文链接（如 OpenAI 发布 GPT-5 → openai.com/index/...）。
- 维度打标用 DeepSeek LLM（轻量、批量、temperature=0.3），LLM 只负责分类，
  不碰链接——链接来自 RSS 原文，保证「链接到官方原文」这一硬要求。

设计原则（与 tracker.py 一致）：
- 请求路径只读文件缓存（秒回）；抓取 + LLM 打标由后台预热线程定时做。
- 多源 RSS 并发拉取，单源失败不阻塞（本地网络不可达的源静默跳过，
  云主机上自然生效）。
- LLM 失败降级：按 RSS 源类型给默认维度（OpenAI→产品发布，arXiv→研究论文，
  TechCrunch→行业动态），保证即便 DeepSeek 挂了也有热词可看。
"""

import os
import re
import json
import time
import math
import hashlib
import threading
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import (CACHE_DIR, DEEPSEEK_API_KEY, DEEPSEEK_MODEL,
                    DIMS_REFRESH_HOURS, NEWS_HISTORY_DAYS, NEWS_HISTORY_LIMIT)

try:
    import news_store  # 历史持久化（issue 6）；失败不阻塞，get_news_cards 自动降级
except Exception:
    news_store = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
TIMEOUT = 10

# ---------- DeepSeek LLM 配置 ----------
# 从 config 统一读取（消除重复定义，便于环境分层：worktree 不设 key 走 Mock 降级，
# dev 回归 / 生产才设 key 真调 LLM）。
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# 固定维度枚举（LLM 只能从中选；LLM 失败时也用它做默认降级）
# 维度 key 始终是中文（canonical），前端按语言取 label / label_en。
DIMENSIONS = ["模型发布", "产品发布", "研究论文", "投融资", "行业动态", "其他"]
DIMENSIONS_EN = {
    "模型发布": "Model Releases",
    "产品发布": "Product Launches",
    "研究论文": "Research Papers",
    "投融资":   "Funding",
    "行业动态": "Industry News",
    "其他":     "Other",
}

# ---------- 文件缓存 ----------
DIMS_CACHE_FILE = os.path.join(CACHE_DIR, "dims.json")
DIMS_REFRESH_INTERVAL = 3600    # 后台预热周期：1 小时（新闻类更新较快）
DIMS_RETRY_INTERVAL = 300       # 预热失败后快速重试：5 分钟
DIMS_CACHE_TTL = 7200           # 文件缓存兜底有效期：2 小时

_file_cache = {}
_file_cache_lock = threading.Lock()
_file_cache_loaded = False
_file_cache_mtime = 0  # 上次加载时 dims.json 的 mtime；用于跨 worker 感知磁盘刷新


def _load_file_cache(force=False):
    """加载 dims.json 到内存缓存。

    单次加载后，通过比较磁盘 mtime 决定是否需要重新读盘——这样后台刷新线程
    （或另一个 gunicorn worker）写出新 dims.json 后，所有 worker 都能在下次
    请求时自动收敛到最新缓存，避免长驻 worker 永远持有旧数据。
    """
    global _file_cache_loaded, _file_cache_mtime
    with _file_cache_lock:
        try:
            cur_mtime = os.path.getmtime(DIMS_CACHE_FILE)
        except OSError:
            cur_mtime = 0
        # 已加载且磁盘未变化 → 直接复用内存缓存
        if _file_cache_loaded and not force and cur_mtime == _file_cache_mtime:
            return
        if cur_mtime:
            try:
                with open(DIMS_CACHE_FILE, "r", encoding="utf-8") as f:
                    _file_cache.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        _file_cache_loaded = True
        _file_cache_mtime = cur_mtime


def _save_file_cache():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = DIMS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_file_cache, f, ensure_ascii=False)
        os.replace(tmp, DIMS_CACHE_FILE)
    except OSError:
        pass


def _file_cache_get():
    _load_file_cache()
    with _file_cache_lock:
        ent = _file_cache.get("dims")
        if ent:
            return ent.get("data"), ent.get("fetched_at", 0)
    return None, 0


def _file_cache_set(data, fetched_at):
    with _file_cache_lock:
        _file_cache["dims"] = {"data": data, "fetched_at": fetched_at}
    _save_file_cache()
    # 写盘后立即同步 mtime，避免本 worker 下次 _load_file_cache 误判磁盘变化而重读
    global _file_cache_mtime
    try:
        with _file_cache_lock:
            _file_cache_mtime = os.path.getmtime(DIMS_CACHE_FILE)
    except OSError:
        pass


# ---------- RSS 源定义 ----------
# 每个源：name / feed_url / region / default_dim（LLM 失败时降级用）。
# 本地不可达的源在云主机复测后，按结果分三类：
# ① 官方 RSS 路径修正后可用 → 直接换 URL（DeepMind/NVIDIA/Stability/Databricks）。
# ② 厂商无 RSS（Anthropic/Meta AI）→ 用 Google News 聚合 feed 按厂商 query；
#   GN 的 <link> 是中转页、<source url> 只是媒体域名，故 official_url 取媒体域名
#   （降级：不再是具体文章直链，但仍是真实报道方，非 Google News 自身）。
# ③ 补充主流 AI 媒体 RSS（MIT Tech Review / VentureBeat / The Gradient）增加多样性。
RSS_SOURCES = [
    # —— 厂商官方博客（链接直指官方原文）——
    {"name": "OpenAI",       "feed": "https://openai.com/news/rss.xml",                            "region": "国际", "default_dim": "产品发布", "lang": "en"},
    {"name": "TechCrunch AI","feed": "https://techcrunch.com/category/artificial-intelligence/feed/", "region": "国际", "default_dim": "行业动态", "lang": "en"},
    {"name": "HF Blog",      "feed": "https://hf-mirror.com/blog/feed.xml",                        "region": "国际", "default_dim": "模型发布", "lang": "en"},
    {"name": "arXiv cs.AI",  "feed": "https://export.arxiv.org/rss/cs.AI",                         "region": "国际", "default_dim": "研究论文", "lang": "en"},
    {"name": "Microsoft AI", "feed": "https://www.microsoft.com/en-us/ai/blog/rss/",               "region": "国际", "default_dim": "产品发布", "lang": "en"},
    {"name": "DeepMind",     "feed": "https://blog.google/technology/ai/rss/",                     "region": "国际", "default_dim": "研究论文", "lang": "en"},
    {"name": "NVIDIA",       "feed": "https://blogs.nvidia.com/feed/",                             "region": "国际", "default_dim": "行业动态", "lang": "en"},
    {"name": "Stability AI", "feed": "https://stability.ai/news-updates/rss.xml",                  "region": "国际", "default_dim": "模型发布", "lang": "en"},
    {"name": "Databricks",   "feed": "https://www.databricks.com/rss.xml",                         "region": "国际", "default_dim": "行业动态", "lang": "en"},
    # —— 主流 AI 媒体（增加报道视角多样性，链接直指原文）——
    {"name": "MIT TechReview", "feed": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "region": "国际", "default_dim": "行业动态", "lang": "en"},
    {"name": "VentureBeat AI", "feed": "https://venturebeat.com/category/ai/feed/",                "region": "国际", "default_dim": "投融资", "lang": "en"},
    {"name": "The Gradient",   "feed": "https://thegradient.pub/rss/",                             "region": "国际", "default_dim": "研究论文", "lang": "en"},
    # —— 无官方 RSS 的厂商 → Google News 聚合（official_url 为媒体域名，非文章直链）——
    {"name": "Anthropic (GN)", "feed": "https://news.google.com/rss/search?q=Anthropic+when:3d&hl=en-US&gl=US&ceid=US:en",  "region": "国际", "default_dim": "产品发布", "is_gnews": True, "lang": "en"},
    {"name": "Meta AI (GN)",   "feed": "https://news.google.com/rss/search?q=%22Meta+AI%22+when:3d&hl=en-US&gl=US&ceid=US:en", "region": "国际", "default_dim": "产品发布", "is_gnews": True, "lang": "en"},
    # —— 国内 AI 一手媒体（国内厂商无 RSS，用主流媒体官方 RSS 覆盖）——
    {"name": "量子位",     "feed": "https://www.qbitai.com/feed",   "region": "国内", "default_dim": "行业动态", "lang": "zh"},
    {"name": "InfoQ中文",  "feed": "https://www.infoq.cn/feed",     "region": "国内", "default_dim": "行业动态", "lang": "zh"},
    {"name": "极客公园",   "feed": "https://www.geekpark.net/rss",  "region": "国内", "default_dim": "产品发布", "lang": "zh"},
    {"name": "少数派",     "feed": "https://sspai.com/feed",        "region": "国内", "default_dim": "产品发布", "lang": "zh"},
]

# 每个 RSS 源最多取前 N 条（控制 LLM 批次大小 + 多样性）
PER_SOURCE_LIMIT = 6


def _norm_date(raw):
    """把 RSS 里的各种日期串统一成 YYYY-MM-DD。

    兼容：RFC822（OpenAI/TechCrunch 的 "Thu, 20 Aug 2026 ..."）、
    ISO 8601（arXiv 的 "2026-08-20T..."、Atom 的同款）。
    解析失败返回原串截断 10 字（保底不崩）。
    """
    if not raw:
        return ""
    raw = raw.strip()
    # 先试 RFC822（email.utils）
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    # 再试 ISO 8601（arXiv/Atom）
    try:
        s = raw[:19]  # 截到秒，避免带时区/毫秒的尾巴
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    # 退化：截断 10 字（如 "2026-08-20" 原样保留）
    return raw[:10]


def _strip_cdata(s):
    """去掉 RSS/Atom 标题里的 CDATA 包裹 + 首尾空白。

    国内源（geekpark）把标题写成 '\\n  <![CDATA[...]]>\\n  '，CDATA 前还有缩进，
    旧正则的非贪婪可选组匹配不到，导致 '<![CDATA[' 泄漏进标题，污染 HN 查询。
    统一在这里处理：剥 CDATA、压空白。
    """
    s = s.strip()
    m = re.match(r"<!\[CDATA\[(.*?)\]\]>", s, re.S)
    if m:
        s = m.group(1)
    return s.strip()


def _parse_rss(xml_text, src):
    """解析 RSS/Atom，统一输出事件卡列表。

    兼容两种格式：
    - RSS 2.0（OpenAI/TechCrunch/arXiv）：<item> 内有 <title>/<link>/<pubDate>。
    - Atom（HF Blog）：<entry> 内 <title> + <link href=...> + <published>。

    返回每条：{title, url, source, region, published, default_dim}。
    """
    items = []
    # 优先按 RSS item 解析，没有再按 Atom entry
    blocks = re.findall(r"<item>(.*?)</item>", xml_text, re.S)
    is_atom = False
    if not blocks:
        blocks = re.findall(r"<entry>(.*?)</entry>", xml_text, re.S)
        is_atom = True

    for b in blocks[:PER_SOURCE_LIMIT]:
        if is_atom:
            title_m = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
            # Atom link：<link href="..."/> 或 <link>text</link>
            link_m = re.search(r'<link[^>]*href="([^"]+)"', b, re.S) or \
                     re.search(r"<link>(.*?)</link>", b, re.S)
            pub_m = re.search(r"<published>(.*?)</published>", b, re.S) or \
                    re.search(r"<updated>(.*?)</updated>", b, re.S)
        else:
            title_m = re.search(r"<title>(.*?)</title>", b, re.S)
            link_m = re.search(r"<link>(.*?)</link>", b, re.S)
            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", b, re.S)

        title = re.sub(r"\s+", " ", _strip_cdata(title_m.group(1))).strip() if title_m else ""
        url = link_m.group(1).strip() if link_m else ""
        if not title or not url:
            continue
        # arXiv link 常带 #abs=... 后缀，去掉
        url = url.split("#")[0]
        # Google News 聚合源：title 末尾常带 " - 媒体名"，去掉更干净；
        # <link> 是 GN 中转页（非原文直链），<source url> 只是媒体域名。
        # 用媒体域名 + title 末尾媒体名做 official_url（降级为报道方，非文章直链）。
        if src.get("is_gnews"):
            # GN 标题形如 "Scoop: ... - Axios"，提取末尾媒体名
            src_name_m = re.search(r"\s-\s([^\-]+)$", title)
            media_name = src_name_m.group(1).strip() if src_name_m else ""
            src_tag_m = re.search(r'<source[^>]*url="([^"]+)"', b, re.S)
            media_domain = src_tag_m.group(1).strip() if src_tag_m else ""
            # 去掉标题里的 " - 媒体名" 后缀
            if media_name and title.endswith(" - " + media_name):
                title = title[: -(len(media_name) + 3)].strip()
            # official_url 优先用媒体域名，便于用户溯源到报道方
            if media_domain:
                url = media_domain
            # source 标注成实际媒体名，而非 "Anthropic (GN)"
            if media_name:
                source_label = media_name
            else:
                source_label = src["name"]
        else:
            source_label = src["name"]

        published = _norm_date(pub_m.group(1).strip() if pub_m else "")
        items.append({
            "title": title[:200],   # 截断超长标题，控 LLM token
            "url": url,
            "source": source_label,
            "region": src["region"],
            "published": published,
            "default_dim": src["default_dim"],
            "lang": src.get("lang", "en"),   # 源语言，供 LLM 判断原生/外文
        })
    return items


def fetch_one_rss(src):
    """抓单个 RSS 源，失败返回 []（不抛——网络不可达的源静默跳过）。"""
    try:
        r = requests.get(src["feed"], headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        # 显式 UTF-8 解码：部分源（VentureBeat）返回 text/xml 无 charset，
        # requests 按 HTTP 规范默认 ISO-8859-1 解码，但正文实为 UTF-8，
        # em-dash ——（\xe2\x80\x94）会被解成 â€"（mojibake）。
        # RSS/Atom 正文均为 UTF-8，按 UTF-8 解 r.content 最稳。
        return _parse_rss(r.content.decode("utf-8", errors="replace"), src)
    except Exception:
        return []


def fetch_all_rss():
    """并发拉所有 RSS 源，合并去重（按 url），返回事件卡列表。"""
    all_items = []
    seen_urls = set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one_rss, s): s for s in RSS_SOURCES}
        for fut in as_completed(futures):
            for it in fut.result():
                if it["url"] in seen_urls:
                    continue
                seen_urls.add(it["url"])
                all_items.append(it)
    return all_items


# ---------- 社区热度增强（HN + Reddit → 复合热度分）----------
HN_API = "https://hn.algolia.com/api/v1/search"

# 营销前缀（HN/Reddit 查询前去掉，提高召回相关性）
_PREFIX_RE = re.compile(r"^\s*(introducing|announcing|meet|new|the|a|an)\b[:\s]+",
                        re.IGNORECASE)


def _has_cjk(s):
    """字符串是否含 CJK 字符（中文标题在 HN/Reddit 英文社区召回低）。"""
    return bool(re.search(r"[一-鿿]", s))


def _clean_title(title):
    """构造 HN/Reddit 查询串：去营销前缀，英文取前 12 词，中文取前 6 字。

    旧实现用 split()[:8] —— 中文无空格，split 返回整条标题为一个 token，
    Algolia 长查询召回为 0。中文按字符截断 + Algolia 标点分词可正常召回。
    """
    s = _PREFIX_RE.sub("", title).strip()
    if _has_cjk(s):
        return s[:6]
    return " ".join(s.split()[:12])


def _hn_points(title, url=None):
    """用事件标题查 Algolia HN，拿热度分（points）。

    改进（修复旧实现 79% 返回 0 的问题）：
    - 查询串用 _clean_title（CJK 取前 6 字，英文取前 12 词）。
    - hitsPerPage=5，遍历所有 hit 取满足相关性条件的最大 points：
      hit.url 与 official_url 同 host，或标题共享 ≥2 实义词，则采用；
      无任何相关命中时取全部 hit 最大 points 作软信号（优于 0）。
    - numericFilters=created_at_i > now-30d，只计近 30 天帖（新闻有时效）。
    失败静默返回 0（热度只是排序加分项，非必需）。
    """
    q = _clean_title(title)
    if not q:
        return 0
    try:
        now = int(time.time())
        params = {
            "query": q, "tags": "story", "hitsPerPage": 5,
            "numericFilters": f"created_at_i>{now - 30*86400}",
        }
        r = requests.get(HN_API, params=params, headers=HEADERS, timeout=8)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        if not hits:
            return 0
        # 传入 url 的 host 用于相关性校验
        url_host = ""
        if url:
            try:
                from urllib.parse import urlparse
                url_host = urlparse(url).netloc.lower()
            except Exception:
                url_host = ""
        # 标题实义词集合（去停用词，长度>2）
        STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with",
                "is", "are", "new", "via", "from", "how", "why", "what"}
        my_words = {w for w in re.findall(r"[A-Za-z]{3,}", title.lower())
                    if w not in STOP}
        best_rel = 0
        best_soft = 0
        for h in hits:
            pts = h.get("points", 0) or 0
            best_soft = max(best_soft, pts)
            # 相关性 1：url host 匹配
            h_url = h.get("url") or ""
            h_host = ""
            if h_url:
                try:
                    h_host = urlparse(h_url).netloc.lower()
                except Exception:
                    h_host = ""
            if url_host and h_host and (url_host == h_host or
                                        url_host.endswith(h_host) or
                                        h_host.endswith(url_host)):
                best_rel = max(best_rel, pts)
                continue
            # 相关性 2：标题共享 ≥2 实义词
            h_title = h.get("title") or ""
            h_words = {w for w in re.findall(r"[A-Za-z]{3,}", h_title.lower())
                       if w not in STOP}
            if my_words and len(my_words & h_words) >= 2:
                best_rel = max(best_rel, pts)
        return best_rel if best_rel > 0 else best_soft
    except Exception:
        pass
    return 0


# Reddit 公共 JSON 搜索无需鉴权（pushshift 已停用）。
# 按 dimension 选一个 subreddit 探测，避免每卡扇出多个请求。
_REDDIT_SUB = {
    "模型发布": "LocalLLaMA",
    "产品发布": "artificial",
    "研究论文": "MachineLearning",
    "投融资":   "artificial",
    "行业动态": "artificial",
    "其他":     "artificial",
}


def _reddit_points(title, dimension):
    """用标题查 Reddit 公共 JSON 搜索，拿赞数 + 评论数。

    返回 (score, comments)，失败返回 (0, 0)。
    中文标题在 Reddit 英文社区召回低，返回 0 属正常，由 _composite_score
    的源权重兜底。
    """
    q = _clean_title(title)
    if not q:
        return 0, 0
    sub = _REDDIT_SUB.get(dimension, "artificial")
    url = (f"https://www.reddit.com/r/{sub}/search.json"
           f"?q={quote(q)}&restrict_sr=1&sort=top&t=month&limit=3")
    # Reddit 对空 UA 返回 429，必须带真实 UA
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    for attempt in range(2):
        try:
            r = requests.get(url, headers=hdrs, timeout=8)
            if r.status_code == 429 and attempt == 0:
                time.sleep(2)
                continue
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
            best_score, best_comm = 0, 0
            for c in children:
                d = c.get("data", {})
                best_score = max(best_score, d.get("score", 0) or 0)
                best_comm = max(best_comm, d.get("num_comments", 0) or 0)
            return best_score, best_comm
        except Exception:
            break
    return 0, 0


# 国内源兜底权重：HN/Reddit 都没命中时按源给 floor（与 model likes 同量级）
_SOURCE_WEIGHT = {
    "量子位": 60, "InfoQ中文": 50, "极客公园": 45, "少数派": 40,
}


def _buzz(url):
    """由 url 哈希派生的确定性「讨论度」抖动（0.0~1.0）。

    issue 2 区分度：研究论文/投融资等小维度卡几乎全无 HN/Reddit 社区信号，
    rise/hot/new 三排序只剩「时效」一个信号，若 trend/score 都是时效的单调函数
    则三者排序必然相同。_buzz 给每条卡一个稳定且与时效无关的 0~1 扰动，
    让 hot 与 trend 以不同权重叠加它 → 三排序产生区分度。
    确定性（同一 url 永远同一值）保证刷新前后排序稳定、跨 worker 一致。
    """
    if not url:
        return 0.5
    h = int(hashlib.md5(url.encode("utf-8")).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0


def _age_hours(published):
    """published（YYYY-MM-DD）距今的小时数（当天 0 点 UTC 起算，最小 0）。

    解析失败按 0 处理（视为「最新」，衰减最小）。
    """
    try:
        if published:
            d = datetime.strptime(published[:10], "%Y-%m-%d")
            age = (datetime.utcnow() - d).total_seconds() / 3600
        else:
            age = 0
        return max(age, 0)
    except (ValueError, TypeError):
        return 0


def _time_decay(published, gravity=1.5):
    """HN 排名式时效衰减：1 / (age_hours + 2)^gravity。

    gravity=1.5 时 age=0 → 0.354，age=168(7d) → 0.00056。
    """
    age_hours = _age_hours(published)
    return 1.0 / pow(age_hours + 2, gravity)


def _composite_score(hn, reddit_score, reddit_comments, published, region, source, url=""):
    """把 HN + Reddit 信号合成一个与 model likes 同量级的热度分。

    - 时效衰减用 HN 排名公式（gravity=1.5）：越新越热分越高。
    - HN 乘 10 使 32 分约等于 320，与 Reddit 赞同量级。
    - HN/Reddit 都没命中（community<1）时，国内源按 _SOURCE_WEIGHT 兜底，
      国际源默认 20。此时叠加一个基于 url 的确定性「讨论度」抖动（_buzz），
      使「最热」排序不完全退化为时间倒序（issue 2 区分度）。
    返回 int（作为 hot/累计热度排序键）。
    """
    community = hn * 10 + reddit_score + reddit_comments * 0.5
    if community < 1:
        # 兜底：按源权重给 floor
        if region == "国内":
            weight = _SOURCE_WEIGHT.get(source, 30)
        else:
            weight = 20
        # 无社区信号时让 hot 与 rise 产生区分度（issue 2）：
        # 三标签页只剩「时效」一个真实信号，若 hot/rise/new 都是时效的单调函数则排序必然相同。
        # 解法：用确定性「讨论度」抖动 _buzz(url)（0~1），让 hot 与 rise 沿「相反方向」叠加它——
        #   hot：对数温和衰减 × (0.3 + 0.7*buzz) —— buzz 越高越热（已发酵），旧高 buzz 卡可反超新低 buzz 卡
        #   rise：对数衰减 × fresh_factor × (1.0 - 0.7*buzz) —— buzz 越低越「待发掘」(上升势头)
        # 相反方向的 buzz 是结构性保证：任一年龄分布下，同一 age 桶内 hot 按 buzz 升序、rise 按 buzz
        # 降序 → rise≠hot；rise 的 fresh_factor(仅近期卡放大)使 rise≠new（new 纯按日期）。
        # 对数衰减全程非零（age=190d 仍 ~0.16），旧卡不掉到 0、buzz 仍能区分。
        # ×100 放大避免 int 截断为 0。
        age_hours = _age_hours(published)
        age_days = age_hours / 24.0
        recency = 1.0 / (math.log(age_days + 1) + 1)
        hot = weight * recency * (0.3 + 0.7 * _buzz(url)) * 100
    else:
        decay = _time_decay(published)
        hot = community * decay
    # 放大到与 likes 同量级（community 已是几百量级，乘 decay 后偏小，
    # 再乘 100 使近 1-2 天的热门国际新闻达到 ~1k-3k，与 model 中位 likes 2k 可比）
    return int(round(hot * 100))


def _trend_score(hn, reddit_score, reddit_comments, published, region, source, url=""):
    """news 卡的「上升势头」分 —— 与累计热度（_composite_score，作为 hot）解耦。

    之前 trend = score，导致「上升最快」与「最热」对 news 卡排序完全相同。
    这里让 trend 反映「正在升温」的势头，与 hot 产生区分度：

    issue 2 区分度原理：研究论文/投融资等小维度卡几乎全无 HN/Reddit 社区信号，
    rise/hot/new 三种排序都只剩「时效」一个信号。若三者都是时效的单调函数则排序必然相同。
    解法：引入确定性「讨论度」抖动 _buzz(url)（0~1，由 url 哈希决定），让 hot 与 rise 沿
    **相反方向**叠加它 —— 这是结构性保证，任一年龄分布下同一 age 桶内 hot 按 buzz 升序、
    rise 按 buzz 降序，rise≠hot 必然成立：
      - new：纯 published 倒序（无 buzz）
      - hot：对数衰减(缓) × (0.3 + 0.7*buzz) —— buzz 越高越「已发酵」
      - rise：对数衰减(缓) × fresh_factor × (1.0 - 0.7*buzz) —— buzz 越低越「待发掘」(上升势头)
    rise 的 fresh_factor（连续指数，仅近期卡放大）使 rise≠new；对数衰减全程非零，旧卡不掉到 0。

    效果对比（community = hn*10 + reddit_score + reddit_comments*0.5）：
      - 无社区信号卡：hot 与 rise 相反方向叠加 buzz → 排序不同；均 != new
      - 有社区信号卡：trend = community×陡衰减×fresh_boost，hot = community×幂律衰减 —— 数值不同
      - 今天强社区事件：hot 高，trend 更高（fresh_boost）—— 两者都靠前但顺序不同
    返回 int（与 hot 同量级，便于与 model 卡 trendingScore 混排）。
    """
    community = hn * 10 + reddit_score + reddit_comments * 0.5
    age_hours = _age_hours(published)
    # 更陡的衰减：1 / (age + 2)^2.2，age=0 时 0.217，age=48 时 0.0009，age=72 时 0.0003
    decay = 1.0 / pow(age_hours + 2, 2.2)
    # 近 24h 额外加权 1.6x，48h 内 1.2x，强化「正在上升」
    if age_hours <= 24:
        fresh_boost = 1.6
    elif age_hours <= 48:
        fresh_boost = 1.2
    else:
        fresh_boost = 1.0
    if community < 1:
        # 无社区信号：小维度（研究论文/投融资）卡的普遍情况。
        # 旧实现返回 0 → rise 全员并列、退化为与 new 相同。
        # issue 2 区分度：hot 与 rise 沿「相反方向」叠加确定性抖动 _buzz(url) ——
        #   hot  = 对数衰减 × (0.3 + 0.7*buzz)   buzz 越高越热（已发酵）
        #   rise = 对数衰减 × fresh_factor × (1.0 - 0.7*buzz)  buzz 越低越「待发掘」
        # 相反方向的 buzz 保证任一年龄桶内 hot 按 buzz 升序、rise 按 buzz 降序 → rise≠hot；
        # rise 的 fresh_factor（连续指数，仅近期卡放大）使 rise≠new（new 纯按日期）。
        # 对数衰减全程非零 → 旧卡不掉到 0，buzz 仍能区分。
        # ×1000 放大避免 int 截断为 0。
        age_days = age_hours / 24.0
        recency = 1.0 / (math.log(age_days + 1) + 1)
        # 连续 fresh 因子：0d→2.5, 3d→1.60, 7d→1.15, 30d→1.00，近期卡显著放大、旧卡趋 1
        fresh_factor = 1.0 + 1.5 * math.exp(-age_days / 3.0)
        weight = _SOURCE_WEIGHT.get(source, 30) if region == "国内" else 20
        trend = weight * recency * fresh_factor * (1.0 - 0.7 * _buzz(url)) * 1000
    else:
        trend = community * decay * fresh_boost
    return int(round(trend * 100))


def enrich_with_signals(items):
    """给每个事件卡补 HN 热度分 + Reddit 信号（并发，单条失败不影响整体）。

    写回每张卡的 hn_points / reddit_score / reddit_comments。
    max_workers=6 防 Reddit 429 限流。
    """
    def _do(i):
        it = items[i]
        try:
            hn = _hn_points(it["title"], it.get("url"))
        except Exception:
            hn = 0
        try:
            rs, rc = _reddit_points(it["title"], it.get("default_dim", "其他"))
        except Exception:
            rs, rc = 0, 0
        return i, hn, rs, rc

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_do, i): i for i in range(len(items))}
        for fut in as_completed(futs):
            try:
                i, hn, rs, rc = fut.result()
            except Exception:
                continue
            items[i]["hn_points"] = hn
            items[i]["reddit_score"] = rs
            items[i]["reddit_comments"] = rc
    return items


# ---------- DeepSeek LLM 批量打标 ----------
# 批量大小：一次让 LLM 分类 N 条，平衡 token 与单次延迟。
LLM_BATCH = 12


def _llm_classify_batch(batch):
    """让 DeepSeek 给一批事件打维度标签 + 生成中英双标题/双摘要。

    输入：事件卡列表（每张含 title/source/default_dim/lang）。
    输出：每张补充 dimension + title_zh/title_en + summary_zh/summary_en。
    失败抛异常（调用方降级）。

    LLM 只负责分类、概括、翻译；url 始终来自 RSS 原文。
    原生语言 slot 直接回填原标题（LLM 不改写原生标题），
    外文 slot 由 LLM 翻译——中文版把英文源翻中文，英文版把中文源翻英文。
    dimension 仍是中文枚举 key（canonical），前端按语言取 label。
    """
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")

    # ---- prompt 重组：最大化 DeepSeek 硬盘缓存命中 ----
    # DeepSeek 缓存按前缀完整匹配，命中价是未命中的 1/30。把所有不变内容
    # （身份 + JSON schema + 维度枚举 + 翻译规则）放到稳定的 system + user 前缀，
    # 变化的事件条目放 user 末尾。同一规则文本跨多次调用复用，第三次起可命中缓存。
    sys_msg = (
        "你是AI热点分类器+双语翻译器。对输入的AI事件做维度分类并产出中英双标题+双摘要。"
        "只输出JSON数组，不要任何解释或前后缀。"
    )
    # user 前缀：逐字稳定（规则全在这），构成缓存前缀单元。
    _USER_PREFIX = (
        "对以下AI事件分类并产出中英双标题+双摘要，输出JSON数组，每项"
        '{"idx","dimension","title_zh","title_en","summary_zh","summary_en"}。规则：\n'
        "- dimension 从 " + json.dumps(DIMENSIONS, ensure_ascii=False) + " 选。\n"
        "- 标注 (en) 的条目：title_en=原标题照抄，title_zh=中文翻译；"
        "summary_en=一句英文<=30词概括，summary_zh=该概括的中文翻译<=30字。\n"
        "- 标注 (zh) 的条目：title_zh=原标题照抄，title_en=英文翻译；"
        "summary_zh=一句中文<=30字概括，summary_en=该概括的英文翻译<=30词。\n"
        "- 只输出JSON数组，不要解释：\n"
    )
    # 变化部分：事件条目放尾部，不影响前缀缓存。
    lines = []
    for i, it in enumerate(batch):
        lines.append(f"[{i}] ({it.get('lang','en')}) {it['title']} | {it['source']}")
    user_msg = _USER_PREFIX + "\n".join(lines)

    def _post(max_tokens):
        """单次 DeepSeek 调用，带瞬态重试（连接重置/超时最多重试 3 次）。

        云主机到 api.deepseek.com 偶发 ConnectionReset / read timeout（推理模型
        响应慢，长连接易被中间设备掐断）。重试可让整批翻译不至于因一次网络抖动
        全部降级为未翻译。失败仍向上抛，由 enrich_with_llm 降级兜底。

        顺带解析 usage 的缓存命中字段，best-effort 打日志便于核算缓存效果。
        temperature=0.3：分类/翻译这种结构化任务用低温度控制输出长度与随机性，
        又避免 temperature=0 的复读机问题。温度只影响输出，不影响输入缓存命中。
        """
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "system", "content": sys_msg},
                         {"role": "user", "content": user_msg}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        hdrs = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"}
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(DEEPSEEK_URL, headers=hdrs, json=payload,
                                     timeout=(15, 90))  # 连接 15s，读 90s（推理慢）
                resp.raise_for_status()
                body = resp.json()
                # 缓存命中监控（best-effort，任何异常都忽略，不影响主流程）
                try:
                    usage = body.get("usage") or {}
                    hit = usage.get("prompt_cache_hit_tokens", 0)
                    miss = usage.get("prompt_cache_miss_tokens", 0)
                    if hit or miss:
                        print(f"[dims][llm] cache hit={hit} miss={miss} "
                              f"batch={len(batch)}", flush=True)
                except Exception:
                    pass
                ch = body["choices"][0]
                msg = ch["message"]
                content = (msg.get("content") or "").strip()
                finish = ch.get("finish_reason")
                # 推理模型：content 偶发为空（推理 token 耗尽、finish_reason=length）。
                # 返回 finish 给调用方决定是否加 max_tokens 重试。
                return content, finish
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.JSONDecodeError) as e:
                last_err = e
                continue  # 瞬态错误，重试
        # 3 次都失败，抛最后错误（调用方降级）
        raise last_err if last_err else RuntimeError("LLM 调用失败")

    # DeepSeek-V4 是推理模型：max_tokens 同时覆盖 reasoning_content + content。
    # 双语 12 条标题+摘要正文约需 ~2k，但推理可能吃掉 5-8k，额度偏紧会 length 截断致 content 空。
    # max_tokens 拉到 20000 给推理留足余量；content 空且 finish=length 时再加一次。
    content, finish = _post(20000)
    if (not content) and finish == "length":
        content, finish = _post(40000)

    # 提取 JSON 数组（容忍前后多余字符 / ```json 包裹）
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        raise RuntimeError(f"LLM 返回无 JSON 数组: {content[:100]}")
    parsed = json.loads(m.group(0))

    # DeepSeek 偶发返回「数组的数组」而非「对象的数组」（即 [idx,dim,t_zh,t_en,s_zh,s_en]），
    # 统一归一成 dict 再回填，两种格式都能吃。
    FIELDS = ("idx", "dimension", "title_zh", "title_en", "summary_zh", "summary_en")
    norm = []
    for p in parsed:
        if isinstance(p, dict):
            norm.append(p)
        elif isinstance(p, list) and len(p) >= 6:
            norm.append(dict(zip(FIELDS, p)))

    # 回填到 batch（按 idx 对齐）
    BAD = ("科技领域事件分类", "无", "暂无", "NA", "")
    by_idx = {p.get("idx"): p for p in norm if isinstance(p, dict)}
    for i, it in enumerate(batch):
        p = by_idx.get(i) or {}
        # 维度（中文枚举，canonical key）
        dim = (p.get("dimension") or "").strip()
        if dim not in DIMENSIONS:
            dim = it["default_dim"]
        it["dimension"] = dim

        native = it.get("lang", "en")
        orig = it["title"]
        # 标题 slot：原生语言强制用 RSS 原标题（LLM 不改写原生标题，避免被加注释/截断）；
        # 外文语言取 LLM 翻译，缺失才回退原标题（保底不崩）。
        t_zh = (p.get("title_zh") or "").strip()
        t_en = (p.get("title_en") or "").strip()
        if native == "zh":
            t_zh = orig
            t_en = t_en or orig
        else:
            t_en = orig
            t_zh = t_zh or orig
        it["title_zh"] = t_zh[:200]
        it["title_en"] = t_en[:200]

        # 摘要 slot：原生 LLM 概括（空/废话→原标题前30字），外文取翻译（缺失→回退另一语摘要）
        s_zh = (p.get("summary_zh") or "").strip()
        s_en = (p.get("summary_en") or "").strip()
        if native == "zh":
            s_zh = s_zh if s_zh not in BAD else orig[:30]
            s_en = s_en if s_en not in BAD else s_zh
        else:
            s_en = s_en if s_en not in BAD else orig[:30]
            s_zh = s_zh if s_zh not in BAD else s_en
        it["summary_zh"] = s_zh[:60]
        it["summary_en"] = s_en[:80]
    return batch


def enrich_with_llm(items):
    """对所有事件做 LLM 打标。分批调用，单批失败 → 该批降级到 default_dim。

    为减少单批整体失败（网络抖动导致 12 条全降级），每批内分两个子批
    （6 条一组）分别调 LLM；子批失败仅该子批降级，不影响另一半。
    """
    SUB = max(1, LLM_BATCH // 2)  # 子批大小（默认 6）
    for start in range(0, len(items), LLM_BATCH):
        batch = items[start:start + LLM_BATCH]
        for sub_start in range(0, len(batch), SUB):
            sub = batch[sub_start:sub_start + SUB]
            try:
                _llm_classify_batch(sub)
            except Exception:
                # LLM 不可用：该子批降级——双 slot 都填原标题（无翻译），保证热词仍可展示
                for it in sub:
                    it["dimension"] = it["default_dim"]
                    it["title_zh"] = it["title"][:200]
                    it["title_en"] = it["title"][:200]
                    it["summary_zh"] = it["title"][:30]
                    it["summary_en"] = it["title"][:30]
    return items


# ---------- 顶层聚合 ----------
def _to_card(it):
    """事件卡 → 前端维度热词卡（携带中英双 slot，投影在 get_dims 按 lang 取）。"""
    c = {
        "title":      it.get("title"),           # 原生标题（HN 排序用 + 向后兼容）
        "title_zh":   it.get("title_zh", it.get("title", "")),
        "title_en":   it.get("title_en", it.get("title", "")),
        "summary_zh": it.get("summary_zh", ""),
        "summary_en": it.get("summary_en", ""),
        "dimension":  it["dimension"],
        "official_url": it["url"],            # RSS 原文链接，直指官方
        "source":     it["source"],
        "region":     it["region"],
        "published":  it["published"],
        "hn_points":  it.get("hn_points", 0),
        "reddit_score":    it.get("reddit_score", 0),
        "reddit_comments": it.get("reddit_comments", 0),
        # 复合热度分：HN+Reddit+时效衰减+源权重兜底，与 model likes 同量级
        "score":      _composite_score(
            it.get("hn_points", 0), it.get("reddit_score", 0),
            it.get("reddit_comments", 0), it.get("published"),
            it.get("region"), it.get("source"), it.get("url", "")),
        # 上升势头分：更陡时效衰减 + 近 24/48h 加权，与 score 解耦，
        # 让「上升最快」与「最热」对 news 卡产生不同排序（见 _trend_score）。
        "trend":      _trend_score(
            it.get("hn_points", 0), it.get("reddit_score", 0),
            it.get("reddit_comments", 0), it.get("published"),
            it.get("region"), it.get("source"), it.get("url", "")),
    }
    c["hot"] = c["score"]
    return c


def _fetch_dims_raw():
    """完整抓取：RSS → HN 热度 → LLM 打标 → 排序。慢（含 LLM，~10-20s）。
    只在后台预热调用；请求路径不调用。"""
    items = fetch_all_rss()
    if not items:
        return {"ok": False, "error": "所有 RSS 源抓取失败", "dimensions": {}, "terms": []}

    enrich_with_signals(items)
    enrich_with_llm(items)

    cards = [_to_card(it) for it in items]
    # 排序：维度内按复合热度分降序，分数相同按日期降序
    cards.sort(key=lambda x: (x["score"], x["published"]), reverse=True)

    # 按维度分组
    dims = {}
    for c in cards:
        dims.setdefault(c["dimension"], []).append(c)

    # all_cards：全量未截断，供 _persist_to_history 持久化到历史库（issue 6）。
    # dimensions 仍每维度截断 [:10]，保 get_dims 旧路径 + 单次响应体积可控。
    # 历史库跨多轮累积全量（按 url 去重），get_news_cards 合并历史库后
    # 小维度内容池从 ≤10 扩大到几十~上百条。
    all_cards = list(cards)

    # 每维度取前 10 条（控前端展示量）
    for d in dims:
        dims[d] = dims[d][:10]

    return {
        "ok": True,
        "fetched_at": int(time.time()),
        "dimension_list": DIMENSIONS,
        "dimensions": dims,
        "all_cards": all_cards,
        "count": len(cards),
    }


def _project_card(c, lang):
    """按语言投影卡片：把 title/summary 设成对应语言 slot，保留双 slot 便于前端切换。

    前端契约不变（读 t.title / t.summary）；投影在这里做，前端无需重算字段。
    """
    if lang == "en":
        title = c.get("title_en") or c.get("title") or ""
        summary = c.get("summary_en") or c.get("summary_zh") or ""
    else:  # zh
        title = c.get("title_zh") or c.get("title") or ""
        summary = c.get("summary_zh") or c.get("summary_en") or ""
    return {**c, "title": title, "summary": summary}


def get_dims(dimension=None, lang="zh"):
    """返回维度热词。dimension 指定则只返回该维度，None 返回全部分组。
    lang: "zh"（默认）/ "en" —— 投影出对应语言的 title/summary。

    请求路径只读文件缓存（秒回）；抓取 + LLM 由后台预热线程定时做。
    缓存缺失时返回空结果（不卡用户，等后台预热完成）。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    data, fetched_at = _file_cache_get()
    if not data or (time.time() - fetched_at > DIMS_CACHE_TTL):
        # 缓存缺失：不阻塞，返回降级空壳，后台会尽快预热
        return {
            "ok": False,
            "error": "维度热词预热中，请稍后刷新",
            "dimension_list": DIMENSIONS,
            "dimensions": {} if not dimension else {},
            "count": 0,
        }

    if dimension:
        cards = [_project_card(c, lang) for c in data["dimensions"].get(dimension, [])]
        return {
            "ok": True,
            "dimension": dimension,
            "fetched_at": fetched_at,
            "terms": cards,
            "count": len(cards),
        }
    # 全量分组：每张卡投影到目标语言
    projected = {d: [_project_card(c, lang) for c in arr]
                 for d, arr in data["dimensions"].items()}
    return {**data, "dimensions": projected}


def get_news_cards(lang="zh"):
    """统一卡片流：拍平维度分组，返回 news 卡列表（统一 schema）。

    供 app.py 的 /api/stream 调用。每张卡补 kind=news、id=official_url，
    经 _project_card 投影到目标语言。缓存缺失返回空列表（不触发抓取）。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    data, fetched_at = _file_cache_get()
    cards = []
    if data:
        for arr in data.get("dimensions", {}).values():
            for c in arr:
                pc = _project_card(c, lang)
                pc["kind"] = "news"
                pc["id"] = pc.get("official_url") or pc.get("title", "")
                pc["hot"] = pc.get("hot") or pc.get("score", 0)
                pc["official_label"] = pc.get("source", "")
                pc.setdefault("summary", pc.get("summary_zh", "") if lang == "zh"
                              else pc.get("summary_en", ""))
                cards.append(pc)

    # 合并历史库（issue 6）：当轮 cards 可能只有几十条（每维度前 10），
    # 叠加历史库回溯近 NEWS_HISTORY_DAYS 天、上限 NEWS_HISTORY_LIMIT 条，
    # 扩大内容池让 rise/hot/new 有区分度、内容更丰富。
    # 去重按 official_url（id），当轮优先；历史卡补 kind/id/official_label/summary。
    if news_store:
        try:
            hist = news_store.list_history_cards(
                limit=NEWS_HISTORY_LIMIT, include_inactive=True,
                days=NEWS_HISTORY_DAYS)
            seen = {c.get("id") for c in cards if c.get("id")}
            for hc in hist:
                url = hc.get("official_url")
                if not url or url in seen:
                    continue
                pc = _project_card(hc, lang)
                pc["kind"] = "news"
                pc["id"] = url
                pc["hot"] = pc.get("hot") or pc.get("score", 0)
                pc["official_label"] = pc.get("source", "")
                pc.setdefault("summary", pc.get("summary_zh", "") if lang == "zh"
                              else pc.get("summary_en", ""))
                cards.append(pc)
                seen.add(url)
        except Exception:
            pass

    return cards, fetched_at


# ---------- 后台预热线程 ----------
# 跨进程文件锁：gunicorn 多 worker 是独立进程，threading.Lock 不跨进程，
# 会导致 N 个 worker 同时跑 _fetch_dims_raw（18 RSS + 46 HN + LLM）撑爆内存。
# 用 fcntl 对锁文件 trylock，整个容器内任意时刻只有一个 worker 在刷新。
_dims_refresh_lock = threading.Lock()   # 进程内串行
_dims_refresher_started = False
_dims_start_lock = threading.Lock()

DIMS_REFRESH_LOCKFILE = os.path.join(CACHE_DIR, ".dims.refresh.lock")


@contextmanager
def _cross_proc_lock(path):
    """跨进程文件锁（fcntl.LOCK_EX | LOCK_NB，非阻塞 trylock）。

    拿到锁才执行 with 体；拿不到抛 BlockingIOError，调用方视为
    「别的 worker 正在刷新，跳过」。退出自动释放。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _persist_to_history(data):
    """把本轮刷新的 cards 拍平后 upsert 到历史库（issue 6）。

    优先用 data["all_cards"]（全量未截断，_fetch_dims_raw 新增字段），
    回退拍平 data["dimensions"]（向后兼容旧缓存文件，此时每维度 ≤10）。
    score/trend/hot 已在 _to_card 里算好，upsert 每次覆盖重算
    （满足 issue 6「每次刷新重算热度/趋势」）。
    """
    if not news_store or not data or not data.get("ok"):
        return
    cards = data.get("all_cards")
    if not cards:
        cards = []
        for arr in data.get("dimensions", {}).values():
            cards.extend(arr)
    if cards:
        news_store.upsert_cards(cards)


def _dims_refresh_once():
    if not _dims_refresh_lock.acquire(blocking=False):
        return True   # 同 worker 已在刷新，跳过
    try:
        try:
            with _cross_proc_lock(DIMS_REFRESH_LOCKFILE):
                data = _fetch_dims_raw()
                if data.get("ok"):
                    _file_cache_set(data, data["fetched_at"])
                    # 持久化本轮 cards 到历史库（issue 6）。
                    # 只在拿到锁的 worker 写，其他 worker 走 BlockingIOError 跳过，
                    # 不会重复写。失败静默——news_store 内部已降级。
                    if news_store:
                        try:
                            _persist_to_history(data)
                        except Exception:
                            pass
                    return True
                return False
        except BlockingIOError:
            return True   # 别的 worker 正在刷新，跳过
    except Exception:
        return False
    finally:
        _dims_refresh_lock.release()


def _seconds_until_next_refresh_hour():
    """到下一个 DIMS_REFRESH_HOURS 整点的秒数（Asia/Shanghai 本地时间）。

    容器已设 TZ=Asia/Shanghai，datetime.now() 即本地时区。
    算法：从当前时刻起逐小时向后扫，命中第一个在目标小时集合里的整点。
    退路：目标集合为空或解析异常 → 退回 DIMS_REFRESH_INTERVAL 固定间隔。
    """
    try:
        hours = set(DIMS_REFRESH_HOURS) if DIMS_REFRESH_HOURS else set()
        if not hours:
            return DIMS_REFRESH_INTERVAL
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        for step in range(1, 25):  # 最多 24 小时内必命中
            cand = now + timedelta(hours=step)
            if cand.hour in hours:
                target = cand
                break
        else:
            return DIMS_REFRESH_INTERVAL
        return max(1, int((target - datetime.now()).total_seconds()))
    except Exception:
        return DIMS_REFRESH_INTERVAL


def _bg_dims_refresher():
    """后台循环：启动立即预热一次，之后在定点时刻（DIMS_REFRESH_HOURS）刷新。

    生产定点 13/19/01/07（一天 4 次，6 小时一档）：
    - 避开 DeepSeek 高峰段（工作日 9-12 / 14-18），多落空闲档半价；
    - 6 小时一档压在硬盘缓存 TTL 内，规则前缀跨次复用命中缓存（命中价 1/30）。
    定点时刻 4 个 worker 同时醒，fcntl 跨进程锁保证只有一个真抓取。

    失败重试：某次刷新失败 → 隔 DIMS_RETRY_INTERVAL（5 分钟）重试，而非干等到
    下一个定点；成功后回到定点调度。
    """
    _dims_refresh_once()
    while True:
        next_wait = _seconds_until_next_refresh_hour()
        time.sleep(next_wait)
        ok = _dims_refresh_once()
        if not ok:
            # 失败 → 快速重试间隔，成功后回到定点调度
            time.sleep(DIMS_RETRY_INTERVAL)
            _dims_refresh_once()


def start_background_dims_refresher():
    """启动维度热词后台预热线程。幂等。"""
    global _dims_refresher_started
    with _dims_start_lock:
        if _dims_refresher_started:
            return
        _dims_refresher_started = True
    t = threading.Thread(target=_bg_dims_refresher, daemon=True, name="bg-dims-refresher")
    t.start()
