"""
赞助位数据访问层 —— SQLite 存储 + 优雅降级。

设计原则（复刻 tracker.py）：
- 纯 stdlib（sqlite3），零新依赖。
- 任何失败都不阻塞服务：DB 缺失/不可写 → 返回 []/no-op，绝不抛异常。
- 赞助位读不到时回退到 cache/sponsors.json（若有）。
- 所有访问收口在本模块，将来迁 Postgres 只改这里。

表结构见 config/计划文档。WAL 模式让 4 个 gunicorn worker 并发读安全。
"""

import os
import re
import json
import sqlite3
import threading
import datetime

import config

_DB_OK = False                 # DB 是否可用（init 后置 True）
_db_lock = threading.Lock()    # 串行化写（SQLite 写锁）
_today = lambda: datetime.date.today().isoformat()


# ---------- HTML 净化 ----------
# 仅白名单标签 + 白名单属性，剔除 script/on*。给 banner_html 用。
_TAG_OK = {"a", "b", "strong", "em", "i", "br", "span", "p"}
_ATTR_OK = {"href", "title"}

def sanitize_banner_html(html):
    """净化自定义 banner HTML。返回安全字符串。

    策略：逐标签扫描，白名单外的标签整体转义其内容，
    白名单内的标签去掉 on* 与非法属性，a 的 href 仅允许 http(s)/mailto。
    """
    if not html:
        return ""
    out = []
    i = 0
    n = len(html)
    while i < n:
        if html[i] != "<":
            out.append(html[i]); i += 1; continue
        end = html.find(">", i)
        if end == -1:                       # 没闭合，剩余当文本
            out.append(html[i:].replace("<", "&lt;")); break
        tag_chunk = html[i + 1:end]         # 去掉 <>
        is_close = tag_chunk.startswith("/")
        inner = tag_chunk[1:] if is_close else tag_chunk
        name_match = re.match(r"([a-zA-Z0-9]+)", inner)
        if not name_match:
            out.append(html[i:end + 1].replace("<", "&lt;")); i = end + 1; continue
        tag = name_match.group(1).lower()
        if tag not in _TAG_OK:
            # 非白名单标签：丢掉标签本身，保留内部文本（不递归，简单安全）
            i = end + 1; continue
        if is_close:
            out.append(f"</{tag}>"); i = end + 1; continue
        # 开标签：重建属性
        attrs = []
        for m in re.finditer(r'([a-zA-Z-]+)\s*=\s*"([^"]*)"', inner):
            k, v = m.group(1).lower(), m.group(2)
            if k.startswith("on"):
                continue
            if k not in _ATTR_OK:
                continue
            if k == "href":
                v = v.strip()
                if not re.match(r"^(https?:|mailto:|/|#)", v, re.I):
                    continue
            attrs.append(f'{k}="{v}"')
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        out.append(f"<{tag}{attr_str}>"); i = end + 1
    return "".join(out)


# ---------- 初始化 ----------
def init_db():
    """建表 + 开 WAL。失败置 _DB_OK=False，后续全部降级。"""
    global _DB_OK
    config.ensure_data_dir()
    try:
        conn = _conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sponsor_slots (
                slot_id      TEXT PRIMARY KEY,
                name         TEXT,
                text         TEXT,
                subtext      TEXT,
                link_url     TEXT,
                image_url    TEXT,
                banner_html  TEXT,
                region       TEXT DEFAULT 'all',
                active       INTEGER DEFAULT 1,
                sort_order   INTEGER DEFAULT 0,
                start_date   TEXT,
                end_date     TEXT,
                cta_text     TEXT DEFAULT '了解',
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS sponsor_stats (
                slot_id TEXT, date TEXT,
                impressions INTEGER DEFAULT 0,
                clicks      INTEGER DEFAULT 0,
                PRIMARY KEY (slot_id, date)
            );
            CREATE TABLE IF NOT EXISTS pageviews (
                date TEXT PRIMARY KEY, count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS visits (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ip      TEXT NOT NULL,
                country TEXT,             -- ISO 国家码（CN/US…）或 "Unknown"
                path    TEXT,             -- 访问路径，默认 "/"
                ts      TEXT NOT NULL,    -- 完整时间戳 ISO，用于时序
                date    TEXT NOT NULL     -- YYYY-MM-DD，索引化便于按日聚合
            );
            CREATE INDEX IF NOT EXISTS idx_visits_date ON visits(date);
            CREATE INDEX IF NOT EXISTS idx_visits_ip_date ON visits(ip, date);
            CREATE TABLE IF NOT EXISTS search_queries (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                query   TEXT NOT NULL,             -- 用户输入的搜索关键词（原样，已 trim+限长）
                lang    TEXT,                      -- zh / en（搜索时前端语言，便于分语言看热词）
                ip      TEXT,                      -- 搜索者 IP（与 visits 同策略，供去重/细查）
                country TEXT,                      -- ISO 国家码或 "Unknown"
                ts      TEXT NOT NULL,             -- 完整时间戳 ISO，用于时序
                date    TEXT NOT NULL              -- YYYY-MM-DD，索引化便于按日聚合
            );
            CREATE INDEX IF NOT EXISTS idx_search_date ON search_queries(date);
            CREATE INDEX IF NOT EXISTS idx_search_query ON search_queries(query);
            -- v2: 搜索结果点击追踪 → 计算「搜索→点击」漏斗
            CREATE TABLE IF NOT EXISTS search_clicks (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                query   TEXT NOT NULL,             -- 用户搜的词（trim+限长）
                url     TEXT,                      -- 用户点的链接（result.url），用于回查是哪条结果
                ip      TEXT,
                country TEXT,
                ts      TEXT NOT NULL,
                date    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_click_query_date ON search_clicks(query, date);
            CREATE INDEX IF NOT EXISTS idx_click_date ON search_clicks(date);
        """)
        conn.commit()
        _DB_OK = True
    except Exception:
        _DB_OK = False


def _conn():
    """每次请求一个连接（SQLite 连接轻量；线程隔离）。"""
    conn = sqlite3.connect(config.DB_PATH, timeout=3.0)
    conn.row_factory = sqlite3.Row
    return conn


def _within_dates(slot):
    """投放期判断：start/end 为空视为无界。"""
    today = _today()
    s = slot.get("start_date")
    e = slot.get("end_date")
    if s and today < s:
        return False
    if e and today > e:
        return False
    return True


def _row_to_slot(row):
    return dict(row) if row else None


# ---------- 赞助位 CRUD ----------
def list_slots(region=None, active_only=True):
    """返回赞助位列表。DB 不可用时回退 cache/sponsors.json。"""
    if not _DB_OK:
        return _fallback_slots(region, active_only)
    try:
        conn = _conn()
        q = "SELECT * FROM sponsor_slots"
        clauses, params = [], []
        if active_only:
            clauses.append("active = 1")
        if region and region != "all":
            clauses.append("(region = ? OR region = 'all')")
            params.append(region)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY sort_order ASC, created_at ASC"
        rows = conn.execute(q, params).fetchall()
        conn.close()
        slots = [_row_to_slot(r) for r in rows]
        if active_only:
            slots = [s for s in slots if _within_dates(s)]
        return slots
    except Exception:
        return _fallback_slots(region, active_only)


def get_slot(slot_id):
    if not _DB_OK:
        return None
    try:
        conn = _conn()
        row = conn.execute("SELECT * FROM sponsor_slots WHERE slot_id = ?",
                           (slot_id,)).fetchone()
        conn.close()
        return _row_to_slot(row)
    except Exception:
        return None


def upsert_slot(data):
    """新增或更新（按 slot_id）。data 为 dict。返回 slot_id 或 None。"""
    if not _DB_OK:
        return None
    slot_id = (data.get("slot_id") or "").strip()
    if not slot_id:
        return None
    now = _today()
    fields = {
        "slot_id": slot_id,
        "name": (data.get("name") or "").strip()[:120],
        "text": (data.get("text") or "").strip()[:500],
        "subtext": (data.get("subtext") or "").strip()[:200],
        "link_url": (data.get("link_url") or "").strip()[:500],
        "image_url": (data.get("image_url") or "").strip()[:500],
        "banner_html": sanitize_banner_html(data.get("banner_html") or ""),
        "region": (data.get("region") or "all").strip()[:16],
        "active": 1 if str(data.get("active", "1")) in ("1", "true", "on") else 0,
        "sort_order": int(data.get("sort_order") or 0),
        "start_date": (data.get("start_date") or "").strip()[:10] or None,
        "end_date": (data.get("end_date") or "").strip()[:10] or None,
        "cta_text": (data.get("cta_text") or "了解").strip()[:20],
        "updated_at": now,
    }
    try:
        with _db_lock:
            conn = _conn()
            existing = conn.execute("SELECT 1 FROM sponsor_slots WHERE slot_id = ?",
                                    (slot_id,)).fetchone()
            if existing:
                sets = ", ".join(f"{k} = ?" for k in fields if k != "slot_id")
                vals = [fields[k] for k in fields if k != "slot_id"]
                conn.execute(f"UPDATE sponsor_slots SET {sets} WHERE slot_id = ?",
                             vals + [slot_id])
            else:
                fields["created_at"] = now
                cols = ", ".join(fields.keys())
                ph = ", ".join("?" for _ in fields)
                conn.execute(f"INSERT INTO sponsor_slots ({cols}) VALUES ({ph})",
                             list(fields.values()))
            conn.commit()
            conn.close()
        return slot_id
    except Exception:
        return None


def delete_slot(slot_id):
    if not _DB_OK:
        return False
    try:
        with _db_lock:
            conn = _conn()
            conn.execute("DELETE FROM sponsor_slots WHERE slot_id = ?", (slot_id,))
            conn.execute("DELETE FROM sponsor_stats WHERE slot_id = ?", (slot_id,))
            conn.commit()
            conn.close()
        return True
    except Exception:
        return False


def toggle_slot(slot_id):
    """翻转 active。返回新状态或 None。"""
    if not _DB_OK:
        return None
    try:
        with _db_lock:
            conn = _conn()
            row = conn.execute("SELECT active FROM sponsor_slots WHERE slot_id = ?",
                               (slot_id,)).fetchone()
            if not row:
                conn.close(); return None
            new = 0 if row["active"] else 1
            conn.execute("UPDATE sponsor_slots SET active = ?, updated_at = ? WHERE slot_id = ?",
                         (new, _today(), slot_id))
            conn.commit()
            conn.close()
        return new
    except Exception:
        return None


# ---------- 统计 ----------
def record_pageview():
    """全站 PV +1。best-effort，失败静默。"""
    if not _DB_OK or not config.ANALYTICS_ENABLED:
        return
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO pageviews(date, count) VALUES(?, 1) "
                "ON CONFLICT(date) DO UPDATE SET count = count + 1",
                (_today(),))
            conn.commit()
            conn.close()
    except Exception:
        pass


def record_impression(slot_id):
    if not _DB_OK or not config.ANALYTICS_ENABLED or not slot_id:
        return
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO sponsor_stats(slot_id, date, impressions, clicks) VALUES(?, ?, 1, 0) "
                "ON CONFLICT(slot_id, date) DO UPDATE SET impressions = impressions + 1",
                (slot_id, _today()))
            conn.commit()
            conn.close()
    except Exception:
        pass


def record_click(slot_id):
    if not _DB_OK or not config.ANALYTICS_ENABLED or not slot_id:
        return
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO sponsor_stats(slot_id, date, impressions, clicks) VALUES(?, ?, 0, 1) "
                "ON CONFLICT(slot_id, date) DO UPDATE SET clicks = clicks + 1",
                (slot_id, _today()))
            conn.commit()
            conn.close()
    except Exception:
        pass


def stats_30d():
    """返回 {pageviews: N, sponsors: [{slot_id,name,impressions,clicks}]}。DB 挂返回空。"""
    if not _DB_OK:
        return {"pageviews": 0, "sponsors": []}
    try:
        conn = _conn()
        since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        pv = conn.execute(
            "SELECT COALESCE(SUM(count),0) AS c FROM pageviews WHERE date >= ?", (since,)
        ).fetchone()["c"]
        rows = conn.execute(
            """SELECT s.slot_id, s.name,
                      COALESCE(SUM(st.impressions),0) AS impressions,
                      COALESCE(SUM(st.clicks),0) AS clicks
               FROM sponsor_slots s
               LEFT JOIN sponsor_stats st ON s.slot_id = st.slot_id AND st.date >= ?
               GROUP BY s.slot_id ORDER BY impressions DESC""", (since,)).fetchall()
        conn.close()
        return {
            "pageviews": pv,
            "sponsors": [_row_to_slot(r) for r in rows],
        }
    except Exception:
        return {"pageviews": 0, "sponsors": []}


# ---------- GeoLite2 离线地域查询（懒加载，缺失即降级）----------
_geoip_reader = None
_geoip_lock = threading.Lock()
_geoip_unavailable = False   # 一旦确认不可用，后续直接跳过，避免每次请求都 try import


def geoip_country(ip):
    """返回 ISO 国家码（如 'CN'），失败/无库返回 'Unknown'。线程安全、懒加载。

    geoip2 在函数内懒导入：未装 / 无 mmdb → 置 _geoip_unavailable=True，
    后续请求直接返回 Unknown，全程不抛、不阻塞服务（与 store 整体降级哲学一致）。
    """
    global _geoip_reader, _geoip_unavailable
    if _geoip_unavailable or not ip:
        return "Unknown"
    with _geoip_lock:
        if _geoip_reader is None:
            try:
                import geoip2.database
                _geoip_reader = geoip2.database.Reader(config.GEOIP_DB_PATH)
            except Exception:
                _geoip_unavailable = True
                return "Unknown"
    try:
        resp = _geoip_reader.country(ip)
        cc = (resp.country.iso_code or "").strip().upper()
        return cc or "Unknown"
    except Exception:
        # AddressNotFoundError（库内无此 IP，如 1.1.1.1 / 172.18.x 私网）→ 统一 Unknown。
        return "Unknown"


# ---------- 访问记录（监控页数据源）----------
def record_visit(ip, country, path="/"):
    """记录一次访问到 visits 表。best-effort，失败静默（与 record_pageview 同模式）。

    每次访问一行：PV = 行数，UV = COUNT(DISTINCT ip)。
    存完整 IP（用户决策）以便去重与未来细查；country 来自反代头 / GeoLite2。
    """
    if not _DB_OK or not config.ANALYTICS_ENABLED or not ip:
        return
    now = datetime.datetime.now()
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO visits(ip, country, path, ts, date) VALUES(?,?,?,?,?)",
                (ip, country or "Unknown", path,
                 now.isoformat(timespec="seconds"), now.date().isoformat()))
            conn.commit()
            conn.close()
    except Exception:
        pass


def monitor_stats(days=30):
    """返回监控页所需数据：总览 + 每日趋势 + 地域分布 + 近期明细。

    DB 不可用返回零值，与 stats_30d() 同模式，绝不抛异常。
    """
    empty = {"total_pv": 0, "total_uv": 0, "today_pv": 0, "today_uv": 0,
             "regions": [], "daily": [], "recent": []}
    if not _DB_OK:
        return empty
    try:
        conn = _conn()
        today = _today()
        since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        # 总览（近 N 天 PV / UV）
        agg = conn.execute(
            "SELECT COUNT(*) AS pv, COUNT(DISTINCT ip) AS uv FROM visits WHERE date >= ?",
            (since,)).fetchone()
        td = conn.execute(
            "SELECT COUNT(*) AS pv, COUNT(DISTINCT ip) AS uv FROM visits WHERE date = ?",
            (today,)).fetchone()
        # 地域分布（近 N 天，按独立 IP 数降序 —— 关注 IP 地域的核心指标）
        regions_rows = conn.execute(
            """SELECT country, COUNT(DISTINCT ip) AS c FROM visits
               WHERE date >= ? GROUP BY country ORDER BY c DESC""", (since,)).fetchall()
        total_region = sum(r["c"] for r in regions_rows) or 1
        regions = [{"country": r["country"] or "Unknown",
                    "count": r["c"],
                    "pct": round(r["c"] * 100 / total_region, 1)} for r in regions_rows]
        # 每日 PV / UV 时序
        daily_rows = conn.execute(
            """SELECT date, COUNT(*) AS pv, COUNT(DISTINCT ip) AS uv FROM visits
               WHERE date >= ? GROUP BY date ORDER BY date ASC""", (since,)).fetchall()
        daily = [{"date": r["date"], "pv": r["pv"], "uv": r["uv"]} for r in daily_rows]
        # 近期访问明细（最近 30 条，体现存完整 IP + 关注地域）
        recent_rows = conn.execute(
            """SELECT ip, country, path, ts FROM visits
               ORDER BY id DESC LIMIT 30""").fetchall()
        recent = [{"ip": r["ip"], "country": r["country"] or "Unknown",
                   "path": r["path"], "ts": r["ts"]} for r in recent_rows]
        conn.close()
        return {
            "total_pv": agg["pv"], "total_uv": agg["uv"],
            "today_pv": td["pv"], "today_uv": td["uv"],
            "regions": regions, "daily": daily, "recent": recent,
        }
    except Exception:
        return empty


# ---------- 用户搜索记录（搜索功能 + 后台监控）----------
def record_search_query(query, lang=None, ip=None, country=None):
    """记录一次用户搜索关键词到 search_queries 表。

    best-effort，失败静默（与 record_visit / record_pageview 同模式）。
    query 经 trim + 限长 80 字符，空串不入库。每次搜索一行，供监控页统计
    热门搜索词 / 近期搜索词 / 搜索 PV。
    """
    if not _DB_OK or not config.ANALYTICS_ENABLED:
        return
    q = (query or "").strip()
    if not q:
        return
    q = q[:80]   # 限长，防超长输入撑库
    now = datetime.datetime.now()
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO search_queries(query, lang, ip, country, ts, date) "
                "VALUES(?,?,?,?,?,?)",
                (q, lang, ip or "", country or "Unknown",
                 now.isoformat(timespec="seconds"), now.date().isoformat()))
            conn.commit()
            conn.close()
    except Exception:
        pass


def search_stats(days=30, top_n=20):
    """返回监控页搜索统计：总搜索次数、独立关键词数、热门词 Top-N、近期搜索。

    DB 不可用返回零值，与 monitor_stats() 同模式，绝不抛异常。
    """
    empty = {"total": 0, "unique": 0, "today": 0,
             "top": [], "recent": []}
    if not _DB_OK:
        return empty
    try:
        conn = _conn()
        today = _today()
        since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        agg = conn.execute(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT query) AS u "
            "FROM search_queries WHERE date >= ?", (since,)).fetchone()
        td = conn.execute(
            "SELECT COUNT(*) AS c FROM search_queries WHERE date = ?",
            (today,)).fetchone()
        top_rows = conn.execute(
            """SELECT query, COUNT(*) AS c FROM search_queries
               WHERE date >= ? GROUP BY query ORDER BY c DESC, query ASC LIMIT ?""",
            (since, top_n)).fetchall()
        top = [{"query": r["query"], "count": r["c"]} for r in top_rows]
        recent_rows = conn.execute(
            """SELECT query, lang, country, ts FROM search_queries
               ORDER BY id DESC LIMIT 30""").fetchall()
        recent = [{"query": r["query"], "lang": r["lang"],
                   "country": r["country"] or "Unknown", "ts": r["ts"]}
                  for r in recent_rows]
        conn.close()
        return {
            "total": agg["c"], "unique": agg["u"], "today": td["c"],
            "top": top, "recent": recent,
        }
    except Exception:
        return empty


def record_search_click(query, url=None, ip=None, country=None):
    """记录一次「搜索结果点击」到 search_clicks 表。

    best-effort，与 record_search_query 同模式。每次用户点了搜索结果卡里的
    链接就落一条；供监控漏斗：哪些搜索词真的有转化（点击率 = clicks / searches）。
    """
    if not _DB_OK or not config.ANALYTICS_ENABLED:
        return
    q = (query or "").strip()[:80]
    u = (url or "").strip()[:500]
    if not q:
        return
    now = datetime.datetime.now()
    try:
        with _db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO search_clicks(query, url, ip, country, ts, date) "
                "VALUES(?,?,?,?,?,?)",
                (q, u, ip or "", country or "Unknown",
                 now.isoformat(timespec="seconds"), now.date().isoformat()))
            conn.commit()
            conn.close()
    except Exception:
        pass


def search_funnel(days=30, top_n=15):
    """返回搜索→点击漏斗：每个热门词的搜索次数、点击次数、点击率。

    数据源：search_queries（搜索次数，按 query+date 去重）+ search_clicks
    （点击次数，按 query+date 去重）。点击率 = clicks / searches，0 次搜
    索的词不出现在漏斗里。供 monitor.html 用。
    """
    empty = {"total_searches": 0, "total_clicks": 0, "items": []}
    if not _DB_OK:
        return empty
    try:
        conn = _conn()
        since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        # 每个 query 的搜索次数（按搜索行计，不去重用户）
        s_rows = conn.execute(
            """SELECT query, COUNT(*) AS s FROM search_queries
               WHERE date >= ? AND query != ''
               GROUP BY query""", (since,)).fetchall()
        # 每个 query 的点击次数
        c_rows = conn.execute(
            """SELECT query, COUNT(*) AS c FROM search_clicks
               WHERE date >= ? AND query != ''
               GROUP BY query""", (since,)).fetchall()
        clicks_map = {r["query"]: r["c"] for r in c_rows}
        items = []
        total_s = 0
        total_c = 0
        for r in s_rows:
            q = r["query"]; s = r["s"]; c = clicks_map.get(q, 0)
            total_s += s; total_c += c
            rate = round(c / s * 100, 1) if s else 0
            items.append({"query": q, "searches": s, "clicks": c, "rate": rate})
        # 按搜索次数降序截断 Top-N
        items.sort(key=lambda x: (-x["searches"], x["query"]))
        conn.close()
        return {
            "total_searches": total_s,
            "total_clicks": total_c,
            "items": items[:top_n],
        }
    except Exception:
        return empty


def search_suggest(prefix, limit=8):
    """根据 prefix 返回热门搜索词建议（前缀匹配优先 + 包含匹配兜底）。

    数据源：近 30 天 search_queries 表，去重 query + 按 COUNT 降序。
    空串 / DB 不可用返回 []。供搜索框输入时实时下拉补全。
    """
    if not _DB_OK or not prefix:
        return []
    p = prefix.strip()
    if not p:
        return []
    p_sql = p.replace("%", "\\%").replace("_", "\\_")
    try:
        conn = _conn()
        since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        # 优先：prefix 开头；再：包含 prefix
        rows = conn.execute(
            """SELECT query, COUNT(*) AS c FROM search_queries
               WHERE date >= ? AND query LIKE ? ESCAPE '\\' AND query != ''
               GROUP BY query
               ORDER BY (query LIKE ?) DESC, c DESC, query ASC
               LIMIT ?""",
            (since, "%" + p_sql + "%", p_sql + "%", limit)).fetchall()
        conn.close()
        return [{"query": r["query"], "count": r["c"]} for r in rows]
    except Exception:
        return []


# ---------- 降级回退 ----------
def _fallback_slots(region=None, active_only=True):
    """DB 不可用时读 cache/sponsors.json（若有）。"""
    try:
        path = os.path.join(config.CACHE_DIR, "sponsors.json")
        with open(path, "r", encoding="utf-8") as f:
            slots = json.load(f)
        if active_only:
            slots = [s for s in slots if s.get("active", 1) and _within_dates(s)]
        if region and region != "all":
            slots = [s for s in slots if s.get("region", "all") in (region, "all")]
        return slots
    except Exception:
        return []


# 启动即初始化（失败也安全，后续降级）
init_db()
