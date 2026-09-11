"""
热点聚合服务 —— 通过多个免费 API / RSS / 网页抓取聚合当前最新热点。

运行：
    pip install flask requests
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

import io
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
import ratelimit
import text_utils
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
        hd = (cached.get("term") or {}) if cached.get("ok") else None
        # community 按页面语言分流：缓存 hf:<id> 不带 lang（zh/en 共享），
        # 必须浅拷贝后按 lang 重建，不能污染缓存（2026-09-05 需求）
        if hd and isinstance(hd, dict) and "community" in hd and hd.get("term"):
            hd = {**hd, "community": tracker.community_links(hd["term"], lang)}
        return hd

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
                # 词条自有数据增量：term_snapshots 聚合的近 7 天活跃度（无数据 []）
                "trend": terms_mod.get_term_trend(canon, days=7),
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
                "trend": [],
                "hf": {"full_id": hf_detail.get("full_id", ""),
                       "likes": hf_detail.get("likes", 0),
                       "trending_score": hf_detail.get("trending_score", 0),
                       "downloads": hf_detail.get("downloads", 0),
                       "official_url": hf_detail.get("official_url", ""),
                       "author": hf_detail.get("author", ""),
                       "tags": hf_detail.get("tags") or []},
                "hf_detail": hf_detail, "legacy_hf": True}
    return {"ok": False, "trend": []}


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


def _initial_dimension_meta_for_ssr(sort="rise", lang="zh"):
    """首页分类条 SSR 元数据：基于全量词卡（与 /api/stream 同口径）计算。

    返回 (dimension_list, dimension_counts, total)。分类条首屏直接显示全量
    计数，避免加载完成后 all 20→100、other 1→5 的数值跳变闪烁——标签数字
    从一开始就是最终值，全量列表到达后数字不再变化。
    任何失败返回 ([], {}, 0)。
    """
    try:
        if not terms_mod:
            return [], {}, 0
        cards, _ = terms_mod.get_word_cards(sort=sort, lang=lang,
                                            limit=WORD_STREAM_LIMIT)
        cards = _dedupe_stream_cards(cards)[:WORD_STREAM_LIMIT]
        if not cards:
            return [], {}, 0
        return (_stream_dimension_list(cards, "words"),
                _stream_dimension_counts(cards, "words"),
                len(cards))
    except Exception:
        return [], {}, 0


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
# 2026-09-11 SEO（BWT「重复 meta description」修复）：英文描述统一 150-158 字符
# （BWT 建议 150-160，过长会在 SERP 被截断），中文描述 ~90-105 字；每个页面
# 形态（首页 / HF 榜 / 词条页 / 条款 / 隐私 / 搜索）各有独立描述，保证同一语言下
# 任意两个 URL 的描述互不相同——BWT 的重复判定是 URL 两两比对，逐页唯一描述
# 是从源头消除重复告警的做法。
SITE_DESC = ("AI 热点聚合平台：汇总 36 个 RSS 源、HuggingFace 模型榜与 arXiv 论文，"
             "每日多次更新上升最快、最热、最新的 AI 热词榜与事件卡，"
             "中英双语免费查看，无需注册。")
SITE_DESC_EN = ("AI trend aggregation from 36 RSS sources: rising AI keywords, "
                "HuggingFace model trends, arXiv papers and community buzz on "
                "one bilingual board, updated daily.")
# /terms 与 /privacy 是单页内嵌双语的裸 URL 页，描述中英并列（SERP 语言随查询词）；
# 站名取 config.SITE_NAME（生产为 AITrendWatch），避免品牌写死导致漂移。
_SITE_NAME = config.SITE_NAME or "AITrendWatch"
SITE_TERMS_DESC = (f"{_SITE_NAME} Terms of Service: acceptable use, advertising, "
                   "intellectual property, disclaimers and liability for this AI "
                   "trend aggregation site. 服务条款中英双语。")
SITE_PRIVACY_DESC = (f"{_SITE_NAME} Privacy Policy: what we collect (IP, GeoIP, "
                     "analytics, cookies), how ads and analytics providers use it, "
                     "retention and opt-out. 隐私政策中英双语。")
# /search 为 noindex 页（见 search 路由），描述仅用于分享卡片与爬虫兜底。
SEARCH_DESC = (f"在 {_SITE_NAME} 聚合的 AI 热点库中搜索热词、HuggingFace 模型与 "
               "arXiv 论文，结果按热度与时效排序，支持中英双语检索，"
               "覆盖模型发布、产品动向与行业动态。")
SEARCH_DESC_EN = ("Search AI trends, hot keywords, HuggingFace models and arXiv papers "
                  f"across {_SITE_NAME}'s aggregated news archive, ranked by heat and "
                  "recency.")

# 服务条款最后更新日期（修改条款时同步更新）。
SITE_TERMS_UPDATED = "2026-08-26"
# 隐私政策页最后更新日期（2026-09-07 P1：独立 /privacy 页补齐运营合规）。
SITE_PRIVACY_UPDATED = "2026-09-07"



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
    """页面语言：显式 ?lang=zh → 中文；其余一律英文（主语言）。

    2026-09-11 SEO：**不再按 Accept-Language 协商**。协商会让爬虫在裸 URL `/`
    上拿到与 `/?lang=en` 逐字节相同的 HTML（同 title / 同 description），
    Bing 因此报「重复 meta description」。现在裸 URL 是英文的规范页
    （`/?lang=en` 由 `_canonical_lang_redirect` 301 收敛到 `/`），
    中文只在显式 `?lang=zh` 上出现，两个 URL 各自唯一。
    """
    return "zh" if request.args.get("lang") == "zh" else "en"


def _lang_url(path, lang):
    """给站内链接标注语言：英文用裸 URL（主语言），中文追加 lang=zh。

    英文是裸 URL 规范页，因此 en 变体不再携带 `lang=en`——否则内链会指向
    301（多一跳，且爬虫反复发现 `?lang=en` 变体）；zh 变体显式标注，
    保证中文页面点击后不丢语言。
    """
    if lang != "zh":
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}lang=zh"


def _qs(parts):
    """把已编码的 'k=v' 段拼成查询串（含前导 '?'），无段时返回 ''。"""
    segs = [p for p in parts if p]
    return ("?" + "&".join(segs)) if segs else ""


def _with_query(path, parts):
    """在 path 后追加已编码的 'k=v' 段（保持顺序），无段时原样返回。"""
    segs = [p for p in parts if p]
    if not segs:
        return path
    sep = "&" if "?" in path else "?"
    return path + sep + "&".join(segs)


def _clip_desc(text, limit):
    """按长度裁剪描述：英文按词边界、CJK 按字符边界，末尾加省略号。"""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 1)].rstrip()
    if " " in cut:
        cut = cut[:cut.rfind(" ")].rstrip()
    return cut + "…"


# 英文是裸 URL 主语言：显式 `?lang=en` 变体 301 收敛到裸 URL（保留其它查询参数）。
# 2026-09-11 SEO：此前 `/` 与 `/?lang=en` 都返回 200 且 HTML 逐字节相同（同
# title / 同 meta description / 同 canonical），Bing 因此报「重复 meta
# description」；收敛后同一页面只有一个可索引 URL。只作用于可索引 HTML 页面——
# `/api/*`（前端 JS 显式带 lang=en）、`/admin`、`/monitor` 不在白名单，行为不变。
_BARE_URL_ENDPOINTS = {"index", "hf_page", "search_page", "term_detail"}
# 单页内嵌双语的页面：没有语言变体，任何 ?lang= 都收敛到裸 URL（历史页脚
# 链接曾写 /terms?lang=en|zh、/privacy?lang=en|zh，会与裸 URL 重复 description）。
_MONOLINGUAL_ENDPOINTS = {"terms", "privacy"}


@app.before_request
def _canonical_lang_redirect():
    if request.method not in ("GET", "HEAD"):
        return None
    if request.endpoint in _MONOLINGUAL_ENDPOINTS:
        if "lang" not in request.args:
            return None
    elif request.endpoint in _BARE_URL_ENDPOINTS:
        # 中文是显式变体（?lang=zh），只有英文变体需要收敛到裸 URL
        if request.args.get("lang") != "en":
            return None
    else:
        return None
    rest = [(k, v) for k, v in request.args.items(multi=True) if k != "lang"]
    target = request.path + _qs([f"{k}={quote(v)}" for k, v in rest])
    return redirect(target, code=301)


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


def _rate_limit_deny(retry_after):
    """限流 429 响应（JSON + Retry-After）。调用方已按 (bucket, IP) 判定超限。"""
    resp = jsonify({"ok": False, "error": "请求过于频繁，请稍后重试"})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after)
    return resp


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
    # SSR 首屏词链接携带当前榜单状态（非默认项），返回恢复滚动位置需要原样状态。
    # 英文是裸 URL 主语言 → 只有中文变体标注 lang=zh，英文链接不带 lang（带
    # lang=en 的内链会命中 301，浪费一跳且让爬虫反复发现 ?lang=en 变体）。
    ssr_term_parts = []
    if lang == "zh":
        ssr_term_parts.append("lang=zh")
    if requested_view != "words":
        ssr_term_parts.append(f"view={requested_view}")
    if requested_sort != "rise":
        ssr_term_parts.append(f"sort={requested_sort}")
    if requested_cat and requested_cat != "all":
        ssr_term_parts.append(f"cat={quote(requested_cat)}")
    ssr_term_qs = _qs(ssr_term_parts)
    initial_terms = (
        _initial_terms_for_ssr(sort=requested_sort, lang=lang)
        if _seo_enabled() and requested_view != "news" else []
    )
    # 分类条首屏元数据：基于全量词卡计算（与 /api/stream 同口径），
    # 计数首屏即显示全量值，避免加载完成后 all 20→100 的数值跳变闪烁。
    if initial_terms:
        (initial_dimensions, initial_dimension_counts,
         initial_total) = _initial_dimension_meta_for_ssr(
             sort=requested_sort, lang=lang)
    else:
        initial_dimensions, initial_dimension_counts, initial_total = [], {}, 0
    # hreflang 互指：zh/en 两个语言变体互为 alternate（x-default → en 主语言，
    # 即裸 URL `/`）；2026-09-11 起英文是裸 URL 规范页、中文在 `/?lang=zh`，
    # `_abs` 在 BASE_URL 未设时返回 None，模板据此跳过输出。
    hreflang = {
        "zh": _abs(_lang_url("/", "zh")),
        "en": _abs(_lang_url("/", "en")),
    }
    return render_template("index.html", sources=SOURCE_META,
                           sponsors=sponsors, site_name=config.SITE_NAME,
                           site_desc=SITE_DESC_EN if lang == "en" else SITE_DESC,
                           base_url=_base_url(), canonical=_abs(_lang_url("/", lang)),
                           hreflang=hreflang,
                           seo_enabled=_seo_enabled(),
                           initial_terms=initial_terms,
                           initial_dimensions=initial_dimensions,
                           initial_dimension_counts=initial_dimension_counts,
                           initial_total=initial_total,
                           requested_cat=requested_cat,
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


def _term_meta_desc(data, lang):
    """词条页 meta description：含具体数据 + 最新报道标题，逐词唯一。

    2026-09-11 SEO：此前描述是「词 + 报道数 + 热度」的固定模板（约 85 字符），
    既偏短又与其它词条高度同构。改为「词 + 报道数 + 最新报道标题 + 追踪说明」，
    标题按剩余预算裁剪（`_clip_desc`），总长 ≤160 字符；标题缺失或无报道时
    退回 HF 数据/通用句式，恒非空。
    """
    t = data.get("term") or {}
    news = data.get("news") or []
    name = (t.get("term") or "").strip() or "AI"
    cnt = int(t.get("news_cnt") or 0)
    hf = data.get("hf_detail") or data.get("hf") or {}
    limit = 160
    if cnt > 0:
        title = ""
        for item in news:
            title = (item.get("title") or "").strip()
            if title:
                break
        if lang == "zh":
            body = f"{name} 最新动态聚合：{cnt} 篇相关报道与社区讨论。"
            tail = f"持续追踪 {name} 的模型、产品、融资与政策进展。"
            prefix, suffix = "最新报道：", "。"
        else:
            body = f"{name}: {cnt} related reports aggregated from AI news sources. "
            tail = f"Track {name} models, products, funding and policy moves."
            prefix, suffix = "Latest: ", " "
        # 预算分配：先保 body，再保 ≥36 字符的可读标题（不够时裁尾句让位），
        # 最后按剩余空间裁标题——长词名（如 60+ 字符的 HF 模型 id）也不会挤掉标题。
        min_head = len(prefix) + len(suffix) + 36
        room = limit - len(body) - len(tail) - min_head
        if title and room < 0:
            tail_budget = len(tail) + room
            tail = _clip_desc(tail, tail_budget) if tail_budget >= 20 else ""
        room = limit - len(body) - len(tail) - len(prefix) - len(suffix)
        head = prefix + _clip_desc(title, room) + suffix if title and room >= 24 else ""
        return body + head + tail
    # 无报道支撑（纯 HF 模型词 / 冷启动）：用 HF 数据兜底，仍保持句式唯一
    likes = hf.get("likes") or 0
    downloads = hf.get("downloads") or 0
    if lang == "zh":
        return (f"{name}（HuggingFace 开源模型）：{likes} 点赞、{downloads} 下载。"
                f"查看官方模型卡、社区讨论与相关 arXiv 论文聚合。")
    return (f"{name} on HuggingFace: {likes} likes and {downloads} downloads. "
            f"See the official model card, community discussion and related "
            f"arXiv papers aggregated on AITrendWatch.")


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

    # P1 索引质量门槛：词条是否允许被收录由词池行质量判定（薄词条 noindex，
    # 页面仍照常渲染）；与 sitemap（list_terms_for_sitemap → term_row_indexable）
    # 同一门槛单点收口。词池外的 HF 长尾回退页 row=None → 不可索引。
    row = terms_mod.get_term_row(term_name) if terms_mod else None
    indexable = bool(row) and terms_mod.term_row_indexable(row)

    t = data["term"]
    slug = t.get("term") or term_name
    canonical = _abs(_lang_url(f"/term/{quote(slug)}", lang))
    # hreflang 互指：zh/en 语言变体互为 alternate（x-default → en 主语言，即裸 URL）；
    # slug 与 canonical 同用 quote(slug)，_abs 在 BASE_URL 未设时返回 None，
    # 模板据此跳过输出。
    hreflang = {
        "zh": _abs(_lang_url(f"/term/{quote(slug)}", "zh")),
        "en": _abs(_lang_url(f"/term/{quote(slug)}", "en")),
    }
    desc = _term_meta_desc(data, lang)
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
    home_url = _with_query(_lang_url("/", lang), back_parts + ["scroll_back=1"])
    return render_template("term_detail.html", word=data, lang=lang,
                           site_name=config.SITE_NAME,
                           site_desc=desc, base_url=_base_url(),
                           canonical=canonical, hreflang=hreflang,
                           seo_enabled=_seo_enabled(),
                           indexable=indexable,
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
                           site_desc=SITE_TERMS_DESC,
                           base_url=_base_url(), canonical=_abs("/terms"),
                           seo_enabled=_seo_enabled(),
                           contact_email=config.CONTACT_EMAIL,
                           updated_at=SITE_TERMS_UPDATED)


@app.route("/privacy")
def privacy():
    """隐私政策页（中英双语，SEO 可索引；P1 2026-09-07 补齐运营/变现合规）。

    独立于 /terms（条款页内嵌的隐私节是摘要），覆盖自建埋点（IP/GeoIP/
    session_id/事件流/赞助位曝光点击）与 Google Analytics、第三方广告 Cookie、
    本地存储偏好等实际数据处理；AdSense 审核与 GDPR 义务要求的页面入口。
    单页内嵌双语、canonical 固定裸 URL /privacy（与 /terms 同构）。
    """
    return render_template("privacy.html", site_name=config.SITE_NAME,
                           site_desc=SITE_PRIVACY_DESC,
                           base_url=_base_url(), canonical=_abs("/privacy"),
                           seo_enabled=_seo_enabled(),
                           contact_email=config.CONTACT_EMAIL,
                           updated_at=SITE_PRIVACY_UPDATED)


@app.route("/privacy-policy")
def privacy_policy_redirect():
    """常见拼写别名 → /privacy（301）。AdSense/审核方可能先试 /privacy-policy。"""
    return redirect("/privacy", code=301)


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


@app.errorhandler(500)
def internal_error(e):
    """500 → 记录完整堆栈 + 简洁降级页/JSON（P2：此前只有 404 handler，
    未捕获异常会裸露 Flask/nginx 默认错误页）。

    API 请求（/api/* 或 Accept: application/json）返 JSON {ok:false}，
    页面请求返 noindex HTML，避免爬虫索引错误页。
    """
    app.logger.exception("Internal Server Error: %s", e)
    wants_json = request.path.startswith("/api/") or \
        "application/json" in (request.headers.get("Accept") or "")
    if wants_json:
        resp = jsonify({"ok": False, "error": "服务器内部错误，请稍后重试"})
        resp.status_code = 500
        return resp
    html = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"robots\" content=\"noindex,nofollow\">"
        "<title>500 · 服务器内部错误</title></head>"
        "<body style=\"font-family:sans-serif;text-align:center;padding:60px\">"
        "<h1>500</h1><p>服务器开小差了，请稍后刷新重试。</p>"
        "<p><a href=\"/\">← 返回首页</a></p></body></html>"
    )
    return Response(html, status=500, mimetype="text/html; charset=utf-8")


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
    # community 按页面语言分流（get_terms 兜底路径未过 get_model_cards，
    # 统一在此投影；重复投影幂等，不依赖缓存内容）
    cards = tracker.localize_model_cards(cards, lang)
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
    # hreflang 互指：/hf（英文裸 URL）与 /hf?lang=zh 互为 alternate
    # （x-default → en 主语言）；_abs 在 BASE_URL 未设时返回 None，模板据此跳过。
    hreflang = {
        "zh": _abs(_lang_url("/hf", "zh")),
        "en": _abs(_lang_url("/hf", "en")),
    }
    # 2026-09-11 SEO：英文 150-158 字符 / 中文 ~100 字，与首页、词条页描述互不相同
    if lang == "zh":
        desc = ("HuggingFace 开源模型榜：按趋势分、点赞数与下载量排序查看开源 AI 模型，"
                "附 pipeline 标签、arXiv 论文与官方/社区讨论入口，"
                "数据取自 HuggingFace 官方榜，每数小时更新。")
    else:
        desc = ("HuggingFace open-source model leaderboard: browse trending AI models "
                "by trend score, likes or downloads, with pipeline tags, arXiv papers "
                "and community links.")
    toggle = _lang_url(f"/hf?sort={sort}", "en" if lang == "zh" else "zh")
    return render_template(
        "hf.html", models=models, sort=sort, fetched_at=fetched_at,
        lang=lang, site_name=config.SITE_NAME, site_desc=desc,
        base_url=_base_url(), canonical=canonical, hreflang=hreflang,
        seo_enabled=_seo_enabled(),
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
    # 需求 1：搜索结果不双显同一报道——历史库与当轮池可能各带同一篇文章的
    # 不同 url 形态（&amp;/&、utm 变体等镜像/孪生行），news 卡按归一化标题
    # 去重（与词条关联列表同口径的标题键；评分已降序，保留最高分那份）。
    # model/word 卡不参与：HF 模型名与新闻报道同名时两者都是有效命中。
    deduped = []
    seen_titles = set()
    for s in scored:
        if s.get("kind") == "news":
            tkey = None
            for field in ("title_zh", "title_en", "title"):
                k = text_utils.normalized_title_key(s.get(field))
                if k:
                    tkey = k
                    break
            if tkey is not None:
                if tkey in seen_titles:
                    continue
                seen_titles.add(tkey)
        deduped.append(s)
    return deduped[:limit], word_hits[:3], history_hits


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

    search_canonical = _abs(_lang_url(
        "/search" + ("?q=" + quote(q) if q else ""), lang)) if q else None
    return render_template(
        "search.html",
        q=q, lang=lang, terms=results, word_hits=word_hits,
        count=len(results), history_hits=history_hits,
        suggest=suggest, site_name=config.SITE_NAME,
        # 空查询（/search 无 q）此前也会渲染出本地兜底描述；改由 app.py 的
        # SEARCH_DESC / SEARCH_DESC_EN 统一提供（该页 noindex，描述只用于
        # 分享卡片与爬虫兜底，长度与其它页面口径一致）。
        search_desc=SEARCH_DESC if lang == "zh" else SEARCH_DESC_EN,
        base_url=_base_url(), canonical=search_canonical,
        seo_enabled=_seo_enabled(),
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
    """站点地图（主语言 = 英文，裸 URL 即英文规范页）。

    2026-09-11 SEO：英文变体从 `?lang=en` 改为裸 URL（`/`、`/hf`、
    `/term/<slug>`），与 canonical 一致——此前提交的 `?lang=en` 已 301 到
    裸 URL，继续提交会浪费抓取配额并让 Bing 保留旧变体。中文变体不重复
    提交，由页面 head 的 hreflang zh↔en 互指关联；/terms 与 /privacy 是
    单页内嵌双语（canonical 固定裸 URL），本就直接提交裸 URL。
    BASE_URL 未设 → 无法生成绝对 URL，返回空 urlset。
    """
    base = _base_url()
    urls = []
    if base:
        urls.append(f"{base}/")
        if _seo_enabled():
            # 服务条款/隐私政策页（均单页双语，裸 URL）+ HF 模型榜（常驻索引，英文裸 URL）
            urls.append(f"{base}/terms")
            urls.append(f"{base}/privacy")
            urls.append(f"{base}/hf")
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


# 站点 logo 图标（源文件 assets/logo-icon-512.jpg），离线缩放到三尺寸 PNG
# 内联为 base64 以避免引入静态目录/compose 挂载变更（生成脚本 make_logo_icons.py）：
#   32×32  → /favicon.ico（浏览器标签页，PNG 兼容性最高）
#   180×180 → /apple-touch-icon.png（iOS 添加到主屏幕）
#   192×192 → /favicon.png（模板高优先级 icon + 首页 header logo）
# 32x32 PNG, 1855 bytes
_FAVICON_PNG32_B64 = (
    "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"
    "RU5ErkJggg=="
)

# 180x180 PNG, 21411 bytes
_APPLE_TOUCH_PNG180_B64 = (
    "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"
)

# 192x192 PNG, 20962 bytes
_LOGO_PNG192_B64 = (
    "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"
    "jWqtnDI1WSEkaaTHDrsDeyumFI7epjyQdxW5YHBtbZ98OHCCgWgzUzOB"
    "UOLwqw8frr708vLje49+8o8fiYLA6LDCeJ7PADhxvBhF2RpFjtCXGzED"
    "PsbIrUV/4jS2PHGQWOC0MFfrdzrT8KgDW4V/pE6i5eXl7/+O+C//twer"
    "RUlVs3PCNqkFMPUapl3OnKj+zf/p6C/9f9c/9dnVQqXmsRqjqMni8ycr"
    "sRPvzYXAMeEtaikhUAAoVNryTuzuR/VakhpCw4RkmJu+Pb/69dcWv3q+"
    "funa4v2H8Uys3/nhdN/Ogqlx1FZMoyjgDh+e2brl4cKiRU4wjsa/vkxl"
    "6nit5zWnhbla56TnBSMX+L0qRq/UAlFfXP7JHyr97E8fMpj3qdCJc+Hx"
    "6GKTq00rFVgpiMsirk+0VIz/5z9/Yq761m/85yflWgWexUJ67tlKrrPp"
    "8kF/Wav6f6jisI0FtW1iQNjRpEYzi5xIlsvZzdurL7/65Gvn61eu6ePF"
    "BBIV4q1zNV1caVy/Vt+3szCMIZ0LVVIoZjZbi44csC99LY2jErSpYuyc"
    "qMoFjibCXAPU38Jgoad1aqOt82GHhS9Zx4XTx1wYsrwgyksYqdGLRMv1"
    "9IPvjT/yJw+pKlREHEXS+0vJp19uXngL9xcKq+lyJXLbZgqnDhY/9Lzb"
    "OWOmXs2Z/YWf2v/g3rVPfbmZmu7fwWNHtgAqee4wcI3BWOtnXhtiNoMn"
    "LcqZh8EQCRyz5seNt+rnX1n66ssLV6/7+hLFRVGhWK6VxWCe5gWpXLy4"
    "8B0f2DqB6witABw/XvqDryyTUTYVY2z3c9aLuWgdioi2iS2PqWOuQQaU"
    "p6hCTZrYtb3xV37mWARTkJJSouXPX0j+xcdLd57MSqSxECw/NHvjkX7p"
    "6vLHvxr96Q+Xv/O0U/VEBPvZP3fg3vzF2pz94Q9vr5XDM3CYUEVhVJ1D"
    "80+eKf2JMIKAUMX1G4tffbl+/pXFa2+ki6sukqhYKJdnaEjN1LwzA5AY"
    "XOzk0rXUe3OOmIy/fuLElmK8bObzyo1tcLMku7RvNlHXbjMw18DRZuZr"
    "Bgq+0fjRH9y6fSbW1CCgREsf/4r9v787ZxGqlUyDIROWFakUZh6u1H/x"
    "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"
    "g1blrQtzDQj2KoEwRPNRrPMP0rdu1jvEWYaP3JAA9u0p7t5lPhkqMDIa"
    "c7W5/uatd1XDlPrx3XhqMzAX+9bNREG4QWAevlBwr1/2zcTiIIwgapqW"
    "f/g9yfOHkt/9ypNXrkeLK1QVsjE7Ky8crHzfe93B7aq6kcGmwEe6caP+"
    "4IlUCy6n6E9tJzI6+TsCMSeWisNiHZcuL584OmOWr6wf3F0lAPNWiOXw"
    "kfL1a81iMR5QGBzc52IH07QtAD7h2VjvdDN7xvXW3efqx1yDWhlU0Yjw"
    "Kkk5ctfeavzO5+7/2Id3pql3RoJeLdq/Lf7I9+tCE0+W1HsnrrilxtlC"
    "kA6EZVVn79U5GShiP+rzmgNw8dJj750YfYe0wYZ3IvdO1jIrSzhTUnDx"
    "0uqP/FBHGjRKxhsEjp+Y+dQn7wHx2jFX6/BbK+Kyc2R9wxbETehzTWLi"
    "YgqjKiEqpiwUyh/7jQeffulhHDkRoTjANFXzytmCHNgWHd7Jg9swWzDv"
    "U+/VVJykiv/0qWurjTR3oxOXVs2c0Mxev+pjgYYtSVPZidyjOmaBSy9G"
    "bzSDFmJef7O+tJTQqRk5ZDdNmBsMF3HiSKFS9b61KMkkE3gej7nMzLfz"
    "Hmt167IMKay13iAAYxjMIDYPc/W32CTfp5q9nYitavH//ofzf/eX33jp"
    "lSePlxpOxEVCJ1Qz771P1aemSofIuchFN24v/x9/58KVi6hWCt5be3hv"
    "QrYY+fBxevNmWoiijsLpRnciD/RPYu0leFHkHj7AW281gNZSPhsx7GiG"
    "3bvLe3aVkwT5Yi+FjcJTwzAXBx5w+wbAXKP2hbV00CKhxJWPf84+9cVb"
    "u+bS97+45ds/UDu0f3am5AjXmsiqr6Q3bj7+7EsLn/mv9fsP9H/7X7Z0"
    "xByd0C+HeavL1xeePEGhElnHA1lTn2sS6+lLmJCsli5fWT59ujZCbaU1"
    "w21qUSRHjxSvXmmwVDBLmbG8pQNSsSNytXhnXc5J+veqZ8tHp6/2ssE+"
    "1yTRNRrQo1ExppUavc3MLyX/7uON//yZ1Z07Hu3ZGdXKabFoqw0urbj5"
    "u80H99OVNIrimQN7V48fnQG0BQonZEAbFHCvXWymZiXQm2+R6je8E7m9"
    "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"
    "WUh9Orut8dN/8ZDEIEXoKC3X0xc6KPDe9u0sfeTP7rNkMc9BfMZ2gxKU"
    "IUIW7zbMRftmwlyWE7sVTMEU8KASkqT1n/qZPdt3F723QUyAvi8nSH36"
    "7e/b9pM/MrNYr4tEGfkcJlQBRN27GHN1+Az7psJcoRPKbE5SADqJF+r1"
    "7/+jtTMf3OY1Hch1iwZ+Qjqnan/yjx289+j6pz9br9Vq3mfr5fuHeL4B"
    "+1x+4gnIb54+F8QCRRNmkUAkwsLS8gc/XPrRnz6oakI38GJl0PZgOtAE"
    "AvtLf/7Ae19Evb4YuYC52IOq332Yi/im6XP1aPpkzpaEuAj1+uIL78Of"
    "+u/3gybMFxBO4IkN8DA40MyXYv7Vnz126gTq9SUXZdxEG8B25jc65urw"
    "6kMTkndbn2uINboIi0uLR0/hZ/7y0ahoaq3cZ8D0HwcBVGvfbAPJpaX0"
    "Fz965aXzrMxUVRNRycS3oWYqFmVdtbGRq9XPX0vkmuzGaf/40ujIxb4e"
    "e04aH4B6xnAL17ITOcuaJ9uJPGHkWgfmysYmkGazDhb0ME1YWKgvP/9e"
    "+5m/crQyE1mm3chhtGyOFchSM6E2E370l9/4xOeatZmqwWhBUwxmqYnC"
    "Yg53JGOVZsdKGE1kQIOyZma843Bw/KjXZKaYySE7v9xghq6N3onc9bk6"
    "dyKv8T7IJBNuExhQx/WaMIVF2SgpPekIt1hf/sD3FP/Uzz4TF2k2fnSf"
    "EyismXqlODN87Ndv/OZ/XoriWiGO1IfxhBSiZoWw+ZwjWfGc7HOu1YBo"
    "6Kj3SE+rLhMkNBuBucYUaYY3jDvrPYNWtHbMVGxMK57jloZOqOffMiAx"
    "AZuAg0WgijBNfZLUP/xj1R//M4cMMPMiMn5WeiKJvqyLYSQ/98UH//zX"
    "7t57WKhUi6YkkkzmzAY0g8av25k+5pKxmEuGFxTWFLk4PLcbFrlGZ81v"
    "A+ZqSTFJPvlBxCJWX25u3dH8iT+7+8Vv32Z5sJoQtdike6QJ9d45d+f+"
    "6j/7F2985Q9UiuWoFFlqHLQzbcyZszHXN9GKp17iaRtz5Stfct+TTyv0"
    "Yq4MrA9xPDZqVw2HYa41RvD1ha3J57m65tuso9NOE3FJ0zeTlRe+Tf74"
    "nzu0bVfRa+rETQ5pJgphgMJcSDS8WuwEwKc/f//f/vv5t27HpUrBOcJ3"
    "M9ANBh9EymXQW2xgnmtg5OrOmtta8b5zP1evzwi1V2q/AXFICXisAfV8"
    "rkn2cw2cyZzSPJd0Tmh1yqw5oXqsrDR27U9/6Cd3vP9DO0F4r9LeGOVH"
    "tvnX5oGsY2sEoCAMwicL6X/4jzc//qmFheVKqRQ7R/OWeWKQ9B2dq1z9"
    "I9MpzyY5ByhQfwtzZTt81mQ9eaQz5v6GufUoSNPA6BIhROBTv7rqS7WV"
    "7/iBme//I89UZyM14wDFIk4vhPUaVKqezjkA124s/95/mf/CS8uLS1Fc"
    "KjsHQQIzRdRqw+YTk+xQ5A/KGPotzLVhzOV6o3n4XJl2QzApDSuTfYrG"
    "6kp1Vs++v/zdP7zzmSNVAOo9RdbXfFyvAQVdRXg171wEyM23Vn/vE7e/"
    "+OXG/CNHceXYFUQywQnAMuKRZsViZohM+1mjE9WabZMxFzlCR/cdh7ms"
    "4+etim7wr0YKzDSNGs3E0Jzbnrz4gfJ3ff++PQdKgHmfCiNw/a3rdRoQ"
    "LO+IUVXD6m4CePy4+crXF7/w0vLrl+tLS5H35uIoci6kTaSaSffOjelh"
    "Lg7ezyW9ipbrxVwdi083hLnaWxPXFrnCZxyiNM2ckNO6FHq1NE2TJBFB"
    "bS45enL2xfdXT52tzW4Ny24NNKEHHGz92+7Wa0DZ0L+3fJjV1GAQlykD"
    "3JtfuXS9ce3a8uVLy3fv2fKyqgJKmlBEKBCjtFVINoK5Wtlue2VTH+Yi"
    "acqM5TvQ8WQTKkNTZkHXHPJYzMW1qF5uBHMZ1MxMaWYa7gBVhJWq27Eb"
    "h0+UDh2tHjpe2LarEh6NejBbtpB5qfzwyttrQEFpyNrzQyG0hGUALkc8"
    "aervzvtHD1fm59Nb9+r351cWlwory/HKsiyvJEmSHaE1Rq4BmKvtfoZj"
    "LjNgkIYGR3ZJuzrtQ3zP08NcFsUoV+JyxUqVZm022bajvGNvdfvOeOuO"
    "0o5dkUTSWtIAy3hilvkq5ioyOgngGvj1/wMiBPXFb4+Z2wAAAABJRU5E"
    "rkJggg=="
)

_FAVICON_PNG32 = base64.b64decode(_FAVICON_PNG32_B64)
_APPLE_TOUCH_PNG180 = base64.b64decode(_APPLE_TOUCH_PNG180_B64)
_LOGO_PNG192 = base64.b64decode(_LOGO_PNG192_B64)


@app.route("/favicon.ico")
def favicon():
    # 返回真实 PNG（而非 SVG），最大化浏览器标签页兼容性；附带一年强缓存
    # 头，避免每次页面加载都重新请求 favicon。
    resp = Response(_FAVICON_PNG32, mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/favicon.png")
def favicon_png():
    """站点 logo 192×192 PNG——模板里作为高优先级 icon 引用，首页 header logo 也用它。"""
    resp = Response(_LOGO_PNG192, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    """iOS 添加到主屏幕用的 180×180 PNG 图标。"""
    resp = Response(_APPLE_TOUCH_PNG180, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# OG 社交分享图（1200×630，Open Graph / Twitter Card 推荐尺寸）。
# 纯 PIL 生成，无外部依赖；首次请求后进程内缓存。
_OG_IMAGE_CACHE = {}

@app.route("/og-image.png")
@app.route("/og-image-v2.png")
def og_image():
    """生成站点级 OG 社交分享图（1200×630）。

    蓝色渐变底 + 白色站名 + 副标题，用于 Open Graph / Twitter Card。
    进程内缓存避免重复渲染。
    """
    if "img" in _OG_IMAGE_CACHE:
        png = _OG_IMAGE_CACHE["img"]
    else:
        try:
            from PIL import Image, ImageDraw, ImageFont
            W, H = 1200, 630
            img = Image.new("RGB", (W, H), "#0f1117")
            draw = ImageDraw.Draw(img)
            # 渐变背景（左上蓝 → 右下紫）
            for y in range(H):
                r = int(15 + (79 - 15) * y / H)
                g = int(17 + (60 - 17) * y / H)
                b = int(23 + (180 - 23) * y / H)
                draw.line([(0, y), (W, y)], fill=(r, g, b))
            # 尝试加载系统字体，降级到默认
            font_large = None
            font_small = None
            font_paths = [
                # Linux
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                # macOS
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/HelveticaNeue.ttc",
                # Windows
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/calibrib.ttf",
                "C:/Windows/Fonts/msyhbd.ttc",  # 微软雅黑粗体
            ]
            for fp in font_paths:
                try:
                    font_large = ImageFont.truetype(fp, 72)
                    font_small = ImageFont.truetype(fp, 36)
                    break
                except (OSError, IOError):
                    continue
            if font_large is None:
                font_large = ImageFont.load_default()
                font_small = font_large
            # 站名
            site = config.SITE_NAME or "AITrendWatch"
            bbox = draw.textbbox((0, 0), site, font=font_large)
            tw = bbox[2] - bbox[0]
            draw.text(((W - tw) // 2, 200), site, fill="#ffffff", font=font_large)
            # 副标题
            sub = "AI Trend Aggregator"
            bbox2 = draw.textbbox((0, 0), sub, font=font_small)
            sw = bbox2[2] - bbox2[0]
            draw.text(((W - sw) // 2, 320), sub, fill="#8b91a3", font=font_small)
            # 底部标语
            tagline = "HuggingFace · arXiv · AI News"
            bbox3 = draw.textbbox((0, 0), tagline, font=font_small)
            tlw = bbox3[2] - bbox3[0]
            draw.text(((W - tlw) // 2, 420), tagline, fill="#4f8cff", font=font_small)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            png = buf.getvalue()
            _OG_IMAGE_CACHE["img"] = png
        except ImportError:
            # PIL 不可用 → 返回最小 1x1 PNG 占位
            png = _FAVICON_PNG32
    resp = Response(png, mimetype="image/png")
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
        # 限流：每 IP 每 LOGIN_RATE_WINDOW 最多 LOGIN_RATE_LIMIT 次尝试
        # （防暴力猜 ADMIN_TOKEN；含成功尝试，量级远低于阈值）。
        allowed, retry_after = ratelimit.allow(
            "admin_login", _client_ip(),
            config.LOGIN_RATE_LIMIT, config.LOGIN_RATE_WINDOW)
        if not allowed:
            return _rate_limit_deny(retry_after)
        token = (request.form.get("token") or "").strip()
        if token and hmac.compare_digest(token, config.ADMIN_TOKEN):
            session["admin_token"] = token
            nxt = request.args.get("next") or "/monitor"
            # 只允许站内相对路径回跳，防开放重定向
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/monitor"
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
    """旧管理后台入口 → 重定向到合并后的 /monitor#sponsors。"""
    return redirect("/monitor#sponsors", code=302)


@app.route("/admin/sponsors/list")
@admin_required
def admin_sponsors_list():
    """赞助位列表 JSON API（供合并后的 monitor 页面 AJAX 加载）。"""
    slots = store.list_slots(active_only=False)
    return jsonify({"ok": True, "slots": slots})


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


# ---------- 统一管理后台（流量监控 + 赞助位管理，Tab 切换）----------
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


# ---------- 用户行为事件上报（埋点系统 v3）----------
@app.route("/api/event", methods=["POST"])
def api_event():
    """接收前端批量事件上报。

    Body JSON: {events: [{event_type, event_data?}, ...], session_id?, path?}
    或单条: {event_type, event_data?, session_id?, path?}
    返回 {ok: true, received: N}。best-effort，不阻塞前端。
    """
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    cip = _client_ip()
    # 限流：每 IP 每 EVENT_RATE_WINDOW 最多 EVENT_RATE_LIMIT 次上报
    # （防刷量把 SQLite/事件表撑爆；浏览器会话远低于阈值）。
    allowed, retry_after = ratelimit.allow(
        "api_event", cip, config.EVENT_RATE_LIMIT, config.EVENT_RATE_WINDOW)
    if not allowed:
        return _rate_limit_deny(retry_after)
    cc = _client_country(cip)
    sid = (body.get("session_id") or "").strip()[:64]
    path = (body.get("path") or request.path).strip()[:200]
    events = body.get("events")
    if isinstance(events, list):
        # 批量模式
        cleaned = []
        for ev in events[:50]:  # 单次最多 50 条，防滥用
            et = (ev.get("event_type") or "").strip()
            ed = ev.get("event_data")
            cleaned.append({"event_type": et, "event_data": ed})
        store.record_events_batch(cleaned, ip=cip, country=cc,
                                  session_id=sid, path=path)
        return jsonify({"ok": True, "received": len(cleaned)})
    else:
        # 单条模式
        et = (body.get("event_type") or "").strip()
        ed = body.get("event_data")
        store.record_event(et, event_data=ed, ip=cip, country=cc,
                           session_id=sid, path=path)
        return jsonify({"ok": True, "received": 1})


@app.route("/monitor/api/events")
@admin_required
def monitor_events_api():
    """监控页用户事件统计：按类型计数 + 每日趋势 + 近期明细。"""
    days = request.args.get("days", "30")
    try:
        days = max(1, min(int(days), 90))
    except ValueError:
        days = 30
    return jsonify(store.event_stats(days))


if __name__ == "__main__":
    print("=" * 50)
    print(" 热点聚合服务启动中...")
    print(" 打开 http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
