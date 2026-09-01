"""
热点聚合服务 —— 通过多个免费 API / RSS / 网页抓取聚合当前最新热点。

运行：
    pip install flask requests
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

import os
import re
import json
import time
import math
import base64
import hmac
import threading
import hashlib
import zlib
import struct
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

import requests
from flask import (Flask, jsonify, render_template, request, Response,
                   redirect, session, abort)

import tracker
import dims
import config
import store
from stream_utils import (card_identity as _stream_card_identity,
                          dedupe_cards as _dedupe_stream_cards,
                          dimension_members as _stream_dimension_members,
                          dimension_counts as _stream_dimension_counts,
                          dimension_list as _stream_dimension_list_base)

try:
    import terms as terms_mod  # 词粒度聚合层（词榜/详情/搜索联动）；失败自动降级
except Exception:
    terms_mod = None

# 启动后台预热线程：定时抓取 HF + arXiv 写文件缓存，请求路径只读缓存秒回。
# 每个 gunicorn worker 各起一个 daemon 线程；通过 fcntl 跨进程文件锁串行化，
# 整个容器内任意时刻只有一个 worker 在抓取（省 arXiv 配额 + 防多 worker 并发撑爆内存）。
tracker.start_background_refresher()
# 维度热词后台预热：RSS + HN + DeepSeek 打标，独立跨进程文件锁串行化。
dims.start_background_dims_refresher()

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

# ---------- 通用配置 ----------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
TIMEOUT = 5           # 单个上游请求超时（秒）—— 慢源快速失败，避免拖垮整体
SOURCE_DEADLINE = 25  # 单源总抓取截止时间（秒）—— HN 需逐条拉取，留足时间
CACHE_TTL = 300       # 单源结果缓存 5 分钟
_cache = {}      # {source: (timestamp, data)}
_cache_lock = threading.Lock()

# 统一返回格式：每条热点 -> {title, hot, url, extra}
def _ok(source, items):
    return {"source": source, "ok": True, "count": len(items),
            "fetched_at": int(time.time()), "items": items}

def _fail(source, err):
    return {"source": source, "ok": False, "count": 0,
            "fetched_at": int(time.time()), "items": [], "error": str(err)}

def _cached(source):
    with _cache_lock:
        ent = _cache.get(source)
        if ent and time.time() - ent[0] < CACHE_TTL:
            return ent[1]
    return None

def _set_cache(source, data):
    with _cache_lock:
        _cache[source] = (time.time(), data)


# ---------- SEO 辅助 ----------
# 详情页进程内缓存：get_term_detail 是 live HF + 同步 arXiv（~1-4s），
# 用 TTL 缓存避免每次请求都打上游。key = term_name（小写归一）。
_detail_cache = {}
_detail_cache_lock = threading.Lock()

def _detail_cached(name):
    with _detail_cache_lock:
        ent = _detail_cache.get(name)
        if ent and time.time() - ent[0] < config.TERM_DETAIL_CACHE_TTL:
            return ent[1]
    return None

def _detail_set_cache(name, data):
    with _detail_cache_lock:
        _detail_cache[name] = (time.time(), data)


def _explain_fallback(term, lang, news_cnt=0, hot=0, rise=0, origin="news"):
    """热词解释三级取词的兜底：数据化模板（保证每个热词页都有解释块）。

    静态词典 / terms 表 LLM 解释都未命中时使用，内容来自词元信息本身，
    诚实且零 LLM 成本。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    if origin == "hf":
        if lang == "en":
            return (f"\"{term}\" is a trending AI model on the "
                    f"HuggingFace community.")
        return f"「{term}」是 HuggingFace 社区热推的 AI 模型。"
    if lang == "en":
        parts = [f"\"{term}\" is a trending AI term, linked to "
                 f"{news_cnt} related reports."]
        if hot:
            parts.append(f"Hotness {hot}.")
        # rise == -1.0 是本周期无活跃报道的占位（非真实下跌），解释文案不展示
        if rise and rise > -0.999:
            parts.append(f"Rise {rise:.2f}.")
        return " ".join(parts)
    parts = [f"「{term}」是近期 AI 热点词，与 {news_cnt} 篇相关报道关联。"]
    if hot:
        parts.append(f"热度 {hot}。")
    if rise and rise > -0.999:
        parts.append(f"环比上升 {rise:.2f}。")
    return " ".join(parts)


def _word_detail(term_name, lang="zh"):
    """通用词聚合数据装配（/api/word 与 /term/<name> 详情页共用）。

    1. normalize → 查 terms 词主表：命中（任何词都有页）→ 词元信息 +
       关联报道（news_cards LIKE，≤50，按语言投影）；
       origin ∈ {hf,both} 时额外调 tracker.get_term_detail 拿 live
       官方/社区/arXiv 区块（沿用进程内 TTL 缓存，~1-4s 慢路径只在详情页）。
    2. 未命中词池 → 回退 tracker live（HF 长尾模型仍可直达）。
    3. 都未命中 → {"ok": False}，调用方 404。
    """
    lang = lang if lang in ("zh", "en") else "zh"
    if not terms_mod:
        return {"ok": False}
    canon = terms_mod.normalize_term(term_name)
    if not canon:
        return {"ok": False}

    def _project(c):
        title = (c.get("title_zh") if lang == "zh" else c.get("title_en")) \
            or c.get("title") or ""
        summary = (c.get("summary_zh") if lang == "zh" else c.get("summary_en")) \
            or c.get("summary_zh") or c.get("summary_en") or ""
        return {**c, "title": title, "summary": summary}

    def _hf_live(full_id):
        """HF live 区块（官方/社区/论文），进程内 TTL 缓存，失败静默。"""
        if not full_id:
            return None
        ck = f"hf:{full_id.lower()}"
        cached = _detail_cached(ck)
        if cached is None:
            try:
                cached = tracker.get_term_detail(full_id)
            except Exception:
                cached = {"ok": False}
            _detail_set_cache(ck, cached)
        return (cached.get("term") or {}) if cached.get("ok") else None

    row = terms_mod.get_term_row(term_name)
    if row:
        hf = None
        if row.get("hf_json"):
            try:
                hf = json.loads(row["hf_json"])
            except (json.JSONDecodeError, ValueError):
                hf = None
        # Query by the canonical key that was used to find the row.  The
        # terms layer still accepts aliases for direct callers, while this
        # avoids letting a display/path spelling affect historical fallback.
        news = [_project(c) for c in
                terms_mod.get_term_news(canon, limit=50, lang=lang)]
        term_info = {
            "term": (row.get("display_en") if lang == "en"
                     else row.get("display")) or row.get("display") or canon,
            "display_zh": row.get("display_zh") or "",
            "origin": row.get("origin") or "news",
            "news_cnt": row.get("total_mentions", 0),
            "hot": row.get("cur_hot", 0),
            "rise": row.get("cur_rise", 0),
            "novelty": row.get("cur_novelty", 0),
            "first_seen_at": row.get("first_seen_at") or "",
            "last_seen_at": row.get("last_seen_at") or "",
            "explain": "",
        }
        # 三级取词：静态词典 → terms 表 LLM 解释 → 数据化模板兜底（恒非空）
        term_info["explain"] = (
            terms_mod.get_term_explanation(canon, lang)
            or _explain_fallback(term_info["term"], lang,
                                 term_info["news_cnt"], term_info["hot"],
                                 term_info["rise"], term_info["origin"]))
        return {"ok": True, "term": term_info, "news": news, "hf": hf,
                "hf_detail": _hf_live((hf or {}).get("full_id")),
                "legacy_hf": False}

    # 未命中词池：HF 长尾模型直达（保持旧详情页可达性）
    hf_detail = _hf_live(term_name)
    if hf_detail:
        return {"ok": True,
                "term": {"term": hf_detail.get("term") or term_name,
                         "display_zh": "", "origin": "hf",
                         "news_cnt": 0, "hot": 0, "rise": 0, "novelty": 0,
                         "first_seen_at": "", "last_seen_at": "",
                         "explain": _explain_fallback(
                             hf_detail.get("term") or term_name,
                             lang, 0, 0, 0, "hf")},
                "news": [],
                "hf": {"full_id": hf_detail.get("full_id", ""),
                       "likes": hf_detail.get("likes", 0),
                       "trending_score": hf_detail.get("trending_score", 0),
                       "downloads": hf_detail.get("downloads", 0),
                       "official_url": hf_detail.get("official_url", ""),
                       "author": hf_detail.get("author", ""),
                       "tags": hf_detail.get("tags") or []},
                "hf_detail": hf_detail, "legacy_hf": True}
    return {"ok": False}


def _base_url():
    """站点根 URL（末尾无斜杠）。BASE_URL 未设 → 返回 ''，调用方据此降级。"""
    return (config.BASE_URL or "").rstrip("/")

def _abs(path):
    """拼绝对 URL。BASE_URL 未设时返回 None（模板据此跳过 canonical/OG url）。"""
    base = _base_url()
    if not base:
        return None
    return base + path

def _seo_enabled():
    return bool(config.SEO_ENABLED)


# 统一卡片流的展示上限。排序由各数据源在截断前完成，前端只过滤不重排。
# 60 → 100（2026-09-02）：词池 words.json 保留 200 词，60 的展示窗口让今日热词
# （如 Openclaw，按热窗新鲜度加权后仍 ~60-90 名）长期被挤出首屏；放宽到 100，
# 配合 terms 的热度新鲜度加权，让近期热词稳定可见。
WORD_STREAM_LIMIT = 100


def _stream_number(card, field):
    """读取排序字段，兼容缓存中的字符串/空值且不产生比较异常。"""
    try:
        value = float(card.get(field, 0) or 0)
        return value if math.isfinite(value) else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _stream_dimension_list(cards, view):
    """返回包含实际卡片维度的稳定分类顺序。"""
    return _stream_dimension_list_base(cards, view, dims.DIMENSIONS)


# 首页 SSR 渲染的热词条数（Top-N）。读文件缓存，秒回。
SSR_INITIAL_LIMIT = 20


def _initial_terms_for_ssr(sort="rise", lang="zh"):
    """首页 SSR 用的首屏词卡：使用与后续 stream 相同的排序前缀。

    词维度重构后，首屏 SSR 注入词卡（kind=word，含 top_news 迷你列表），
    爬虫可见「词 + 代表报道」结构；JS 接管后拉 /api/stream?view=words 全量替换。
    SSR 只取服务端排序结果的前缀，不再做按维度配额的二次重排，保证替换
    全量结果时已有卡片的顺序不变。
    任何失败返回 []，模板兜底骨架屏。
    """
    try:
        if not terms_mod:
            return []
        cards, _ = terms_mod.get_word_cards(sort=sort, lang=lang,
                                            limit=SSR_INITIAL_LIMIT)
        return _dedupe_stream_cards(cards)
    except Exception:
        return []


def _sitemap_terms():
    """sitemap.xml 用词列表：读 terms 词主表（词维度重构），按热度降序。

    词表为空（冷启动）时回退 tracker HF 榜单，保证 sitemap 不至空转。
    """
    try:
        if terms_mod:
            words = terms_mod.list_terms_for_sitemap(
                max(0, config.SITEMAP_MAX_URLS - 2))
            if words:
                return words
    except Exception:
        pass
    try:
        seen, out = set(), []
        for sort in ("trending", "top"):
            d = tracker.get_terms(sort=sort)
            for t in (d.get("terms") or []):
                slug = t.get("term")
                if slug and slug not in seen:
                    seen.add(slug)
                    out.append(slug)
        return out[:max(0, config.SITEMAP_MAX_URLS - 2)]
    except Exception:
        return []


# 站点级元信息（描述等），集中维护。
SITE_DESC = "AI 热点聚合 · 实时追踪 HuggingFace 模型趋势、arXiv 相关论文与社区讨论。上升最快、最热、最新 AI 模型一页尽览。"
SITE_DESC_EN = "AI trend aggregation · Track HuggingFace model trends, related arXiv papers, and community discussion. Browse the fastest-rising, hottest, and newest AI models in one place."

# 服务条款最后更新日期（修改条款时同步更新）。
SITE_TERMS_UPDATED = "2026-08-26"



# ---------- 各数据源抓取函数 ----------
def fetch_baidu():
    """百度热搜（PC 版结构更规整）"""
    url = "https://top.baidu.com/api/board?platform=pc&tab=realtime"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = []
    for card in data.get("data", {}).get("cards", []):
        for c in card.get("content", []):
            word = c.get("word") or c.get("query")
            if not word:
                continue
            items.append({
                "title": word,
                "hot": c.get("hotScore"),
                "url": c.get("url") or c.get("rawUrl") or
                       f"https://www.baidu.com/s?wd={word}",
                "extra": (c.get("desc") or "")[:80],
            })
    return _ok("baidu", items)


def fetch_bilibili():
    """B站热门（综合热门接口，最稳定）"""
    url = "https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = []
    for v in data.get("data", {}).get("list", []):
        stat = v.get("stat", {}) or {}
        owner = v.get("owner", {}) or {}
        items.append({
            "title": v.get("title", ""),
            "hot": stat.get("view"),
            "url": v.get("short_link_v2") or
                   f"https://www.bilibili.com/video/{v.get('bvid','')}",
            "extra": f"UP: {owner.get('name','')} · {v.get('tname','')}",
        })
    return _ok("bilibili", items)


def fetch_toutiao():
    """今日头条热榜"""
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = []
    for it in data.get("data", []):
        items.append({
            "title": it.get("Title", ""),
            "hot": it.get("HotValue"),
            "url": it.get("Url", ""),
            "extra": it.get("Label", ""),
        })
    return _ok("toutiao", items)


def fetch_hackernews():
    """Hacker News Top Stories (官方 API)"""
    ids = requests.get(
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        timeout=8).json()[:10]
    items = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(requests.get,
                    f"https://hacker-news.firebaseio.com/v0/item/{i}.json",
                    timeout=6): i for i in ids}
        for fut in as_completed(futures):
            try:
                it = fut.result().json()
                if not it:
                    continue
                items.append({
                    "title": it.get("title", ""),
                    "hot": it.get("score", 0),
                    "url": it.get("url") or
                           f"https://news.ycombinator.com/item?id={it.get('id')}",
                    "extra": f"by {it.get('by','')} · {it.get('descendants',0)} comments",
                })
            except Exception:
                pass
    items.sort(key=lambda x: x["hot"], reverse=True)
    return _ok("hackernews", items)


def fetch_github():
    """GitHub Trending (抓取 HTML)"""
    r = requests.get("https://github.com/trending",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text
    items = []
    # repo 链接形如 <h2...><a href="/owner/repo">，h2 下紧跟 a 标签
    repos = re.findall(r'<h2[^>]*>\s*<a\s[^>]*href="(/[^/"]+/[^/"]+)"',
                       html)
    blocks = re.findall(r'<article class="Box-row">(.*?)</article>',
                        html, re.S)
    seen = set()
    for repo in repos[:30]:
        if repo in seen:
            continue
        seen.add(repo)
        owner, name = repo.strip("/").split("/", 1)
        url = "https://github.com" + repo
        items.append({
            "title": f"{owner}/{name}",
            "hot": "★",
            "url": url,
            "extra": "GitHub Trending · 今日热门仓库",
        })
    return _ok("github", items)


# --- 直连官方接口的源（无需 key） ---

def fetch_zhihu():
    """知乎热榜（官方 topstory API，匿名可用）"""
    url = "https://api.zhihu.com/topstory/hot-list?limit=50"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    items = []
    for it in data.get("data", []):
        tgt = it.get("target", {})
        title = tgt.get("title", "")
        if not title:
            continue
        # detail_text 形如 "785 万热度"
        hot = re.sub(r"[^\d]", "", it.get("detail_text", "") or "")
        qid = tgt.get("id", "")
        items.append({
            "title": title,
            "hot": hot,
            "url": f"https://www.zhihu.com/question/{qid}",
            "extra": (tgt.get("excerpt") or "")[:80],
        })
    return _ok("zhihu", items)


def fetch_douyin():
    """抖音热搜（snssdk 官方接口）"""
    url = "https://aweme.snssdk.com/aweme/v1/hot/search/list/"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    wlist = data.get("data", {}).get("word_list", []) or \
            data.get("word_list", [])
    items = []
    for w in wlist:
        word = w.get("word", "")
        if not word:
            continue
        items.append({
            "title": word,
            "hot": w.get("hot_value"),
            "url": "https://www.douyin.com/search/" + requests.utils.quote(word),
            "extra": w.get("label", ""),
        })
    return _ok("douyin", items)


def fetch_weibo():
    """微博热搜（尝试 m.weibo 容器接口；失败则返回降级提示）"""
    cid = ("106003type%3D25%26t%3D3%26disable_hot%3D1"
           "%26filter_type%3Drealtimehot")
    url = f"https://m.weibo.cn/api/container/getIndex?containerid={cid}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        cards = data.get("data", {}).get("cards", [])
        items = []
        for card in cards:
            for g in card.get("card_group", []):
                desc = g.get("desc", "")
                if desc:
                    items.append({
                        "title": desc,
                        "hot": g.get("desc_extr"),
                        "url": g.get("scheme", ""),
                        "extra": "",
                    })
        if items:
            return _ok("weibo", items)
    except Exception:
        pass
    return _fail("weibo", "微博接口需登录态，暂不可用（其他源正常）")


# ---------- 路由 ----------

# source -> fetcher
SOURCES = {
    "baidu": fetch_baidu,
    "bilibili": fetch_bilibili,
    "toutiao": fetch_toutiao,
    "hackernews": fetch_hackernews,
    "github": fetch_github,
    "zhihu": fetch_zhihu,
    "weibo": fetch_weibo,
    "douyin": fetch_douyin,
}

SOURCE_META = {
    "baidu":      {"name": "百度热搜", "region": "国内"},
    "bilibili":   {"name": "B站热门", "region": "国内"},
    "toutiao":    {"name": "今日头条", "region": "国内"},
    "zhihu":      {"name": "知乎热榜", "region": "国内"},
    "weibo":      {"name": "微博热搜", "region": "国内"},
    "douyin":     {"name": "抖音热搜", "region": "国内"},
    "hackernews": {"name": "Hacker News", "region": "国际"},
    "github":     {"name": "GitHub Trending", "region": "国际"},
}


def detect_region():
    """根据 Accept-Language 判断地域。含 zh → 'zh'，否则 'global'。"""
    al = (request.headers.get("Accept-Language") or "").lower()
    return "zh" if al.startswith("zh") or ",zh" in al or ";zh" in al else "global"


def _request_lang():
    """解析页面/API 的语言参数，显式 lang 优先，未传时回退 Accept-Language。"""
    lang = request.args.get("lang")
    if lang in ("zh", "en"):
        return lang
    return "zh" if detect_region() == "zh" else "en"


def _lang_url(path, lang):
    """给站内链接附加明确语言，避免跨页面后丢失当前语言。"""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}lang={lang}"


def _client_ip():
    """取真实客户端 IP。信任自建 Nginx 注入的 X-Forwarded-For（取最左一跳）。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return (request.remote_addr or "").strip()


def _client_country(ip):
    """地域判定：反代头优先（CF-IPCountry / X-Country-Code），GeoLite2 兜底。

    Cloudflare 与带 ngx_http_geoip2_module 的 Nginx 会直接注入国家码头，
    优先采信；否则用本地 GeoLite2 离线库查（无库返回 Unknown）。
    XX / T1 等为反代表示「未知」的占位码，忽略后走兜底。
    """
    for h in ("CF-IPCountry", "X-Country-Code"):
        c = (request.headers.get(h) or "").strip()
        if c and c.upper() not in ("XX", "T1"):
            return c.upper()
    return store.geoip_country(ip)


def get_source(source):
    """带缓存的单源抓取，超时快速失败"""
    if source not in SOURCES:
        return _fail(source, "unknown source")
    cached = _cached(source)
    if cached:
        return cached
    try:
        data = SOURCES[source]()
    except Exception as e:
        data = _fail(source, e)
    _set_cache(source, data)
    return data


def get_source_timeout(source):
    """带硬性截止时间的单源抓取，防止慢源拖垮整体响应"""
    if source not in SOURCES:
        return _fail(source, "unknown source")
    cached = _cached(source)
    if cached:
        return cached
    import concurrent.futures
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(get_source_uncached, source)
            try:
                data = fut.result(timeout=SOURCE_DEADLINE)
            except concurrent.futures.TimeoutError:
                data = _fail(source, f"抓取超时（{SOURCE_DEADLINE}s）")
                try:
                    fut.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
    except Exception as e:
        data = _fail(source, e)
    _set_cache(source, data)
    return data


def get_source_uncached(source):
    """不带缓存直接抓取（供 get_source_timeout 调用）"""
    try:
        return SOURCES[source]()
    except Exception as e:
        return _fail(source, e)


@app.route("/")
def index():
    region = detect_region()
    lang = _request_lang()
    sponsors = store.list_slots(region=region, active_only=True)
    # 服务端记曝光 + PV（best-effort，失败静默）
    store.record_pageview()
    # 记录访问明细（IP + 地域），供监控页统计 PV / 独立 IP / 地域分布
    cip = _client_ip()
    store.record_visit(cip, _client_country(cip))
    for s in sponsors:
        store.record_impression(s.get("slot_id"))
    requested_view = request.args.get("view", "words")
    requested_sort = request.args.get("sort", "rise")
    requested_cat = request.args.get("cat", "all")
    if requested_view not in ("words", "news"):
        requested_view = "words"
    if requested_sort not in ("rise", "hot", "new"):
        requested_sort = "rise"
    # SSR 首屏词链接携带当前榜单状态（非默认项），返回恢复滚动位置需要原样状态
    ssr_term_parts = [f"lang={lang}"]
    if requested_view != "words":
        ssr_term_parts.append(f"view={requested_view}")
    if requested_sort != "rise":
        ssr_term_parts.append(f"sort={requested_sort}")
    if requested_cat and requested_cat != "all":
        ssr_term_parts.append(f"cat={quote(requested_cat)}")
    ssr_term_qs = "&".join(ssr_term_parts)
    initial_terms = (
        _initial_terms_for_ssr(sort=requested_sort, lang=lang)
        if _seo_enabled() and requested_view != "news" else []
    )
    # 前端首屏分类条需要完整的维度顺序与计数，避免 SSR 阶段只出现部分维度就把其它标签“弄丢”。
    initial_dimensions = _stream_dimension_list(initial_terms, "words") if initial_terms else []
    initial_dimension_counts = _stream_dimension_counts(initial_terms, "words") if initial_terms else {}
    return render_template("index.html", sources=SOURCE_META,
                           sponsors=sponsors, site_name=config.SITE_NAME,
                           site_desc=SITE_DESC_EN if lang == "en" else SITE_DESC,
                           base_url=_base_url(), canonical=_abs(_lang_url("/", lang)),
                           seo_enabled=_seo_enabled(),
                           initial_terms=initial_terms,
                           initial_dimensions=initial_dimensions,
                           initial_dimension_counts=initial_dimension_counts,
                           ssr_term_qs=ssr_term_qs,
                           adsense_enabled=config.ADSENSE_ENABLED,
                           adsense_client=config.ADSENSE_CLIENT,
                           baidu_ads_enabled=config.BAIDU_ADS_ENABLED,
                           baidu_cpro_id=config.BAIDU_ADS_CPRO_ID,
                           default_lang=lang,
                           lang_toggle_url=_lang_url(
                               "/", "en" if lang == "zh" else "zh"),
                           lang_toggle_label="中" if lang == "en" else "EN")


@app.route("/api/sources")
def api_sources():
    return jsonify({"sources": SOURCE_META})


@app.route("/api/hot/<source>")
def api_hot(source):
    return jsonify(get_source_timeout(source))


@app.route("/api/all")
def api_all():
    """并发聚合所有源，每源带硬性超时，整体响应可控"""
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(get_source_timeout, s): s for s in SOURCES}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results[s] = fut.result()
            except Exception as e:
                results[s] = _fail(s, e)
    return jsonify({"fetched_at": int(time.time()), "data": results})


# ---------- 热词追踪路由（新主功能）----------
# 注：词维度重构后，旧 JSON API /api/trending /api/top /api/term 已删除。
# tracker 层仅作内部数据源（HF 模型卡进词池、详情页 HF 区块）。


@app.route("/term/<path:term_name>")
def term_detail(term_name):
    """通用热词聚合 HTML 详情页（SEO 可索引长尾页）。

    词维度重构后：任何词（新闻抽词 / HF 模型词）都有页——主体是该词的
    相关报道聚合 + 词热度信息；HF 模型词额外保留官方/社区/arXiv 区块。
    进程内 TTL 缓存（HF live 区块是慢路径）。未找到 → 404 HTML + noindex。
    """
    lang = _request_lang()
    # 缓存键按 canonical 归一（GPT-5 / gpt-5 / GPT5 / GPT 5 共享同一缓存条目）；
    # _word_detail 内部同样归一，纯大小写差异本就同键，这里补上别名/标点归一。
    canon = (terms_mod.normalize_term(term_name) if terms_mod
             else term_name.lower()) or term_name.lower()
    key = f"{lang}:{canon}"
    data = _detail_cached(key)
    if data is None:
        data = _word_detail(term_name, lang=lang)
        _detail_set_cache(key, data)

    if not data.get("ok"):
        abort(404)

    t = data["term"]
    slug = t.get("term") or term_name
    canonical = _abs(_lang_url(f"/term/{quote(slug)}", lang))
    if lang == "zh":
        desc = (f"{slug} 最新动态聚合：{t.get('news_cnt', 0)} 篇相关报道，"
                f"热度 {t.get('hot', 0)}，追踪 {slug} 的模型、产品与行业进展。")
    else:
        desc = (f"{slug}: {t.get('news_cnt', 0)} related reports aggregated, "
                f"hotness {t.get('hot', 0)}. Track the latest on {slug}.")
    # 返回首页时回显进入词条页前的榜单状态（view/sort/cat 非默认项）。
    # 滚动恢复按保存的 scrollY 像素落位，若返回后榜单被重置为默认 Trending，
    # 像素会落在不同排序的列表上 → 位置错乱（20260901 #7 边界修复）。
    back_parts = []
    back_view = request.args.get("view")
    if back_view in ("news",):
        back_parts.append(f"view={back_view}")
    back_sort = request.args.get("sort")
    if back_sort in ("hot", "new"):
        back_parts.append(f"sort={back_sort}")
    back_cat = request.args.get("cat")
    if back_cat and back_cat != "all":
        back_parts.append(f"cat={quote(back_cat)}")
    home_url = _lang_url("/", lang) + ("&" + "&".join(back_parts) if back_parts else "") \
        + "&scroll_back=1"
    return render_template("term_detail.html", word=data, lang=lang,
                           site_name=config.SITE_NAME,
                           site_desc=desc[:160], base_url=_base_url(),
                           canonical=canonical, seo_enabled=_seo_enabled(),
                           home_url=home_url,
                           lang_toggle_url=_lang_url(
                               request.path, "en" if lang == "zh" else "zh"),
                           lang_toggle_label="中文" if lang == "en" else "English")


@app.route("/terms")
def terms():
    """服务条款页（中英双语，SEO 可索引）。

    内容为静态文案，updated_at 由 SITE_TERMS_UPDATED 常量确定。canonical 指向 /terms。
    英文版与隐私声明置于中文之前（适配境外主体 + Adsterra 广告合规要求）。
    """
    return render_template("terms.html", site_name=config.SITE_NAME,
                           site_desc="Terms of Service / 服务条款",
                           base_url=_base_url(), canonical=_abs("/terms"),
                           seo_enabled=_seo_enabled(),
                           contact_email=config.CONTACT_EMAIL,
                           updated_at=SITE_TERMS_UPDATED)


@app.errorhandler(404)
def not_found(e):
    """404 → 简单 HTML（noindex），避免爬虫索引不存在的 term 详情页。"""
    html = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\">"
        "<title>404 · 未找到</title></head>"
        "<body style=\"font-family:sans-serif;text-align:center;padding:60px\">"
        "<h1>404</h1><p>未找到该热词。</p>"
        "<p><a href=\"/\">← 返回首页</a></p></body></html>"
    )
    return Response(html, status=404, mimetype="text/html; charset=utf-8")


@app.route("/api/dims")
def api_dims():
    """维度热词：按 AI 维度（模型与技术/产品与应用/商业与投融资/...）分组的热点卡。
    可选 ?dimension=模型与技术 只返回该维度；?lang=zh/en 投影对应语言（默认 zh）。
    每张卡含 official_url 直链官方原文。"""
    lang = request.args.get("lang", "zh")
    return jsonify(dims.get_dims(dimension=request.args.get("dimension"), lang=lang))


@app.route("/api/stream")
def api_stream():
    """统一卡片流：view=words（词卡，默认）| view=news（逐条新闻，旧逻辑原样）。

    参数：
      lang：默认按 Accept-Language（detect_region → zh/global → zh/en）。
      view：words | news，默认 words。words 读 terms 层 cache/words.json，
            词卡内嵌 top-3 报道；news 合并 model 卡（tracker）+ news 卡（dims）。
      sort：rise（上升/环比）/ hot（热度）/ new（words 视图=新奇度新词发现，
            news 视图=published 时间序），默认 rise。
    返回 {ok, view, fetched_at, count, dimension_list, dimension_counts, terms}。
    只读各自文件缓存，秒回，无需并发。
    """
    region = detect_region()
    lang = request.args.get("lang", "zh" if region == "zh" else "en")
    if lang not in ("zh", "en"):
        lang = "zh" if region == "zh" else "en"
    view = request.args.get("view", "words")
    if view not in ("words", "news"):
        view = "words"
    sort = request.args.get("sort", "rise")
    if sort not in ("rise", "hot", "new"):
        sort = "rise"

    if view == "words":
        cards, fetched_at = (terms_mod.get_word_cards(sort, lang,
                                                       limit=WORD_STREAM_LIMIT)
                             if terms_mod else ([], 0))
        cards = _dedupe_stream_cards(cards)[:WORD_STREAM_LIMIT]
        return jsonify({
            "ok": True,
            "view": "words",
            "fetched_at": fetched_at,
            "count": len(cards),
            "dimension_list": _stream_dimension_list(cards, "words"),
            "dimension_counts": _stream_dimension_counts(cards, "words"),
            "terms": cards,
        })

    model_cards, m_at = tracker.get_model_cards(lang)
    news_cards, n_at = dims.get_news_cards(lang)
    cards = _dedupe_stream_cards(model_cards + news_cards)

    # 排序键：rise→trend, hot→score, new→published（统一字段）
    # 先按身份升序，再按榜单值倒序；稳定排序保证同值卡片每次顺序相同。
    # 「二次排序」根因：历史卡（get_news_cards 打 from_history 标记）的时效分
    # 每次刷新重算，60 条之后的历史卡会随衰减反复重排。修复：历史卡按
    # published 降序固定排序（存档语义），当前卡仍按榜单键排序。
    current = [c for c in cards if not c.get("from_history")]
    history = [c for c in cards if c.get("from_history")]
    current.sort(key=lambda x: _stream_card_identity(x) or ("", ""))
    sort_key = {"rise": lambda x: _stream_number(x, "trend"),
                "hot":  lambda x: _stream_number(x, "score"),
                "new":  lambda x: x.get("published", "") or ""}[sort]
    current.sort(key=sort_key, reverse=True)
    history.sort(key=lambda x: _stream_card_identity(x) or ("", ""))
    history.sort(key=lambda x: x.get("published", "") or "", reverse=True)
    cards = current + history

    fetched_at = max(m_at, n_at)
    return jsonify({
        "ok": True,
        "view": "news",
        "fetched_at": fetched_at,
        "count": len(cards),
        "dimension_list": _stream_dimension_list(cards, "news"),
        "dimension_counts": _stream_dimension_counts(cards, "news"),
        "terms": cards,
    })


# ---------- HuggingFace 独立排序页（/hf 页面 + /api/hf JSON）----------
# 用户需求：HuggingFace 数据最可靠，单独成页作为「开源动向」，可按
# 趋势分 / 点赞 / 下载量排序，并给每个模型打上合理的标签。
# 原则：复用 tracker 缓存，请求路径不抓 HF；只在内存重排，零新后台线程。
_HF_SORT_KEYS = {"trending": "trending_score", "likes": "likes",
                 "downloads": "downloads"}
# 文件缓存缺失（冷启动）时，get_model_cards 返回空 → 回退 get_terms 的
# 对应 sort（自带快速兜底：只抓 HF ~1s，不触发 arXiv 慢路径）。
_HF_SORT_FALLBACK = {"trending": "trending", "likes": "top",
                     "downloads": "top"}


def _hf_models_for(sort, lang="zh"):
    """HF 模型卡列表（复用 tracker 缓存，秒回）。

    1) 首选 tracker.get_model_cards(lang)：trending 文件缓存，统一卡片
       schema（likes/downloads/trending_score/tags/pipeline_tag/community/
       papers 原样透传）；
    2) 冷启动缓存缺失时回退 tracker.get_terms(sort)（自带快速兜底）；
    3) 排序：trending 用趋势分；likes/downloads 在内存按对应字段重排
       （HF 原生 likes 排序经 get_terms('top') 拿到，downloads 内存重排）。
    """
    cards, fetched_at = tracker.get_model_cards(lang)
    if not cards:
        data = tracker.get_terms(_HF_SORT_FALLBACK.get(sort, "trending"))
        cards = data.get("terms") or []
        fetched_at = data.get("fetched_at", 0)
    key = _HF_SORT_KEYS.get(sort, "trending_score")
    cards = list(cards)
    cards.sort(key=lambda c: _stream_number(c, key), reverse=True)
    return cards, fetched_at


@app.route("/hf")
def hf_page():
    """HuggingFace 模型排序页（独立页，作为开源动向）。

    服务端渲染（SEO 可索引）；?sort=trending|likes|downloads&lang=zh|en。
    排序/语言切换都是普通链接，前端零 fetch，自包含。
    """
    lang = _request_lang()
    sort = request.args.get("sort", "trending")
    if sort not in _HF_SORT_KEYS:
        sort = "trending"
    models, fetched_at = _hf_models_for(sort, lang)
    canonical = _abs(_lang_url("/hf", lang))
    if lang == "zh":
        desc = ("HuggingFace 开源模型榜：按趋势分 / 点赞 / 下载量排序，"
                "数据来自 HuggingFace 官方，追踪 AI 开源动向。")
    else:
        desc = ("HuggingFace open-source model leaderboard: sort by trend "
                "score, likes, or downloads. Official HF data, tracking "
                "AI open-source momentum.")
    toggle = _lang_url(f"/hf?sort={sort}", "en" if lang == "zh" else "zh")
    return render_template(
        "hf.html", models=models, sort=sort, fetched_at=fetched_at,
        lang=lang, site_name=config.SITE_NAME, site_desc=desc,
        base_url=_base_url(), canonical=canonical, seo_enabled=_seo_enabled(),
        home_url=_lang_url("/", lang), lang_toggle_url=toggle,
        lang_toggle_label="中文" if lang == "en" else "English")


@app.route("/api/hf")
def api_hf():
    """HF 模型排序 JSON API。

    ?sort=trending|likes|downloads（默认 trending）&lang=zh|en。
    返回 {ok, sort, lang, fetched_at, count, terms}；terms 为模型卡列表
    （含 term/author/pipeline_tag/tags/likes/downloads/trending_score/
    official_url/community/papers）。只读 tracker 文件缓存，秒回。
    """
    lang = request.args.get("lang", "zh")
    if lang not in ("zh", "en"):
        lang = "zh"
    sort = request.args.get("sort", "trending")
    if sort not in _HF_SORT_KEYS:
        sort = "trending"
    models, fetched_at = _hf_models_for(sort, lang)
    return jsonify({
        "ok": True,
        "sort": sort,
        "lang": lang,
        "fetched_at": fetched_at,
        "count": len(models),
        "terms": models,
    })


@app.route("/api/word/<path:term_name>")
def api_word(term_name):
    """单词聚合 JSON：词元信息 + 全量关联报道（≤50）。

    主页词卡「展开更多」与 /term/<name> 详情页共用数据源。
    读 terms 表 + news_cards LIKE 查询，进程内 TTL 缓存 300s。
    """
    # API 保持历史默认 zh；前端展开请求会显式传入当前页面的 lang。
    data = _word_detail(term_name, lang=request.args.get("lang", "zh"))
    if not data.get("ok"):
        return jsonify({"ok": False, "error": "term not found"}), 404
    return jsonify(data)


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ---------- 全站搜索 v2（独立结果页 + 加权打分 + 高亮 + 漏斗）----------
import html as _html


def _highlight(value, q):
    """对单字段做 <mark> 包裹的高亮（HTML 安全）。

    先 HTML escape 防 XSS，再对查询词做大小写不敏感的标记。多词查询时拆词
    高亮（任意子串命中即标），避免「GPT-5」搜「GPT」时高亮缺失。
    返回 escape 后的字符串（可能含 <mark>…</mark>）。
    """
    if not value or not q:
        return _html.escape(value or "") if value else ""
    s = _html.escape(str(value))
    # 拆词：连续空白当分隔，全小写后逐词查
    words = [w for w in q.lower().split() if w]
    if not words:
        return s
    # 按词从长到短替换（避免短词吃掉长词的高亮边界）
    for w in sorted(set(words), key=len, reverse=True):
        # 大小写不敏感，但保留原文大小写：用 re.sub + lambda
        s = re.sub(r"(?i)(" + re.escape(w) + r")",
                   lambda m: f"<mark>{m.group(1)}</mark>", s)
    return s


# 字段权重：标题命中权重最高，摘要次之，来源/作者最弱（v2 加权打分）
_FIELD_WEIGHTS = {
    "title_zh": 30, "title_en": 30, "term": 30,   # 标题/模型名/热词名
    "display_zh": 30,                              # 热词中文别名（词维度重构）
    "summary_zh": 12, "summary_en": 12, "summary": 12,  # 摘要
    "source": 8, "author": 8,                              # 来源/作者
    "title": 25,                                           # 兜底 title（zh/en 投影后字段）
}


def _score_card(card, q):
    """对单张卡按 q 加权打分，返回 {score, matched_fields, card_with_highlights}。

    命中规则：查询词拆词后，任何词出现在字段里即记权重。多词全中得高分。
    热度仅做排序兜底：score + log(hot+1) * 1.5，避免高热度低相关卡霸榜。
    matched_fields：['title', 'summary', ...] 用于前端「为什么命中」展示。
    返回的 dict 已附 _highlight_<field> 字段，供 SSR 直接渲染（前端 escapeHtml 后注入）。
    """
    if not q or not card:
        return None
    words = [w for w in q.lower().split() if w]
    if not words:
        return None

    fields = {
        "title": card.get("title"),
        "title_zh": card.get("title_zh"),
        "title_en": card.get("title_en"),
        "term": card.get("term"),
        "display_zh": card.get("display_zh"),
        "summary": card.get("summary"),
        "summary_zh": card.get("summary_zh"),
        "summary_en": card.get("summary_en"),
        "source": card.get("source") or card.get("official_label"),
        "author": card.get("author"),
    }
    matched = []
    score = 0
    for fname, fval in fields.items():
        if not fval:
            continue
        lv = str(fval).lower()
        # 该字段有几词命中（全部命中得满分，按命中比例算）
        hit = sum(1 for w in words if w in lv)
        if hit == 0:
            continue
        ratio = hit / len(words)         # 部分命中给部分分
        w = _FIELD_WEIGHTS.get(fname, 5)
        # 字段越短命中权重越高（避免摘要超长但只一处命中的卡压过标题全命中的卡）
        len_factor = 1.0 if len(lv) < 80 else 0.8
        score += w * ratio * len_factor
        matched.append(fname)

    if not matched:
        return None
    # 热度兜底
    hot = card.get("hot") or card.get("score") or 0
    score += math.log(max(int(hot), 0) + 1) * 1.5

    out_card = dict(card)
    out_card["_score"] = round(score, 2)
    out_card["_matched_fields"] = matched
    # 高亮关键字段（SSR 直接渲染，前端不要再次 escape）
    if out_card.get("title"):
        out_card["_highlight_title"] = _highlight(out_card["title"], q)
    if out_card.get("term"):
        out_card["_highlight_term"] = _highlight(out_card["term"], q)
    if out_card.get("summary"):
        out_card["_highlight_summary"] = _highlight(out_card["summary"], q)
    return out_card


def _search_pool(lang):
    """拉搜索池：model 当轮 + news 当轮 + news 历史库（合并去重）。"""
    pool = []
    seen = set()
    # model 卡
    try:
        model_cards, _ = tracker.get_model_cards(lang)
        for c in model_cards:
            cid = c.get("id") or c.get("term")
            if cid and cid in seen:
                continue
            if cid: seen.add(cid)
            pool.append(c)
    except Exception:
        pass
    # news 当轮
    try:
        news_cards, _ = dims.get_news_cards(lang)
        for c in news_cards:
            cid = c.get("id") or c.get("official_url")
            if cid and cid in seen:
                continue
            if cid: seen.add(cid)
            pool.append(c)
    except Exception:
        pass
    # news 历史库（近 30 天全量导入，扩大召回）
    try:
        import news_store
        if news_store._DB_OK:
            hist = news_store.list_history_cards(limit=500, days=30)
            for hc in hist:
                pc = dims._project_card(hc, lang)
                pc["kind"] = "news"
                url = pc.get("official_url") or pc.get("title", "")
                pc["id"] = url
                pc["hot"] = pc.get("hot") or pc.get("score", 0)
                pc["official_label"] = pc.get("source", "")
                pc.setdefault("summary", pc.get("summary_zh", "") if lang == "zh"
                              else pc.get("summary_en", ""))
                if url and url in seen:
                    continue
                if url: seen.add(url)
                pool.append(pc)
    except Exception:
        pass
    # 词卡实体（词维度重构）：搜词时顶部可命中「热词卡」，点击进词聚合页
    try:
        if terms_mod:
            word_cards, _ = terms_mod.get_word_cards("hot", lang, limit=200)
            for wc in word_cards:
                wid = "word:" + (wc.get("id") or "")
                if not wc.get("id") or wid in seen:
                    continue
                seen.add(wid)
                pool.append(wc)
    except Exception:
        pass
    return pool


def _do_search(q, lang, limit):
    """v2 核心搜索：对全池打分排序 + 返回 {count, terms, matched_in_history}。

    terms 每项含 _score / _matched_fields / _highlight_* 字段。
    matched_in_history：命中是否来自历史库（前端显示「含历史归档」标记）。
    词维度重构：kind=="word" 的热词卡从主结果流剥离，经 word_hits 单独返回
    （≤3），前端在结果顶部渲染热词卡区，避免与逐条报道重复。
    """
    pool = _search_pool(lang)
    scored = []
    word_hits = []
    history_hits = 0
    for c in pool:
        s = _score_card(c, q)
        if not s:
            continue
        # 标记是否来自历史库（pool 加的来源标记）
        if c.get("_from_history"):
            s["_from_history"] = True
            history_hits += 1
        if c.get("kind") == "word":
            word_hits.append(s)
            continue
        scored.append(s)
    scored.sort(key=lambda x: x.get("_score", 0), reverse=True)
    word_hits.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return scored[:limit], word_hits[:3], history_hits


@app.route("/search")
def search_page():
    """独立搜索结果页（SSR），URL 可分享：/search?q=...&lang=...。

    流程：
      1) 记录搜索词（best-effort）。
      2) 取 query + lang（默认按 region）。
      3) 打分排序 + 切高亮，直接渲染 search.html。
      4) 空结果：带「你可能想搜」补全（基于热门搜索词 + 补全接口）。
    """
    q = (request.args.get("q") or "").strip()
    lang = _request_lang()
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 100))

    cip = _client_ip()
    store.record_search_query(q, lang=lang, ip=cip,
                              country=_client_country(cip))

    results, word_hits, history_hits = [], [], 0
    suggest = []
    if q:
        results, word_hits, history_hits = _do_search(q, lang, limit)
        if not results and not word_hits:
            suggest = store.search_suggest(q[:20], limit=8)

    return render_template(
        "search.html",
        q=q, lang=lang, terms=results, word_hits=word_hits,
        count=len(results), history_hits=history_hits,
        suggest=suggest, site_name=config.SITE_NAME,
        home_url=_lang_url("/", lang),
        lang_toggle_url=_lang_url(
            request.path + ("?q=" + quote(q) if q else ""),
            "en" if lang == "zh" else "zh"),
        lang_toggle_label="中文" if lang == "en" else "English",
    )


@app.route("/api/search/suggest")
def api_search_suggest():
    """搜索建议接口：GET ?q=...&limit=8。

    基于近 30 天热门搜索词做前缀/包含匹配，供前端搜索框下拉补全。
    空串或 < 1 字符直接返回空。绝不抛异常（DB 不可用 → 空数组）。
    """
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit", "8"))
    except ValueError:
        limit = 8
    limit = max(1, min(limit, 20))
    if len(q) < 1:
        return jsonify({"ok": True, "items": []})
    items = store.search_suggest(q, limit=limit)
    return jsonify({"ok": True, "items": items})


@app.route("/api/search/click", methods=["POST"])
def api_search_click():
    """搜索结果点击追踪：POST {q, url}，落 search_clicks 表。

    前端在用户点击结果卡外链时发（不阻塞跳转，用 navigator.sendBeacon）。
    失败静默——这是漏斗统计，不影响主流程。
    """
    payload = request.get_json(silent=True) or request.form or {}
    q = (payload.get("q") or "").strip()[:80]
    url = (payload.get("url") or "").strip()[:500]
    if not q:
        return jsonify({"ok": False, "err": "empty q"}), 400
    cip = _client_ip()
    store.record_search_click(q, url=url, ip=cip,
                              country=_client_country(cip))
    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    """全站搜索 JSON 接口（v2）：带相关性打分 + 高亮。

    返回 {ok, query, count, history_hits, terms}。
    terms 每项含 _score / _matched_fields / _highlight_title / _highlight_summary /
    _highlight_term，前端用 v-html 注入（已 HTML escape 安全）。
    """
    q = (request.args.get("q") or "").strip()
    region = detect_region()
    lang = request.args.get("lang", "zh" if region == "zh" else "en")
    if lang not in ("zh", "en"):
        lang = "zh" if region == "zh" else "en"
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 100))

    cip = _client_ip()
    store.record_search_query(q, lang=lang, ip=cip,
                              country=_client_country(cip))
    if not q:
        return jsonify({"ok": True, "query": "", "count": 0,
                        "history_hits": 0, "terms": [], "word_hits": []})

    results, word_hits, history_hits = _do_search(q, lang, limit)
    return jsonify({"ok": True, "query": q, "count": len(results),
                    "history_hits": history_hits, "terms": results,
                    "word_hits": word_hits,
                    "fetched_at": int(time.time())})


# ---------- SEO 路由：robots / sitemap / favicon ----------

@app.route("/robots.txt")
def robots():
    base = _base_url()
    lines = []
    if _seo_enabled():
        lines.extend([
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin",
            "Disallow: /api/",
        ])
        if base:
            lines.append(f"Sitemap: {base}/sitemap.xml")
    else:
        # SEO 关闭 → 全站禁止索引
        lines.extend(["User-agent: *", "Disallow: /"])
    lines.append("")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap():
    base = _base_url()
    # BASE_URL 未设 → 无法生成绝对 URL，sitemap 退化为仅首页（相对也无意义，返回空集）
    urls = []
    if base:
        urls.append(base + "/")
        if _seo_enabled():
            # 服务条款页（静态，常驻索引）
            urls.append(f"{base}/terms")
            for slug in _sitemap_terms():
                if not slug:
                    continue
                urls.append(f"{base}/term/{quote(slug)}")
                if len(urls) >= config.SITEMAP_MAX_URLS:
                    break
    now = time.strftime("%Y-%m-%d", time.gmtime())
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        body.append(f"  <url><loc>{u}</loc><lastmod>{now}</lastmod></url>")
    body.append("</urlset>")
    return Response("\n".join(body), mimetype="application/xml")


# 预生成 favicon PNG（32×32，蓝色圆角底 + 白色“A”字标），纯标准库生成，
# 内联为 data 以避免引入静态目录。PNG 是浏览器标签页兼容性最高的格式
# （旧版 Chrome/Safari/Edge 不渲染 image/svg+xml favicon）。
_FAVICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAApklEQVR42u2X"
    "UQ6AMAhDuaEX88765wdhpMWyOJVkXxvtm4kDzFxs+3FEKwpkzy/LYpSkBBhC"
    "ZAmZEWseQiAJaoALAj2MQrB67wJgtUoAGcQPMAXg23+B8hFqA2Ah1gFQVsP1"
    "ABCDcll+PMDdWBugIkr3h0oxKUD1k7J5prx9aU7oqPdMvqlvz2pYV7cDA/jZ"
    "QNVqIVrhdDQLIJwPleYZRDohdwN4vxPF7lV1CYFtagAAAABJRU5ErkJggg=="
)
_FAVICON_PNG = base64.b64decode(_FAVICON_PNG_B64)


@app.route("/favicon.ico")
def favicon():
    # 返回真实 PNG（而非 SVG），最大化浏览器标签页兼容性；附带一年强缓存
    # 头，避免每次页面加载都重新请求 favicon。
    resp = Response(_FAVICON_PNG, mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/favicon.svg")
def favicon_svg():
    """现代浏览器可用的 SVG 版本（模板里作为更高优先级的 icon 引用）。"""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="7" fill="#4f8cff"/>'
           '<text x="16" y="23" font-size="20" text-anchor="middle" '
           'fill="#fff" font-family="sans-serif">A</text></svg>')
    resp = Response(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    """iOS 添加到主屏幕用的图标（此处复用 32px PNG，浏览器会自行缩放）。"""
    resp = Response(_FAVICON_PNG, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------- 赞助位点击跳转 ----------
@app.route("/api/click/<path:slot_id>")
def sponsor_click(slot_id):
    """记录点击 + 302 跳转赞助商链接。slot 不存在或无链接 → 跳首页。"""
    slot = store.get_slot(slot_id)
    store.record_click(slot_id)
    url = (slot or {}).get("link_url") or "/"
    return redirect(url, code=302)


# ---------- 管理后台 ----------
# ADMIN_TOKEN 未设 → 所有 /admin/* 返回 404（隐身，不只是锁）。
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not config.ADMIN_TOKEN:
            abort(404)
        token = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                 or request.args.get("token", "").strip()
                 or session.get("admin_token", ""))
        if not token or not hmac.compare_digest(token, config.ADMIN_TOKEN):
            # 未登录 → 登录页（仅页面请求，带 next 回跳）；API 请求返 401
            if request.method == "GET" and "application/json" not in request.headers.get("Accept", ""):
                nxt = quote(request.path, safe="")
                return redirect(f"/admin/login?next={nxt}", code=302)
            abort(401)
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not config.ADMIN_TOKEN:
        abort(404)
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        if token and hmac.compare_digest(token, config.ADMIN_TOKEN):
            session["admin_token"] = token
            nxt = request.args.get("next") or "/admin"
            # 只允许站内相对路径回跳，防开放重定向
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/admin"
            return redirect(nxt, code=302)
        return render_template("admin_login.html", error="令牌错误"), 401
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_token", None)
    return redirect("/admin/login", code=302)


@app.route("/admin")
@admin_required
def admin_home():
    slots = store.list_slots(active_only=False)
    return render_template("admin.html", slots=slots, site_name=config.SITE_NAME)


@app.route("/admin/sponsors", methods=["POST"])
@admin_required
def admin_upsert_sponsor():
    data = request.form.to_dict()
    sid = store.upsert_slot(data)
    if not sid:
        return jsonify({"ok": False, "error": "保存失败（DB 不可用或 slot_id 为空）"}), 500
    return jsonify({"ok": True, "slot_id": sid})


@app.route("/admin/sponsors/<slot_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_sponsor(slot_id):
    new_state = store.toggle_slot(slot_id)
    if new_state is None:
        return jsonify({"ok": False, "error": "未找到或 DB 不可用"}), 404
    return jsonify({"ok": True, "active": new_state})


@app.route("/admin/sponsors/<slot_id>/delete", methods=["POST"])
@admin_required
def admin_delete_sponsor(slot_id):
    ok = store.delete_slot(slot_id)
    if not ok:
        return jsonify({"ok": False, "error": "删除失败"}), 404
    return jsonify({"ok": True})


@app.route("/admin/stats")
@admin_required
def admin_stats():
    return jsonify(store.stats_30d())


# ---------- 流量监控页（仅管理员，只看访问量 + 独立 IP + 地域，不含广告）----------
@app.route("/monitor")
@admin_required
def monitor():
    return render_template("monitor.html", site_name=config.SITE_NAME)


@app.route("/monitor/api")
@admin_required
def monitor_api():
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30
    return jsonify(store.monitor_stats(days))


@app.route("/monitor/api/search")
@admin_required
def monitor_search_api():
    """监控页搜索词统计：热门搜索词 Top-N + 近期搜索 + 总量。"""
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30
    try:
        top_n = int(request.args.get("top", "20"))
    except ValueError:
        top_n = 20
    return jsonify(store.search_stats(days, top_n))


@app.route("/monitor/api/search/funnel")
@admin_required
def monitor_search_funnel_api():
    """搜索→点击漏斗：每个热门搜索词的搜索次数、点击次数、点击率。

    数据源：search_queries + search_clicks。供 monitor.html 漏斗卡用。
    """
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30
    try:
        top_n = int(request.args.get("top", "15"))
    except ValueError:
        top_n = 15
    return jsonify(store.search_funnel(days, top_n))


if __name__ == "__main__":
    print("=" * 50)
    print(" 热点聚合服务启动中...")
    print(" 打开 http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
