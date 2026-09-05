# aitrendwatch 代码索引（Agent 入口）

> **Agent 进场第一份文件。** 读这里定位目标，按需精读单文件，避免全量扫描。
> 本索引基于 dev 分支（release 1.2.1, 2026-08）代码实读生成，行号真实可跳。

## 项目一句话定位

AI 热点聚合单页应用：Flask 后端聚合 36 个 RSS 源（含 4 个 Google News 关键词源） + HuggingFace 模型榜 + arXiv 论文，DeepSeek 做维度打标/双语翻译，产出「热词卡 / 事件卡」，前端单页展示 + 后台定时预热。

## 目录树（仅项目代码）

```
aitrendwatch/
├── app.py          # Flask 入口 + 路由 + 8 个直连抓取源、词详情装配（1747 行；2026-09-05 SEO：词条页 indexable 可索引门槛 + hreflang 传参 + sitemap 主语言 en + 热度口径 desc）
├── config.py       # 全部配置/环境变量/降级开关 + LLM 故障转移链 + 思考强度 + 质量/可用性分离阈值 + 二次提示轮数 + SEO 词条可索引阈值 TERM_INDEX_MIN_NEWS/HOT（186 行）
├── dims.py         # 维度事件层：RSS 抓取 + HN/Reddit 热度 + LLM 故障转移链打标/抽词 + 热词解释生成（1852 行；2026-09-04 需求 1：逐条流卡 id url 归一 + 标题级去重；需求 4：中文标题公司专名关键词保持中文原词、热词翻译提示词禁拼音化/自造英文——两段提示词提升为模块常量 _USER_PREFIX/_TRANSLATE_SYS_MSG）
├── tracker.py      # 热词追踪层：HF 模型榜 + arXiv 论文检索（590 行）
├── terms.py        # 词粒度聚合层：热词池归并 + 三榜打分 + 周期快照 + 词典回填 + 词条解释（动态词典：词池即词典，2336 行，新增；rise 用近 7 天滑动窗口报道数环比；需求 5：抽词关键词经 case_match_original 对齐原文大小写；需求 5 改进：词典外词 display 优先原文表面形态（WorkBuddy 不再显示 Workbuddy），词典权威词存量脏 display 随刷新回归词典规则（Saas→SaaS）；2026-09-02：display_en 增量翻译上限 TRANSLATE_BATCH_MAX_WORDS；2026-09-04 需求 1：_title_key 去重键剥标点加严（委托 text_utils.normalized_title_key），当轮 url 归一到存储键同口径（normalize_url_key）；需求 2：分隔符孪生 canonical 归并——normalize_term 折叠词典治理紧凑孪生（hugging-face→huggingface），refresh_words 按去 '-' 紧凑分组归并自由孪生（ai-agent/aiagent），删残留行/迁快照，榜单无同词两行；需求 4：中文公司/机构专名 display_en 由 _COMPANY_EN_GLOSSARY 官方英名词典确定性映射（词典优先不进 LLM 批次，存量拼音脏值随刷新回归），未收录中文专名不拼音化走 LLM 兜底；2026-09-05 SEO：term_row_indexable 词条可索引判定（sitemap/详情页共用）、get_term_trend 按日聚合近 7 天活跃度趋势、list_terms_for_sitemap 过滤达标词）
├── store.py        # SQLite：赞助位 + 访问统计 + GeoIP + 用户行为事件（819 行）
├── news_store.py   # SQLite：事件卡历史库（upsert/list_history，含 keywords 列，落库前 canonical 归一 + churn 防护；2026-09-04 需求 1：url 主键实体解码+去片段+去 utm 归一 + 批次去重 + 存量孪生行自愈删除）（574 行）
├── stream_utils.py # 统一信息流卡片去重与维度计数规则（70 行）
├── text_utils.py   # RSS 文本/URL HTML entity 解码 + 2026-09-04 需求 1 归一键（url/标题去重键唯一实现源）（147 行）
├── version.py      # 版本号（读 VERSION 文件）（23 行）
├── VERSION         # 版本号单一真相源（1.9.1）
├── templates/      # 8 个 Jinja2 模板
│   ├── index.html         # 首页主单页（1640 行：词卡/逐条新闻双视图，JS fetch + i18n + 埋点追踪，header 含 🤗 HF 入口，页尾悬浮回到顶部按钮；2026-09-05 SEO：热度口径标注 tooltip/footer 脚注 + hreflang head + meta keywords 移除 + 报道来源标签）
│   ├── hf.html            # HuggingFace 独立排序页（438 行：趋势/点赞/下载排序 + pipeline 标签，开源动向；hreflang zh↔en）
│   ├── terms.html         # 服务条款页（383 行）
│   ├── term_detail.html   # 通用热词聚合页（424 行：相关报道聚合 + HF 区块 + 词解释 + 近 7 天活跃度趋势迷你图 + 热度口径脚注 + hreflang + indexable 门槛 noindex 分支；2026-09-05 SEO）
│   ├── search.html        # 搜索结果页（583 行：含热词命中卡区）
│   ├── admin.html         # 赞助位管理后台（353 行，已废弃，合并到 monitor.html）
│   ├── admin_login.html   # 管理员登录（68 行）
│   └── monitor.html       # 统一管理后台：流量监控 + 赞助位管理 Tab 切换（1052 行）
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
| `app.py` | 1747 | Flask 入口、路由、8 直连源抓取、词详情装配 + 2026-09-05 SEO（词条 indexable 门槛传参、hreflang zh↔en、sitemap 主语言 en） | 40 个路由 view 函数（含 `admin_sponsors_list`）+ `_word_detail` + `_explain_fallback` + `_hf_models_for` | tracker, dims, terms, config, store, stream_utils, text_utils |
| `config.py` | 186 | 配置集中地 + LLM 故障转移链 + 思考强度 + `ensure_data_dir()` + SEO 词条可索引阈值 `TERM_INDEX_MIN_NEWS`/`TERM_INDEX_MIN_HOT` | `ensure_data_dir`, `llm_endpoint`, `llm_reasoning_params` | os |
| `dims.py` | 1852 | RSS 事件层 + LLM 故障转移链打标/抽词 + 热词解释生成（09-02：链每轮复位/逐条校验/402 账户级；09-03：质量失败与 provider 故障分离 + 坏条目二次提示修正；09-04 需求 1：逐条流 id url 归一 + `_dedupe_news_titles` 标题级去重；需求 4：抽词/翻译提示词规则防中文公司专名拼音化——`_USER_PREFIX`/`_TRANSLATE_SYS_MSG` 模块常量） | `get_dims`, `get_news_cards`, `start_background_dims_refresher`, `enrich_with_signals`, `_llm_classify_batch`, `explain_terms` | config, requests, terms, text_utils |
| `tracker.py` | 590 | HF 热词 + arXiv 论文（词池数据源） | `get_model_cards`, `get_term_detail`, `start_background_refresher` | requests |
| `terms.py` | 2336 | 词粒度聚合：热词池归并 + 三榜打分 + 快照 + 词典回填 + 动态解释维护（词池即词典）+ 热窗新鲜度加权 + 关键词大小写校验 + 词典外词 display 保留原文大小写（词典权威词存量脏值随刷新回归词典规则）+ 需求 2：分隔符孪生 canonical 归并（normalize 折叠词典治理紧凑孪生 hugging-face→huggingface；refresh 按去 '-' 紧凑分组归并自由孪生 ai-agent/aiagent，删残留行/迁快照，榜单无同词两行）+ 需求 1：`_title_key` 剥标点加严（委托 `text_utils.normalized_title_key`）、当轮 url 归一（`normalize_url_key`）+ 需求 4：中文公司/机构专名官方英文名词典优先（`_COMPANY_EN_GLOSSARY`：display_en 确定性映射、不进 LLM 批次、存量拼音脏值随刷新回归；未收录专名不拼音化）+ 2026-09-05 SEO：`term_row_indexable` 可索引判定 + `get_term_trend` 快照按日聚合趋势 + `list_terms_for_sitemap` 过滤达标词 | `refresh_words`, `get_word_cards`, `get_term_row`, `get_term_explanation`, `get_term_news`, `get_term_trend`, `term_row_indexable`, `list_terms_for_sitemap`, `backfill_history`, `normalize_term`, `extract_keywords_dict`, `case_match_original` | config, sqlite3, news_store, text_utils |
| `store.py` | 819 | 赞助位/统计/GeoIP/用户行为事件 SQLite | `list_slots`, `upsert_slot`, `record_visit`, `monitor_stats`, `geoip_country`, `record_event`, `record_events_batch`, `event_stats` | config, sqlite3 |
| `news_store.py` | 574 | 事件卡历史库 SQLite（url 归一键主键 + 批次去重 + 存量孪生行自愈删除 + keywords 列 + churn 防护） | `upsert_cards`, `list_history_cards`, `count_history`, `search_history` | config, sqlite3, text_utils |
| `stream_utils.py` | 70 | 统一信息流卡片身份、去重、维度成员与计数 | `card_identity`, `dedupe_cards`, `dimension_members`, `dimension_counts`, `dimension_list` | — |
| `text_utils.py` | 147 | 文本有界双层解码、URL 单层解码与危险 scheme 拦截 + 需求 1 归一键（`normalize_url_key` url 键 / `normalized_title_key` 标题键） | `decode_html_entities`, `decode_url_entities`, `normalize_url_key`, `normalized_title_key` | — |
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
