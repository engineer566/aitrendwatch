"""
热词追踪层 —— 聚合 HF / arXiv，产出「热词卡」。

设计原则：
- 纯程序，零 LLM。热词 = HF 模型名（自带官方链接 + 趋势分）。
- 社区讨论链接靠 URL 拼接（知乎/B站/GitHub 搜索），不调需鉴权的 API。
- 7 日上升最快 = HF trendingScore 降序；热度最高 = likes 降序。
- 相关论文：按模型名逐词用 arXiv 全文检索（all:模型名, sortBy=relevance），
  天然相关，避免族名子串匹配的假阳性。串行 + 3s 间隔避开 arXiv 限速。
"""

import os
import json
import re
import time
import threading
import fcntl
from urllib.parse import quote

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
TIMEOUT = 8

HF_BASE = "https://hf-mirror.com"   # 官方 huggingface.co 在本网络不可达，走镜像
# arXiv 必须 HTTPS（HTTP 返回 301）；且要求请求间隔 ≥3s，否则触发 429 限速。
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_GAP = 3.0   # arXiv 官方要求两次请求间隔至少 3 秒
ARXIV_ENRICH_LIMIT = 8   # 只对榜单前 N 个热词做 arXiv 检索，避免触发限速（8 词 × 3s ≈ 24s）

# ---------- 文件缓存（跨 worker 共享 + 持久化）----------
# 关键：arXiv 串行检索慢（8 词 × 3s ≈ 24s），不能在请求路径做。
# 改为后台线程定时预热 → 写文件；4 个 gunicorn worker 都读同一份文件，请求秒回。
CACHE_DIR = os.environ.get("CACHE_DIR", "/app/cache")
CACHE_FILE = os.path.join(CACHE_DIR, "terms.json")
REFRESH_INTERVAL = 21600   # 后台预热周期：6 小时（榜单更新没那么快，省资源）
RETRY_INTERVAL = 300       # 预热失败后快速重试间隔：5 分钟
CACHE_TTL = 86400          # 文件缓存兜底有效期：24 小时（即便预热连续失败，旧缓存最多服务 24 小时）

# ---------- 内存缓存（进程内，兜底）----------
_cache = {}
_cache_lock = threading.Lock()

def _cached(key):
    with _cache_lock:
        ent = _cache.get(key)
        if ent and time.time() - ent[0] < CACHE_TTL:
            return ent[1]
    return None

def _set_cache(key, data):
    with _cache_lock:
        _cache[key] = (time.time(), data)


# ---------- 文件缓存（跨 worker 共享真源）----------
_file_cache = {}            # 进程内镜像，避免每次请求都读盘
_file_cache_lock = threading.Lock()
_file_cache_loaded = False


def _load_file_cache():
    """读磁盘缓存到进程内镜像（只加载一次；更新由 _save_file_cache 同步）。"""
    global _file_cache_loaded
    with _file_cache_lock:
        if _file_cache_loaded:
            return
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _file_cache.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        _file_cache_loaded = True


def _save_file_cache():
    """原子写磁盘（先写 .tmp 再 os.replace，避免其他 worker 读到半截）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_file_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _file_cache_get(sort):
    """返回 (data, fetched_at) 或 None。"""
    _load_file_cache()
    with _file_cache_lock:
        ent = _file_cache.get(sort)
        if ent:
            return ent.get("data"), ent.get("fetched_at", 0)
    return None, 0


def _file_cache_set(sort, data, fetched_at):
    with _file_cache_lock:
        _file_cache[sort] = {"data": data, "fetched_at": fetched_at}
    _save_file_cache()


# ---------- HF 模型热词 ----------
def fetch_hf_models(sort="trendingScore", direction="-1", limit=30):
    """拉 HF 模型列表。sort 可为 trendingScore / likes / downloads。"""
    url = f"{HF_BASE}/api/models"
    params = {"sort": sort, "direction": direction, "limit": limit}
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _model_to_term(m):
    """HF 模型记录 → 热词卡（term card）。"""
    mid = m.get("id", "")
    author, _, name = mid.partition("/")
    display = name or mid  # 热词展示名用短名，如 Qwen3.8-27B
    likes = m.get("likes", 0) or 0
    downloads = m.get("downloads", 0) or 0
    trending = m.get("trendingScore", 0) or 0
    created = (m.get("createdAt") or "")[:10]
    tags = [t for t in (m.get("tags") or []) if isinstance(t, str)][:6]
    pipeline = m.get("pipeline_tag", "")
    return {
        "term": display,
        "full_id": mid,
        "author": author,
        "type": "模型",
        "official_url": f"https://huggingface.co/{mid}",
        "official_label": f"HuggingFace · {mid}",
        "score": trending,            # 7 日趋势分（上升最快排序键）
        "likes": likes,               # 热度排序键
        "downloads": downloads,
        "trending_score": trending,
        "created_at": created,
        "tags": tags,
        "pipeline_tag": pipeline,
        "meta": f"❤ {likes} · ↓ {downloads} · 📅 {created}" +
                (f" · {pipeline}" if pipeline else ""),
        "community": community_links(display),
        "papers": [],                 # 由 enrich_with_papers 填充
    }


def community_links(term):
    """社区讨论入口（纯 URL 拼接，不调 API）。"""
    q = quote(term)
    return [
        {"site": "知乎",  "url": f"https://www.zhihu.com/search?q={q}"},
        {"site": "B站",   "url": f"https://search.bilibili.com/all?keyword={q}"},
        {"site": "GitHub","url": f"https://github.com/search?q={q}&type=repositories"},
    ]


# ---------- arXiv 相关论文（按热词精确全文检索，见下方 search_arxiv_papers）----------


def _base_model_key(t):
    """归一化模型名为「底模键」，用于把同一底模的变体合并。

    例：Qwen3.8-27B / Qwen3.8-27B-GGUF / Qwen3.8-27B-Uncensored-FP8
        → 都归到 'qwen3.8-27b'
    DeepSeek-V4-Flash / DeepSeek-V4-Flash-AWQ → 'deepseek-v4-flash'
    """
    name = t["full_id"].split("/")[-1].lower()
    # 去掉 -GGUF/-FP8/-MLX/-AWQ/-GPTQ 等量化后缀
    name = re.sub(r"-(gguf|fp8|fp16|bf16|mlx|awq|gptq|int8|int4|uncensored"
                  r"|obli?(terat)?ed|instruct|chat|base)(-.*)?$", "", name)
    return name


def _dedupe_by_base_model(terms):
    """同一底模只保留趋势分最高的变体，保持原顺序。"""
    seen = {}
    for t in terms:
        key = _base_model_key(t)
        if key not in seen or t.get("trending_score", 0) > seen[key].get("trending_score", 0):
            seen[key] = t
    # 按原列表顺序输出（保序去重）
    out, emitted = [], set()
    for t in terms:
        key = _base_model_key(t)
        if key in emitted:
            continue
        emitted.add(key)
        out.append(seen[key])
    return out


# ---------- arXiv 相关论文（按热词精确全文检索）----------
# 设计：不再用「拉一批近期论文 + 族名子串匹配」——族名（如 Qwen / DeepSeek）太宽，
# 会把同族不同版本的论文都算上，产生假阳性，降低用户信任。
# 改为对每个热词单独用 arXiv 全文检索 search_query=all:族名（sortBy=relevance），
# 再用族名在标题/摘要里做词边界匹配过滤，保证论文确实在讨论该模型族。
#
# 代价：arXiv 限速（≥3s/请求，否则 429）。所以只对榜单前若干个热词检索，
# 且串行 + 间隔。榜单是 Top-N，N 不大，可接受。

_arxiv_last_request = 0.0
_arxiv_lock = threading.Lock()


def _arxiv_throttle():
    """保证两次 arXiv 请求间隔 ≥ ARXIV_GAP，避免 429。线程安全。"""
    global _arxiv_last_request
    with _arxiv_lock:
        elapsed = time.time() - _arxiv_last_request
        wait = ARXIV_GAP - elapsed
        if wait > 0:
            time.sleep(wait)
        _arxiv_last_request = time.time()


def _search_query_for(term):
    """根据热词构造 (search_query, family) 对，用于 arXiv 全文检索 + 精确过滤。

    策略：用「模型族名（含次版本）」做 all: 全文检索（召回率高），
    再用 family 在标题里做词边界匹配过滤（精确，避免假阳性）。

    族名提取规则（只去量化/规模后缀，保留次版本号）：
      Qwen3.8-27B-GGUF → 去量化后缀 → Qwen3.8-27B → 去规模 -27B → Qwen3.8
      LTX-2.5          → 去版本 -2.5 → LTX
      DeepSeek-V4-Flash→ DeepSeek-V4-Flash
      FLUX.1-dev       → 去后缀 → FLUX.1

    关键：**不再去掉次版本号**。之前 Qwen3.8→Qwen3 会把 Qwen3 的论文
    错配给 Qwen3.8（同族但不同版本，降低相关性）。改为保留 Qwen3.8，
    若 arXiv 上还没有 Qwen3.8 自己的论文（标题含 Qwen3.8），就显示「无论文」——
    宁缺毋滥，不拿旧版本论文顶替。
    """
    name = term.get("term", "").strip()
    if not name or len(name) < 3:
        return None
    # 1) 去掉社区微调者前缀（Huihui- / 某用户名-）——这类前缀不是模型族名，
    #    检索时反而降低召回。判断：若名字含 '-' 且第一段是已知社区前缀或全小写短词，
    #    去掉第一段。保守起见只去明确的小写社区名。
    name = re.sub(r"^(huihui|nunter|mradermacher|bartowski|cognitivecomputations)-",
                  "", name, flags=re.I)
    # 2) 去掉量化/微调后缀
    base = re.sub(r"-(gguf|fp8|fp16|bf16|mlx|awq|gptq|int8|int4|uncensored"
                  r"|instruct|chat|base|abliterated|ridge|fixed|dev|schnell)(-.*)?$",
                  "", name, flags=re.I)
    if len(base) < 3:
        return None
    # 3) 去掉规模后缀（-27B / -A3B / -35B 等）和尾随版本号（-2.5 / -3）
    family = re.sub(r"-\d+(\.\d+)?[A-Za-z]*$", "", base)       # Qwen3.8-27B→Qwen3.8 ; LTX-2.5→LTX
    # 4) 保留次版本点号（Qwen3.8 不再降级成 Qwen3）——避免拿旧版本论文顶替
    if len(family) < 3:
        return None
    return (f"all:{family}", family)


def search_arxiv_papers(query, family, max_results=8, retries=2):
    """对单个 query 做 arXiv 全文检索，再用 family 词边界过滤，返回相关论文。

    每条：{title,url,published,authors}。失败返回 []。
    过滤（三重，保证相关性）：
    1. 标题须含 family（词边界）——标题提及该模型族才算相关。
       只用标题不用摘要：摘要里的缩写常是同名歧义（LTX 既是视频模型也是
       托卡马克实验），标题含 family 的假阳性低得多。
    2. 排除非 CS 分类——arXiv 全文索引会把物理/数学等同名论文召回，
       剔除 physics.* / math.* / q-bio.* 等分类，只留 cs.* / eess.*。
    3. 标题去重。
    """
    out = []
    family_lc = family.lower()
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(family_lc) + r"(?![a-z0-9])")
    # 允许的分类前缀（AI/CS 相关）；物理/数学等同名论文会被剔除
    cat_ok = re.compile(r"^(cs\.|eess\.|stat\.ML)")
    seen_titles = set()
    for attempt in range(retries + 1):
        _arxiv_throttle()
        try:
            r = requests.get(
                ARXIV_API,
                params={
                    "search_query": query,
                    "max_results": max_results,
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                },
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(ARXIV_GAP * 3)
                continue
            if r.status_code != 200:
                break
            for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
                title = re.search(r"<title>(.*?)</title>", e, re.S)
                link = re.search(r"<id>(.*?)</id>", e)
                pub = re.search(r"<published>(.*?)</published>", e)
                authors = re.findall(r"<name>(.*?)</name>", e)
                cats = re.findall(r'<category[^>]*term="([^"]+)"', e)
                if not (title and link and pub):
                    continue
                t = re.sub(r"\s+", " ", title.group(1)).strip()
                # 过滤1：标题含 family（词边界）
                if not pat.search(t.lower()):
                    continue
                # 过滤2：必须是 CS 相关分类（剔除物理/数学同名论文）
                if cats and not any(cat_ok.match(c) for c in cats):
                    continue
                # 过滤3：去重
                key = t.lower()
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                out.append({
                    "title": t,
                    "url": link.group(1).strip(),
                    "published": pub.group(1)[:10],
                    "authors": authors[:3],
                })
                if len(out) >= 3:
                    break
            break
        except Exception:
            time.sleep(ARXIV_GAP)
    return out


def enrich_with_papers(terms, papers=None):
    """给每个热词卡匹配 arXiv 相关论文（按族名全文检索 + 词边界过滤）。

    papers 参数保留兼容旧调用，但本实现不再使用它——改为逐词检索。
    只对前 ARXIV_ENRICH_LIMIT 个热词检索（避免触发 arXiv 限速）。
    论文是锦上添花，任何失败都不阻塞：失败的热词 papers 留空。
    """
    limit = ARXIV_ENRICH_LIMIT
    for i, t in enumerate(terms):
        if i >= limit:
            t["papers"] = []
            continue
        pair = _search_query_for(t)
        if not pair:
            t["papers"] = []
            continue
        query, family = pair
        try:
            t["papers"] = search_arxiv_papers(query, family, max_results=8)
        except Exception:
            t["papers"] = []
    return terms


# ---------- 顶层聚合 ----------
def _fetch_terms_raw(sort):
    """完整抓取：HF 模型 + 底模去重 + arXiv 论文 enrich。慢（~25s）。
    只在后台预热调用；请求路径不调用。"""
    hf_sort = "trendingScore" if sort == "trending" else "likes"
    models = fetch_hf_models(sort=hf_sort, direction="-1", limit=30)
    terms = [_model_to_term(m) for m in models if m.get("id")]
    terms = _dedupe_by_base_model(terms)
    try:
        enrich_with_papers(terms)
    except Exception:
        pass
    if sort == "trending":
        terms.sort(key=lambda x: x.get("trending_score", 0), reverse=True)
    else:
        terms.sort(key=lambda x: x.get("likes", 0), reverse=True)
    return {
        "ok": True,
        "sort": sort,
        "fetched_at": int(time.time()),
        "count": len(terms),
        "terms": terms,
    }


def _fetch_terms_quick(sort):
    """快速兜底：只抓 HF，不做 arXiv enrich（~1s）。用于文件缓存缺失时的首屏。"""
    hf_sort = "trendingScore" if sort == "trending" else "likes"
    models = fetch_hf_models(sort=hf_sort, direction="-1", limit=30)
    terms = [_model_to_term(m) for m in models if m.get("id")]
    terms = _dedupe_by_base_model(terms)
    if sort == "trending":
        terms.sort(key=lambda x: x.get("trending_score", 0), reverse=True)
    else:
        terms.sort(key=lambda x: x.get("likes", 0), reverse=True)
    return {
        "ok": True,
        "sort": sort,
        "fetched_at": int(time.time()),
        "count": len(terms),
        "terms": terms,
    }


def get_terms(sort="trending"):
    """
    返回热词卡列表。sort='trending' → 7 日上升最快；'top' → 热度最高。

    请求路径只读文件缓存（秒回）；arXiv 检索由后台预热线程定时做。
    缓存缺失（首次启动、预热未完成）时走快速兜底：只抓 HF 不论文，保证首屏不卡。
    """
    # 1) 文件缓存（跨 worker 共享，预热好的热数据）
    data, fetched_at = _file_cache_get(sort)
    if data and (time.time() - fetched_at < CACHE_TTL):
        return data

    # 2) 文件缓存有但已过期 → 仍返回旧数据（后台会刷新），不卡用户
    if data:
        return data

    # 3) 文件缓存完全缺失 → 快速兜底（只 HF，~1s），并写回内存缓存
    try:
        quick = _fetch_terms_quick(sort)
        _set_cache(f"terms:{sort}", quick)
        return quick
    except Exception as e:
        return {"ok": False, "error": f"HF 抓取失败：{e}", "terms": []}


# ---------- 后台预热线程 ----------
_refresh_lock = threading.Lock()        # 串行化预热（多 worker 不会同时打 arXiv）
_refresher_started = False
_refresher_start_lock = threading.Lock()


def _refresh_once(sort):
    """预热单个 sort：完整抓取 + 写文件缓存。失败保留旧缓存。

    返回 True 表示成功，False 表示失败（调用方据此决定下次重试间隔）。
    """
    if not _refresh_lock.acquire(blocking=False):
        return True   # 已有 worker 在预热，视为已处理，跳过（省 arXiv 配额）
    try:
        data = _fetch_terms_raw(sort)
        _file_cache_set(sort, data, data["fetched_at"])
        return True
    except Exception:
        return False  # 失败不抛——保留上一份热缓存继续服务；由后台重试
    finally:
        _refresh_lock.release()


def _bg_refresher():
    """后台循环：启动立即预热一次，之后每 REFRESH_INTERVAL 秒一次。

    重试策略：某次预热失败 → 隔 RETRY_INTERVAL（5 分钟）后重试，而非干等
    6 小时。任一 sort 成功即把该 sort 的文件缓存更新好；失败的 sort 在下个
    重试点再来。所有 sort 都成功后，回到正常 REFRESH_INTERVAL（6 小时）周期。
    """
    # 首次立即预热（不等第一个周期），让服务起来后尽快有热缓存
    for sort in ("trending", "top"):
        _refresh_once(sort)

    next_wait = REFRESH_INTERVAL   # 距离下一次预热的睡眠时长
    while True:
        time.sleep(next_wait)
        any_failed = False
        for sort in ("trending", "top"):
            ok = _refresh_once(sort)
            if not ok:
                any_failed = True
        # 有失败 → 缩短到重试间隔尽快重试；全部成功 → 回到正常周期
        next_wait = RETRY_INTERVAL if any_failed else REFRESH_INTERVAL


def start_background_refresher():
    """启动后台预热线程（daemon，进程退出自动结束）。幂等，多次调用只起一个。"""
    global _refresher_started
    with _refresher_start_lock:
        if _refresher_started:
            return
        _refresher_started = True
    t = threading.Thread(target=_bg_refresher, daemon=True, name="bg-refresher")
    t.start()


def get_term_detail(term_name):
    """单个热词详情：找匹配的 HF 模型 + 社区链接 + 相关论文。

    匹配范围：trending 榜与 likes 榜各拉一次（各 100），合并去重后逐条精确匹配。
    不能只查 likes 榜——trending 上的新热词（如 LTX-2.5）可能不在 likes 榜里，
    只查 likes 会导致 trending 列表里的热词点详情报「未找到」。
    """
    pools = []
    for sort in ("trendingScore", "likes"):
        try:
            pools.append(fetch_hf_models(sort=sort, direction="-1", limit=100))
        except Exception:
            pass

    target = None
    term_lower = term_name.lower()
    for models in pools:
        for m in models:
            mid = (m.get("id") or "")
            name = mid.split("/")[-1]
            mid_lc = mid.lower()
            # 精确匹配：短名、完整 id、或 URL 末段
            if term_lower == name.lower() or term_lower == mid_lc:
                target = m
                break
            # 容错：trending 卡里展示的是短名，详情按短名前缀回查
            if name and name.lower().startswith(term_lower):
                target = m
                break
        if target:
            break
    if not target:
        return {"ok": False, "error": f"未找到热词：{term_name}"}

    card = _model_to_term(target)
    try:
        enrich_with_papers([card])
    except Exception:
        pass
    return {"ok": True, "term": card, "fetched_at": int(time.time())}
