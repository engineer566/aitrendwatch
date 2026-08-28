"""
新闻历史持久化层 —— SQLite 存储 + 优雅降级。

解决 issue 6「内容过少」：dims.py 每次后台刷新只保留当轮 cards（覆盖写
dims.json），历史内容丢失。本模块把每轮 cards 持久化到 SQLite，按
official_url 去重 upsert，保留首次发现时间，score/trend/hot 每次刷新
重新计算（issue 6 明确要求「每次后端刷新，热度/趋势/上升评分都重新计算」）。

设计原则（复刻 store.py）：
- 纯 stdlib（sqlite3），零新依赖。
- 任何失败都不阻塞服务：DB 缺失/不可写 → 返回 []/no-op，绝不抛异常。
- WAL 模式让 4 个 gunicorn worker 并发读安全。
- 所有访问收口在本模块。

表结构：
  news_cards —— 每条新闻一行，url 为自然主键。
    首次发现存 first_seen_at / first_published；后续刷新只更新 score/trend/hot
    等动态字段，保留 first_seen_at 不变（历史溯源）。
"""

import os
import json
import sqlite3
import threading
import datetime

import config

_DB_OK = False                 # DB 是否可用（init 后置 True）
_db_lock = threading.Lock()    # 串行化写（SQLite 写锁）
_now_iso = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def init_db():
    """建表 + 开 WAL。失败置 _DB_OK=False，后续全部降级。"""
    global _DB_OK
    config.ensure_data_dir()
    try:
        conn = _conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS news_cards (
                url              TEXT PRIMARY KEY,   -- official_url，自然主键
                title            TEXT,
                title_zh         TEXT,
                title_en         TEXT,
                summary_zh       TEXT,
                summary_en       TEXT,
                dimension        TEXT,
                source           TEXT,
                region           TEXT,
                published        TEXT,               -- YYYY-MM-DD
                hn_points        INTEGER DEFAULT 0,
                reddit_score     INTEGER DEFAULT 0,
                reddit_comments  INTEGER DEFAULT 0,
                score            INTEGER DEFAULT 0, -- 累计热度，每次刷新重算
                trend            INTEGER DEFAULT 0, -- 上升势头，每次刷新重算
                hot              INTEGER DEFAULT 0,
                first_seen_at    TEXT,              -- 首次入库时间（ISO），不变
                last_refresh_at  TEXT,              -- 最近一次刷新时间（ISO）
                active           INTEGER DEFAULT 1  -- 0=近期未被刷新命中（历史归档）
            );
            CREATE INDEX IF NOT EXISTS idx_news_score ON news_cards(score DESC);
            CREATE INDEX IF NOT EXISTS idx_news_trend ON news_cards(trend DESC);
            CREATE INDEX IF NOT EXISTS idx_news_published ON news_cards(published DESC);
            CREATE INDEX IF NOT EXISTS idx_news_dim ON news_cards(dimension);
        """)
        conn.commit()
        conn.close()
        _DB_OK = True
    except Exception:
        _DB_OK = False


def _conn():
    """每次请求一个连接（SQLite 连接轻量；线程隔离）。"""
    conn = sqlite3.connect(config.NEWS_DB_PATH, timeout=3.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 写：刷新后 upsert 本轮全部 cards ----------
def upsert_cards(cards):
    """把一轮刷新的 cards 批量 upsert 到 news_cards。

    - 已存在的 url：更新所有动态字段（score/trend/hot/社区信号/title 投影），
      保留 first_seen_at，刷新 last_refresh_at，置 active=1。
    - 新 url：插入，first_seen_at = last_refresh_at = 当前时间。
    - 本轮未命中的既有 active=1 记录：置 active=0（标记为历史归档，仍可被读取）。

    事务内完成，失败静默（不阻塞 dims 刷新主流程）。
    """
    if not _DB_OK or not cards:
        return
    now = _now_iso()
    seen_urls = set()
    try:
        with _db_lock:
            conn = _conn()
            conn.executemany(
                """
                INSERT INTO news_cards (
                    url, title, title_zh, title_en, summary_zh, summary_en,
                    dimension, source, region, published,
                    hn_points, reddit_score, reddit_comments,
                    score, trend, hot,
                    first_seen_at, last_refresh_at, active
                ) VALUES (
                    :url, :title, :title_zh, :title_en, :summary_zh, :summary_en,
                    :dimension, :source, :region, :published,
                    :hn_points, :reddit_score, :reddit_comments,
                    :score, :trend, :hot,
                    :first_seen_at, :last_refresh_at, 1
                )
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    title_zh=excluded.title_zh,
                    title_en=excluded.title_en,
                    summary_zh=excluded.summary_zh,
                    summary_en=excluded.summary_en,
                    dimension=excluded.dimension,
                    source=excluded.source,
                    region=excluded.region,
                    published=excluded.published,
                    hn_points=excluded.hn_points,
                    reddit_score=excluded.reddit_score,
                    reddit_comments=excluded.reddit_comments,
                    score=excluded.score,
                    trend=excluded.trend,
                    hot=excluded.hot,
                    last_refresh_at=excluded.last_refresh_at,
                    active=1
                """,
                [_card_to_row(c, now) for c in cards],
            )
            # 收集本轮命中的 url，把既有的 active=1 但本轮未命中者置 0
            seen_urls = {c.get("official_url") or c.get("url") or c.get("title", "")
                         for c in cards}
            if seen_urls:
                # SQLite 参数上限 999，分批处理
                seen_list = list(seen_urls)
                BATCH = 500
                for i in range(0, len(seen_list), BATCH):
                    chunk = seen_list[i:i + BATCH]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        "UPDATE news_cards SET active=0 WHERE active=1 AND url NOT IN (%s)"
                        % placeholders,
                        chunk,
                    )
            conn.commit()
            conn.close()
    except Exception:
        pass


def _card_to_row(c, now):
    url = c.get("official_url") or c.get("url") or c.get("title", "")
    return {
        "url": url,
        "title": c.get("title", ""),
        "title_zh": c.get("title_zh", c.get("title", "")),
        "title_en": c.get("title_en", c.get("title", "")),
        "summary_zh": c.get("summary_zh", ""),
        "summary_en": c.get("summary_en", ""),
        "dimension": c.get("dimension", "其他"),
        "source": c.get("source", ""),
        "region": c.get("region", ""),
        "published": c.get("published", ""),
        "hn_points": int(c.get("hn_points", 0) or 0),
        "reddit_score": int(c.get("reddit_score", 0) or 0),
        "reddit_comments": int(c.get("reddit_comments", 0) or 0),
        "score": int(c.get("score", 0) or 0),
        "trend": int(c.get("trend", 0) or 0),
        "hot": int(c.get("hot", 0) or c.get("score", 0) or 0),
        # ON CONFLICT 不更新 first_seen_at，这里给新行用；旧行被覆盖忽略
        "first_seen_at": now,
        "last_refresh_at": now,
    }


# ---------- 读：合并历史库扩大内容池 ----------
def list_history_cards(limit=400, include_inactive=True, days=None):
    """返回历史 news cards（含当轮 active 与历史 inactive）。

    - limit：最多返回条数（按 score 降序兜底，保证高热内容在前）。
    - include_inactive：是否包含 active=0 的历史归档卡。默认 True，扩大内容池。
    - days：仅返回最近 N 天内 published 的卡；None=不限。

    每行转成与 dims._to_card 同 schema 的 dict，供 get_news_cards 合并。
    """
    if not _DB_OK:
        return []
    try:
        conn = _conn()
        clauses, params = [], []
        if not include_inactive:
            clauses.append("active = 1")
        if days:
            cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
            clauses.append("published >= ?")
            params.append(cutoff)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "%s ORDER BY score DESC, published DESC LIMIT ?"
            % ("SELECT * FROM news_cards" + where, ),
            params + [limit],
        ).fetchall()
        conn.close()
        return [_row_to_card(r) for r in rows]
    except Exception:
        return []


def count_history():
    """返回历史库总条数（含 inactive）。DB 不可用返回 0。"""
    if not _DB_OK:
        return 0
    try:
        conn = _conn()
        n = conn.execute("SELECT COUNT(*) FROM news_cards").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def search_history(query, lang="zh", limit=50):
    """在历史 news 库中按关键词搜索，返回匹配卡列表（与 dims 卡同 schema）。

    全字段 LIKE 模糊匹配（title_zh / title_en / summary_zh / summary_en /
    source / dimension），覆盖中英文标题与摘要。DB 不可用或空 query 返回 []。
    结果按 score 降序 + published 降序兜底，limit 截断。
    """
    if not _DB_OK or not query:
        return []
    q = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        conn = _conn()
        rows = conn.execute(
            """SELECT * FROM news_cards
               WHERE title_zh LIKE ? ESCAPE '\\'
                  OR title_en LIKE ? ESCAPE '\\'
                  OR summary_zh LIKE ? ESCAPE '\\'
                  OR summary_en LIKE ? ESCAPE '\\'
                  OR source LIKE ? ESCAPE '\\'
                  OR dimension LIKE ? ESCAPE '\\'
               ORDER BY score DESC, published DESC LIMIT ?""",
            (q, q, q, q, q, q, limit),
        ).fetchall()
        conn.close()
        return [_row_to_card(r) for r in rows]
    except Exception:
        return []


def _row_to_card(r):
    """DB 行 → 与 dims 卡同 schema 的 dict。"""
    d = dict(r)
    return {
        "title": d.get("title") or "",
        "title_zh": d.get("title_zh") or d.get("title", ""),
        "title_en": d.get("title_en") or d.get("title", ""),
        "summary_zh": d.get("summary_zh") or "",
        "summary_en": d.get("summary_en") or "",
        "dimension": d.get("dimension") or "其他",
        "official_url": d.get("url"),
        "source": d.get("source") or "",
        "region": d.get("region") or "",
        "published": d.get("published") or "",
        "hn_points": d.get("hn_points", 0),
        "reddit_score": d.get("reddit_score", 0),
        "reddit_comments": d.get("reddit_comments", 0),
        "score": d.get("score", 0),
        "trend": d.get("trend", 0),
        "hot": d.get("hot", 0) or d.get("score", 0),
    }


init_db()
