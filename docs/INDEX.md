# aitrendwatch 代码索引（Agent 入口）

> **Agent 进场第一份文件。** 读这里定位目标，按需精读单文件，避免全量扫描。
> 本索引基于 dev 分支（release 1.2.1, 2026-08）代码实读生成，行号真实可跳。

## 项目一句话定位

AI 热点聚合单页应用：Flask 后端聚合 36 个 RSS 源（含 4 个 Google News 关键词源） + HuggingFace 模型榜 + arXiv 论文，DeepSeek 做维度打标/双语翻译，产出「热词卡 / 事件卡」，前端单页展示 + 后台定时预热。

## 目录树（仅项目代码）

```
aitrendwatch/
├── app.py          # Flask 入口 + 路由 + 8 个直连抓取源、词详情装配（1689 行）
├── config.py       # 全部配置/环境变量/降级开关 + LLM 故障转移链 + 思考强度（158 行）
├── dims.py         # 维度事件层：RSS 抓取 + HN/Reddit 热度 + LLM 故障转移链打标/抽词 + 热词解释生成（1605 行）
├── tracker.py      # 热词追踪层：HF 模型榜 + arXiv 论文检索（590 行）
├── terms.py        # 词粒度聚合层：热词池归并 + 三榜打分 + 周期快照 + 词典回填 + 词条解释（动态词典：词池即词典，1630 行，新增；rise 用近 7 天滑动窗口报道数环比）
├── store.py        # SQLite：赞助位 + 访问统计 + GeoIP + 用户行为事件（819 行）
├── news_store.py   # SQLite：事件卡历史库（upsert/list_history，含 keywords 列，落库前 canonical 归一 + churn 防护）（433 行）
├── stream_utils.py # 统一信息流卡片去重与维度计数规则（70 行）
├── text_utils.py   # RSS 文本/URL HTML entity 解码（78 行）
├── version.py      # 版本号（读 VERSION 文件）（23 行）
├── VERSION         # 版本号单一真相源（1.6.2）
├── templates/      # 8 个 Jinja2 模板
│   ├── index.html         # 首页主单页（1564 行：词卡/逐条新闻双视图，JS fetch + i18n + 埋点追踪，header 含 🤗 HF 入口）
│   ├── hf.html            # HuggingFace 独立排序页（355 行：趋势/点赞/下载排序 + pipeline 标签，开源动向）
│   ├── terms.html         # 服务条款页（371 行）
│   ├── term_detail.html   # 通用热词聚合页（322 行：相关报道聚合 + HF 区块 + 词解释）
│   ├── search.html        # 搜索结果页（527 行：含热词命中卡区）
│   ├── admin.html         # 赞助位管理后台（353 行，已废弃，合并到 monitor.html）
│   ├── admin_login.html   # 管理员登录（68 行）
│   └── monitor.html       # 统一管理后台：流量监控 + 赞助位管理 Tab 切换（928 行）
├── data/           # SQLite 库（sponsors.db, news.db）+ GeoLite2（运行产物，.gitkeep 占位）
├── cache/          # 文件缓存产物（terms.json, dims.json, words.json + .refresh.lock）
├── history/        # 开发任务备忘（非代码）
├── skills/         # 项目技能（aitrendwatch-task-workflow：需求开发闭环 SKILL.md + 合并清理脚本）
├── docs/           # 代码索引 + Codex 项目记忆
│   ├── PROJECT_MEMORY.md # 迁移来的项目记忆索引
│   └── memory/           # 6 条按主题拆分的记忆条目（含上线前回归清单）
├── vendor/         # ⚠️ vendored 依赖（flask/gunicorn/requests…），勿索引勿读
├── requirements.txt
├── Dockerfile / docker-compose.yml / docker-compose.prod.yml
├── .env.example    # 环境变量样板
├── AGENTS.md       # Codex 项目 agent 守则（自动入口）
├── CLAUDE.md       # 原 Claude 项目守则（迁移凭据）
└── CLAUDE_MEMORY_EXPORT.md # 原 Claude 记忆导出（迁移凭据）
```

**⚠️ `vendor/`（~13 万行）是 pip 安装的第三方库源码，非本项目代码。任何 agent 不应读、不应索引、不应改。**

## 三句话架构

1. **Flask 单体**：`app.py` 一个进程，gunicorn 多 worker，路由直接调 `tracker`/`dims`/`terms`/`store` 读缓存秒回。
2. **三层后台预热**：`tracker`（6h）和 `dims`（定点 13/19/01/07）各起 daemon 线程抓取写文件缓存；dims 刷新锁内再调 `terms.refresh_words` 归并热词池 + 三榜打分写 `words.json`。用 `fcntl` 跨进程文件锁串行化，整个容器任意时刻只有一个 worker 在抓取。
3. **三级缓存**：进程内 `dict`（秒级）→ 文件 `cache/*.json`（跨 worker 共享）→ SQLite（赞助位/统计/事件卡历史库/词池）。

## 模块速查表

| 文件 | 行数 | 职责 | 顶层公开函数（被 app.py 或外部调用） | 依赖 |
|------|------|------|---------------------------------------|------|
| `app.py` | 1567 | Flask 入口、路由、8 直连源抓取、词详情装配 | 40 个路由 view 函数（含 `admin_sponsors_list`）+ `_word_detail` + `_explain_fallback` + `_hf_models_for` | tracker, dims, terms, config, store, stream_utils |
| `config.py` | 158 | 配置集中地 + LLM 故障转移链 + 思考强度 + `ensure_data_dir()` | `ensure_data_dir`, `llm_endpoint`, `llm_reasoning_params` | os |
| `dims.py` | 1607 | RSS 事件层 + LLM 故障转移链打标/抽词 + 热词解释生成 | `get_dims`, `get_news_cards`, `start_background_dims_refresher`, `enrich_with_signals`, `_llm_classify_batch`, `explain_terms` | config, requests, terms, text_utils |
| `tracker.py` | 590 | HF 热词 + arXiv 论文（词池数据源） | `get_model_cards`, `get_term_detail`, `start_background_refresher` | requests |
| `terms.py` | 1582 | 词粒度聚合：热词池归并 + 三榜打分 + 快照 + 词典回填 + 动态解释维护（词池即词典）+ 热窗新鲜度加权 | `refresh_words`, `get_word_cards`, `get_term_row`, `get_term_explanation`, `get_term_news`, `list_terms_for_sitemap`, `backfill_history`, `normalize_term`, `extract_keywords_dict` | config, sqlite3, news_store, text_utils |
| `store.py` | 819 | 赞助位/统计/GeoIP/用户行为事件 SQLite | `list_slots`, `upsert_slot`, `record_visit`, `monitor_stats`, `geoip_country`, `record_event`, `record_events_batch`, `event_stats` | config, sqlite3 |
| `news_store.py` | 433 | 事件卡历史库 SQLite（含 keywords 列 + churn 防护） | `upsert_cards`, `list_history_cards`, `count_history`, `search_history` | config, sqlite3, text_utils |
| `stream_utils.py` | 70 | 统一信息流卡片身份、去重、维度成员与计数 | `card_identity`, `dedupe_cards`, `dimension_members`, `dimension_counts`, `dimension_list` | — |
| `text_utils.py` | 78 | 文本有界双层解码、URL 单层解码与危险 scheme 拦截 | `decode_html_entities`, `decode_url_entities` | — |
| `version.py` | 23 | 版本号 | `__version__` | pathlib |

## 按任务跳转表

| 要做的事 | 先读这个 L2 索引 | 然后精读 |
|----------|------------------|----------|
| 加/改一条路由 | `index/api_routes.md` | `app.py` 对应 view |
| 改某个抓取源（RSS/HF/arXiv） | `index/data_flow.md` | `dims.py`/`tracker.py` 对应函数 |
| 改抽词/词典/词聚合/三榜打分 | `index/modules.md`（terms.py） | `terms.py` 对应函数 |
| 理解模块结构/找某函数 | `index/modules.md` | 目标 `.py` 文件 |
| 改前端页面/JS fetch | `index/frontend.md` | 目标 `templates/*.html` |
| 理解请求链路/缓存/后台预热 | `index/architecture.md` | `app.py` + `tracker.py`/`dims.py`/`terms.py` |
| 改配置/环境变量/降级开关 | `index/data_flow.md` §环境变量 | `config.py` |
| 改 SQLite 表/统计逻辑 | `index/data_flow.md` §SQLite schema | `store.py`/`news_store.py`/`terms.py` |
| 加 Google Analytics / 广告 | `index/api_routes.md` + `index/frontend.md` | `templates/index.html` |

## L2 索引清单

- [architecture.md](index/architecture.md) — 模块依赖图、请求生命周期、后台预热、缓存层级
- [api_routes.md](index/api_routes.md) — 39 条路由全表（路径/方法/函数/行号/分组）
- [modules.md](index/modules.md) — 9 个 Python 模块函数索引（签名/行号/职责/分区）
- [frontend.md](index/frontend.md) — 8 个模板索引（用途/区块/行号/API 引用）
- [data_flow.md](index/data_flow.md) — 外部数据源、SQLite schema、缓存产物、环境变量

## Agent 使用约定

1. **进场先读根目录 `AGENTS.md` 和本文件**，再读 `PROJECT_MEMORY.md` 索引；用「模块速查表」+「按任务跳转表」定位，再精读单文件。
2. 行号锚点格式 `file.py:行号`，可直接跳读对应源码段。
3. `vendor/`、`cache/`、`data/`、`__pycache__/` 永远不读——分别是依赖源码、运行缓存、运行数据、字节码。
4. 索引与代码可能随开发漂移；改动代码后顺手更新对应索引条目（保持行号同步）。
