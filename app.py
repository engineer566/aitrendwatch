"""
热点聚合服务 —— 通过多个免费 API / RSS / 网页抓取聚合当前最新热点。

运行：
    pip install flask requests
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

import re
import json
import time
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

# 首页 SSR 渲染的热词条数（Top-N）。读文件缓存，秒回。
SSR_INITIAL_LIMIT = 20


def _initial_terms_for_ssr():
    """首页 SSR 用的首屏热词：合并 model 卡 + news 卡，按热度取 Top-N。

    读 tracker / dims 文件缓存（不触发 arXiv），任何失败返回 []，模板兜底骨架屏。

    关键：必须同时取 model 卡（模型发布）+ news 卡（产品发布/研究论文/投融资/
    行业动态/其他），否则首屏 20 条全是模型发布，分类条只剩「全部」+「模型发布」
    两个标签。前端的 fetchAll() 首屏短路逻辑（allData 非空就 return，不再请求
    /api/stream）会固化这个首屏状态，用户看到的永远只有两类。

    进一步：单纯按 score 取 Top-N 仍会被高分 model 卡垄断（model 卡 score 普遍
    高于 news 卡），导致首屏仍只有「模型发布/行业动态/产品发布」三类可见，研究
    论文、投融资等维度被挤出。这里改为「每维度配额」——各维度先各取 Top-Quota，
    合并后再按 score 降序截断 SSR_INITIAL_LIMIT，保证 6 个维度都在首屏露出，
    分类条即可渲染全部标签，爬虫也能索引各类内容。
    """
    try:
        region = "zh"  # SSR 无 request 上下文，默认中文；JS 接管后按用户语言重取
        model_cards, _ = tracker.get_model_cards(region)
        news_cards, _ = dims.get_news_cards(region)
        cards = model_cards + news_cards
        if not cards:
            return []

        # 按维度分桶，每桶按 score 降序
        by_dim = {}
        for c in cards:
            by_dim.setdefault(c.get("dimension") or "其他", []).append(c)
        for d in by_dim:
            by_dim[d].sort(key=lambda x: x.get("score", 0), reverse=True)

        # 每维度至少取 PER_DIM_QUOTA 条，保证小维度也在首屏可见；
        # 配额取完后合并、整体按 score 降序截断 SSR_INITIAL_LIMIT。
        quota = max(2, SSR_INITIAL_LIMIT // max(1, len(by_dim)))
        pooled = []
        for d, lst in by_dim.items():
            pooled.extend(lst[:quota])
        pooled.sort(key=lambda x: x.get("score", 0), reverse=True)
        return pooled[:SSR_INITIAL_LIMIT]
    except Exception:
        return []


def _sitemap_terms():
    """sitemap.xml 用热词列表：双榜合并去重，受 SITEMAP_MAX_URLS 限制。"""
    try:
        terms = []
        seen = set()
        for sort in ("trending", "top"):
            d = tracker.get_terms(sort=sort)
            for t in (d.get("terms") or []):
                tid = t.get("full_id") or t.get("term")
                if tid and tid not in seen:
                    seen.add(tid)
                    terms.append(t)
        return terms[:max(0, config.SITEMAP_MAX_URLS - 1)]
    except Exception:
        return []


# 站点级元信息（描述等），集中维护。
SITE_DESC = "AI 热点聚合 · 实时追踪 HuggingFace 模型趋势、arXiv 相关论文与社区讨论。上升最快、最热、最新 AI 模型一页尽览。"

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
    sponsors = store.list_slots(region=region, active_only=True)
    # 服务端记曝光 + PV（best-effort，失败静默）
    store.record_pageview()
    # 记录访问明细（IP + 地域），供监控页统计 PV / 独立 IP / 地域分布
    cip = _client_ip()
    store.record_visit(cip, _client_country(cip))
    for s in sponsors:
        store.record_impression(s.get("slot_id"))
    initial_terms = _initial_terms_for_ssr() if _seo_enabled() else []
    return render_template("index.html", sources=SOURCE_META,
                           sponsors=sponsors, site_name=config.SITE_NAME,
                           site_desc=SITE_DESC,
                           base_url=_base_url(), canonical=_abs("/"),
                           seo_enabled=_seo_enabled(),
                           initial_terms=initial_terms,
                           adsense_enabled=config.ADSENSE_ENABLED,
                           adsense_client=config.ADSENSE_CLIENT,
                           baidu_ads_enabled=config.BAIDU_ADS_ENABLED,
                           baidu_cpro_id=config.BAIDU_ADS_CPRO_ID,
                           default_lang="zh" if region == "zh" else "en")


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

@app.route("/api/trending")
def api_trending():
    """7 日上升最快热词（HF trendingScore 降序）。"""
    return jsonify(tracker.get_terms(sort="trending"))


@app.route("/api/top")
def api_top():
    """热度最高热词（HF likes 降序）。"""
    return jsonify(tracker.get_terms(sort="top"))


@app.route("/api/term/<path:term_name>")
def api_term(term_name):
    """单个热词详情：官方链接 + 社区讨论 + 相关论文。"""
    return jsonify(tracker.get_term_detail(term_name))


@app.route("/term/<path:term_name>")
def term_detail(term_name):
    """单个热词 HTML 详情页（SEO 可索引长尾页）。

    走进程内 TTL 缓存（get_term_detail 是 live HF + 同步 arXiv，~1-4s）。
    未找到 → 404 HTML + noindex。
    """
    key = term_name.lower()
    data = _detail_cached(key)
    if data is None:
        data = tracker.get_term_detail(term_name)
        # 无论成败都缓存，避免未命中的 term 被反复打上游
        _detail_set_cache(key, data)

    if not data.get("ok"):
        abort(404)

    term = data.get("term") or {}
    # 详情页 canonical：用短名（term），URL 更友好
    slug = term.get("term") or term_name
    canonical = _abs(f"/term/{quote(slug)}")
    desc = (f"{term.get('term','')} — {term.get('author','')} · "
            f"{term.get('official_label','HuggingFace')} · "
            f"趋势 {term.get('trending_score','-')} · ❤ {term.get('likes','-')}")
    return render_template("term_detail.html", term=term, site_name=config.SITE_NAME,
                           site_desc=desc[:160], base_url=_base_url(),
                           canonical=canonical, seo_enabled=_seo_enabled())


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
    """维度热词：按 AI 维度（模型发布/产品发布/投融资/...）分组的热点卡。
    可选 ?dimension=模型发布 只返回该维度；?lang=zh/en 投影对应语言（默认 zh）。
    每张卡含 official_url 直链官方原文。"""
    lang = request.args.get("lang", "zh")
    return jsonify(dims.get_dims(dimension=request.args.get("dimension"), lang=lang))


@app.route("/api/stream")
def api_stream():
    """统一卡片流：合并 model 卡（tracker）+ news 卡（dims）为一个扁平列表。

    参数：
      lang：默认按 Accept-Language（detect_region → zh/global → zh/en）。
      sort：rise（上升最快，按 trend 降序）/ hot（最热，按 score 降序）/
            new（最新，按 published 降序），默认 rise。
    返回 {ok, fetched_at, count, dimension_list, terms}。
    两类卡只读各自文件缓存，秒回，无需并发。
    """
    region = detect_region()
    lang = request.args.get("lang", "zh" if region == "zh" else "en")
    if lang not in ("zh", "en"):
        lang = "zh" if region == "zh" else "en"
    sort = request.args.get("sort", "rise")
    if sort not in ("rise", "hot", "new"):
        sort = "rise"

    model_cards, m_at = tracker.get_model_cards(lang)
    news_cards, n_at = dims.get_news_cards(lang)
    cards = model_cards + news_cards

    # 排序键：rise→trend, hot→score, new→published（统一字段）
    sort_key = {"rise": lambda x: x.get("trend", 0),
                "hot":  lambda x: x.get("score", 0),
                "new":  lambda x: x.get("published", "") or ""}[sort]
    cards.sort(key=sort_key, reverse=True)

    fetched_at = max(m_at, n_at)
    return jsonify({
        "ok": True,
        "fetched_at": fetched_at,
        "count": len(cards),
        "dimension_list": dims.DIMENSIONS,
        "terms": cards,
    })


@app.route("/health")
def health():
    return jsonify({"ok": True})


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
            for t in _sitemap_terms():
                slug = t.get("term")
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


if __name__ == "__main__":
    print("=" * 50)
    print(" 热点聚合服务启动中...")
    print(" 打开 http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
