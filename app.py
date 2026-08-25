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
import hmac
import threading
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from flask import (Flask, jsonify, render_template, request, Response,
                   redirect, session, abort)

import tracker
import config
import store

# 启动后台预热线程：定时抓取 HF + arXiv 写文件缓存，请求路径只读缓存秒回。
# 每个 gunicorn worker 各起一个 daemon 线程，但通过 tracker._refresh_lock 串行化，
# 实际只有一个 worker 在打 arXiv，其余跳过（省配额）。
tracker.start_background_refresher()

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
    return render_template("index.html", sources=SOURCE_META,
                           sponsors=sponsors, site_name=config.SITE_NAME,
                           adsense_enabled=config.ADSENSE_ENABLED,
                           adsense_client=config.ADSENSE_CLIENT,
                           baidu_ads_enabled=config.BAIDU_ADS_ENABLED,
                           baidu_cpro_id=config.BAIDU_ADS_CPRO_ID)


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


@app.route("/health")
def health():
    return jsonify({"ok": True})


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
