# aitrendwatch 代码索引（Agent 入口）

> **Agent 进场第一份文件。** 读这里定位目标，按需精读单文件，避免全量扫描。
> 本索引基于 dev 分支（release 0.2.1, 2026-08）代码实读生成，行号真实可跳。

## 项目一句话定位

AI 热点聚合单页应用：Flask 后端聚合 17 个 RSS 源 + HuggingFace 模型榜 + arXiv 论文，DeepSeek 做维度打标/双语翻译，产出「热词卡 / 事件卡」，前端单页展示 + 后台定时预热。

## 目录树（仅项目代码）

```
aitrendwatch/
├── app.py          # Flask 入口 + 37 条路由 + 8 个直连抓取源（859 行）
├── config.py       # 全部配置/环境变量/降级开关（101 行）
├── dims.py         # 维度事件层：RSS 抓取 + HN/Reddit 热度 + DeepSeek 打标（1110 行）
├── tracker.py      # 热词追踪层：HF 模型榜 + arXiv 论文检索（590 行）
├── store.py        # SQLite：赞助位 + 访问统计 + GeoIP（474 行）
├── news_store.py   # SQLite：事件卡历史库（upsert/list_history）（251 行）
├── version.py      # 版本号（读 VERSION 文件）（23 行）
├── VERSION         # 版本号单一真相源（0.2.1）
├── templates/      # 6 个 Jinja2 模板（共 2337 行）
│   ├── index.html         # 首页主单页（970 行，含 JS fetch + i18n）
│   ├── terms.html         # 热词榜页（363 行）
│   ├── term_detail.html   # 单热词详情页（209 行）
│   ├── admin.html         # 赞助位管理后台（345 行）
│   ├── admin_login.html   # 管理员登录（60 行）
│   └── monitor.html       # 流量监控页（390 行）
├── data/           # SQLite 库（sponsors.db, news.db）+ GeoLite2（运行产物，.gitkeep 占位）
├── cache/          # 文件缓存产物（terms.json, dims.json + .refresh.lock）
├── history/        # 开发任务备忘（非代码）
├── docs/           # 本索引
├── vendor/         # ⚠️ vendored 依赖（flask/gunicorn/requests…），勿索引勿读
├── requirements.txt
├── Dockerfile / docker-compose.yml / docker-compose.prod.yml
├── .env.example    # 环境变量样板
└── CLAUDE.md       # 项目 agent 守则
```

**⚠️ `vendor/`（~13 万行）是 pip 安装的第三方库源码，非本项目代码。任何 agent 不应读、不应索引、不应改。**

## 三句话架构

1. **Flask 单体**：`app.py` 一个进程，gunicorn 多 worker，路由直接调 `tracker`/`dims`/`store` 读缓存秒回。
2. **两层后台预热**：`tracker` 和 `dims` 各起一个 daemon 线程，定时抓取写文件缓存；请求路径只读缓存。用 `fcntl` 跨进程文件锁串行化，整个容器任意时刻只有一个 worker 在抓取。
3. **三级缓存**：进程内 `dict`（秒级）→ 文件 `cache/*.json`（跨 worker 共享，TTL 5–30 分钟）→ SQLite（赞助位/统计/事件卡历史库）。

## 模块速查表

| 文件 | 行数 | 职责 | 顶层公开函数（被 app.py 或外部调用） | 依赖 |
|------|------|------|---------------------------------------|------|
| `app.py` | 859 | Flask 入口、路由、8 直连源抓取 | 37 个路由 view 函数 | tracker, dims, config, store |
| `config.py` | 101 | 配置集中地 + `ensure_data_dir()` | `ensure_data_dir` | os |
| `dims.py` | 1110 | RSS 事件层 + DeepSeek 打标 | `get_dims`, `get_news_cards`, `start_background_dims_refresher`, `enrich_with_signals` | config, requests |
| `tracker.py` | 590 | HF 热词 + arXiv 论文 | `get_terms`, `get_model_cards`, `get_term_detail`, `start_background_refresher` | requests |
| `store.py` | 474 | 赞助位/统计/GeoIP SQLite | `list_slots`, `upsert_slot`, `record_visit`, `monitor_stats`, `geoip_country` | config, sqlite3 |
| `news_store.py` | 251 | 事件卡历史库 SQLite | `upsert_cards`, `list_history_cards`, `count_history` | config, sqlite3 |
| `version.py` | 23 | 版本号 | `__version__` | pathlib |

## 按任务跳转表

| 要做的事 | 先读这个 L2 索引 | 然后精读 |
|----------|------------------|----------|
| 加/改一条路由 | `index/api_routes.md` | `app.py` 对应 view |
| 改某个抓取源（RSS/HF/arXiv） | `index/data_flow.md` | `dims.py`/`tracker.py` 对应函数 |
| 理解模块结构/找某函数 | `index/modules.md` | 目标 `.py` 文件 |
| 改前端页面/JS fetch | `index/frontend.md` | 目标 `templates/*.html` |
| 理解请求链路/缓存/后台预热 | `index/architecture.md` | `app.py` + `tracker.py`/`dims.py` |
| 改配置/环境变量/降级开关 | `index/data_flow.md` §环境变量 | `config.py` |
| 改 SQLite 表/统计逻辑 | `index/data_flow.md` §SQLite schema | `store.py`/`news_store.py` |
| 加 Google Analytics / 广告 | `index/api_routes.md` + `index/frontend.md` | `templates/index.html` |

## L2 索引清单

- [architecture.md](index/architecture.md) — 模块依赖图、请求生命周期、后台预热、缓存层级
- [api_routes.md](index/api_routes.md) — 37 条路由全表（路径/方法/函数/行号/分组）
- [modules.md](index/modules.md) — 6 个 Python 模块函数索引（签名/行号/职责/分区）
- [frontend.md](index/frontend.md) — 6 个模板索引（用途/区块/行号/API 引用）
- [data_flow.md](index/data_flow.md) — 外部数据源、SQLite schema、缓存产物、环境变量

## Agent 使用约定

1. **进场先读本文件**，用「模块速查表」+「按任务跳转表」定位，再精读单文件。
2. 行号锚点格式 `file.py:行号`，可直接跳读对应源码段。
3. `vendor/`、`cache/`、`data/`、`__pycache__/` 永远不读——分别是依赖源码、运行缓存、运行数据、字节码。
4. 索引与代码可能随开发漂移；改动代码后顺手更新对应索引条目（保持行号同步）。
