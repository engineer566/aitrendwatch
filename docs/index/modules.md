# 模块函数索引

> 9 个 Python 模块的结构、函数签名、行号、职责。配合 [INDEX.md](../INDEX.md) 使用。
> 行号基于 dev 分支实读。`_` 前缀为模块内部函数，归组列出但不展开细节。

---

## app.py  （1364 行）— Flask 入口 + 路由 + 直连抓取

### 分区清单
| 行号范围 | 分区（`# ----------` 注释段） |
|----------|------------------------------|
| 54–80 | 通用配置（UA/HEADERS/TIMEOUT/CACHE_TTL/`_cache`） |
| 85–245 | SEO 辅助 + 首页 SSR 词卡装配 |
| 281–472 | 各数据源抓取函数（8 个 `fetch_*`） |
| 473–649 | 路由公共配置（`SOURCES`/`SOURCE_META`/region/ip 辅助） |
| 650–811 | 页面 + 词流路由（含语言参数、稳定分类计数） |
| 812–1110 | 全站搜索 v2 |
| 1112–1200 | SEO 路由（robots/sitemap/favicon） |
| 1201–1210 | 赞助位点击跳转 |
| 1211–1294 | 管理后台 |
| 1295–1347 | 流量监控页 |
| 1348–1353 | `__main__` 入口 |

### 公开函数（被路由/外部调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `fetch_baidu()` | 282 | 百度热搜官方接口 |
| `fetch_bilibili()` | 304 | B站热门官方接口 |
| `fetch_toutiao()` | 324 | 今日头条热榜 |
| `fetch_hackernews()` | 341 | HN Firebase API（逐条拉取，慢） |
| `fetch_github()` | 369 | GitHub Trending HTML 抓取 |
| `fetch_zhihu()` | 399 | 知乎热榜（直连） |
| `fetch_douyin()` | 423 | 抖音热搜（直连） |
| `fetch_weibo()` | 445 | 微博热搜（需登录态，常失败） |
| `detect_region()` | 499 | Accept-Language → zh/global |
| `_client_ip()` | 519 | 取真实 IP（信任 X-Forwarded-For） |
| `_client_country(ip)` | 527 | 反代头优先 + GeoLite2 兜底 |
| `get_source(source)` | 541 | 带缓存单源抓取 |
| `get_source_timeout(source)` | 556 | 带硬性截止时间单源抓取 |
| 37 个 view 函数 | 见 [api_routes.md](api_routes.md) | 路由处理 |

### 模块级常量
`SOURCES`（source→fetcher 映射，`app.py:476`）、`SOURCE_META`（8 源元信息，`app.py:487`）、`WORD_STREAM_LIMIT=60`、`SSR_INITIAL_LIMIT=20`、`UA`/`HEADERS`/`TIMEOUT=5`/`SOURCE_DEADLINE=25`/`CACHE_TTL=300`。

---

## config.py  （126 行）— 配置集中地

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 12–16 | Flask 会话签名 `SECRET_KEY` |
| 17–19 | 管理后台令牌 `ADMIN_TOKEN` |
| 21–26 | 站点信息 `SITE_NAME`/`BASE_URL`/`CONTACT_EMAIL` |
| 28–49 | 数据存储路径 + GeoIP + 缓存目录 |
| 51–80 | LLM 提供方（模型故障转移链 `LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD` + `llm_endpoint()`） |
| 81–89 | dims 定点预热 `DIMS_REFRESH_HOURS` |
| 90–97 | 分析开关 |
| 98–105 | SEO 开关 |
| 106–115 | 第三方广告（AdSense/百度联盟） |
| 116–119 | 赞助位展示 |
| 120–126 | `ensure_data_dir()` |

### 公开函数
| 函数 | 行号 | 职责 |
|------|------|------|
| `_as_bool(v, default)` | 66 | 字符串→布尔 |
| `ensure_data_dir()` | 96 | 建 `DATA_DIR`（容器 /app/data，本地 ./data） |

### 关键常量
`SECRET_KEY`、`ADMIN_TOKEN`（未设→admin 路由 404 隐身）、`DB_PATH`/`NEWS_DB_PATH`、`GEOIP_DB_PATH`、`CACHE_DIR`、`LLM_CHAIN`（默认 `glm-4.7-flash,glm-4.6v-flash,glm-4.6v-flashx,glm-4.7-flashx,deepseek-v4-flash`）、`LLM_FAILOVER_THRESHOLD=10`、`DEEPSEEK_API_KEY`/`DEEPSEEK_URL`、`GLM_API_KEY`/`GLM_URL`（智谱 BigModel 免费档，高峰 429/1305 过载）、`llm_endpoint(model)`（deepseek-* → DeepSeek，其余 glm-* → GLM）、`DIMS_REFRESH_HOURS=(13,19,1,7)`、`ANALYTICS_ENABLED`、`SEO_ENABLED`/`SITEMAP_MAX_URLS`/`TERM_DETAIL_CACHE_TTL=1800`、`ADSENSE_ENABLED`/`ADSENSE_CLIENT`、`BAIDU_ADS_ENABLED`/`BAIDU_ADS_CPRO_ID`、`INLINE_SLOT_EVERY_N=8`、`NEWS_HISTORY_LIMIT=400`/`NEWS_HISTORY_DAYS=30`。

---

## dims.py  （1289 行）— 维度事件层（RSS + 热度 + LLM）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 56–72 | LLM 配置（模型故障转移链） |
| 74–143 | 文件缓存（`cache/dims.json`） |
| 145–341 | RSS 源定义 `RSS_SOURCES`（17 源）+ RSS 解析 + 抓取 |
| 342–651 | 社区热度增强（HN/Reddit/复合分/趋势分） |
| 652–712 | LLM 配置与故障转移状态 |
| 713–953 | LLM 批量打标、双语字段与关键词降级 |
| 954–1138 | 顶层聚合（`get_dims`/`get_news_cards`） |
| 1139–1289 | 后台预热线程 + 跨进程锁 + 定点刷新 |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_dims(dimension=None, lang="zh")` | 1055 | 维度热词分组（只读缓存）；`/api/dims` |
| `get_news_cards(lang="zh")` | 1089 | news 卡列表（读缓存 + 历史库）；`/api/stream` |
| `enrich_with_signals(items)` | 621 | 给事件卡加 HN/Reddit/复合分（公开，可外部调） |
| `start_background_dims_refresher()` | 1281 | 启动后台预热线程（app.py 启动时调） |

### 内部函数（按分区归组）
- 缓存：`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`
- RSS：`_norm_date`/`_strip_cdata`/`_parse_rss`/`fetch_one_rss`/`fetch_all_rss`
- 热度：`_has_cjk`/`_clean_title`/`_hn_points`/`_reddit_points`/`_buzz`/`_age_hours`/`_time_decay`/`_composite_score`/`_trend_score`
- LLM：`_active_llm`/`_llm_success`/`_llm_failure`（故障转移状态机）/`_llm_classify_batch`/`enrich_with_llm`/`_LLMTransientError`（瞬态错误类）/`_strip_llm_title_suffix`（剥翻译标题尾部 `| 来源` 噪音）
- 聚合：`_to_card`/`_fetch_dims_raw`/`_project_card`
- 后台：`_cross_proc_lock`/`_persist_to_history`/`_dims_refresh_once`/`_seconds_until_next_refresh_hour`/`_bg_dims_refresher`

### 模块级常量
`RSS_SOURCES`（17 源，`dims.py:153`）、`PER_SOURCE_LIMIT=6`、`DIMS_CACHE_TTL`、`DIMS_REFRESH_HOURS`、`LLM_BATCH=12`、`LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD`（自 config 导入）、`_LLM_ACTIVE_IDX`/`_LLM_FAILS`（故障转移进程级状态）、`DIMENSIONS`（维度枚举，被 `/api/stream` 引用）。

---

## tracker.py  （590 行）— 热词追踪层（HF + arXiv）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 34–63 | 文件缓存（`cache/terms.json`）+ 内存缓存 |
| 110–161 | HF 模型热词抓取 |
| 162–354 | arXiv 论文检索（限速 + 检索式构造） |
| 355–462 | 顶层聚合（`get_terms`/`get_model_cards`） |
| 463–589 | 后台预热线程 + 跨进程锁 |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_terms(sort="trending")` | 399 | 热词榜（trending/top 两种 sort，读缓存）；`/api/trending` `/api/top` |
| `get_model_cards(lang="zh")` | 424 | model 卡列表（读缓存）；`/api/stream` |
| `get_term_detail(term_name)` | 551 | 单热词详情：live HF + 同步 arXiv（~1-4s）；`/api/term/` `/term/` |
| `start_background_refresher()` | 540 | 启动后台预热线程（app.py 启动时调） |

### 内部函数
- 缓存：`_cached`/`_set_cache`/`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`
- HF：`fetch_hf_models`/`_model_to_term`/`community_links`
- arXiv：`_base_model_key`/`_dedupe_by_base_model`/`_arxiv_throttle`/`_search_query_for`/`search_arxiv_papers`/`enrich_with_papers`
- 聚合：`_fetch_terms_raw`/`_fetch_terms_quick`
- 后台：`_cross_proc_lock`/`_refresh_once`/`_bg_refresher`

### 模块级常量
`HF_BASE="https://hf-mirror.com"`（官方 HF 不可达走镜像）、`ARXIV_API`、`ARXIV_GAP=3.0`（限速）、`ARXIV_ENRICH_LIMIT=8`（只检索前 N 热词）、`UA`/`HEADERS`/`TIMEOUT=8`。

---

## store.py  （474 行）— 赞助位/统计/GeoIP SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 27–78 | HTML 净化 + 初始化 `init_db` |
| 131–153 | 连接 + 行映射 |
| 154–276 | 赞助位 CRUD |
| 277–326 | 统计（PV/曝光/点击） |
| 353–384 | GeoLite2 离线地域查询 |
| 385–456 | 访问记录 + `monitor_stats` |
| 457–474 | 降级回退 `_fallback_slots` |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 80 | 建表 + 开 WAL（失败 `_DB_OK=False`） |
| `list_slots(region, active_only)` | 155 | 列赞助位 |
| `get_slot(slot_id)` | 181 | 取单条 |
| `upsert_slot(data)` | 194 | 新建/更新 |
| `delete_slot(slot_id)` | 241 | 删除 |
| `toggle_slot(slot_id)` | 256 | 上下架切换 |
| `record_pageview()` | 278 | PV+1 |
| `record_impression(slot_id)` | 295 | 曝光+1 |
| `record_click(slot_id)` | 311 | 点击+1 |
| `stats_30d()` | 327 | 30 天统计 |
| `geoip_country(ip)` | 359 | GeoLite2 查国家码（无库返 Unknown） |
| `record_visit(ip, country, path)` | 386 | 写 visits 表（监控页数据源） |
| `monitor_stats(days=30)` | 408 | 监控页聚合（PV/UV/地域） |

### SQLite 表
`sponsor_slots`、`sponsor_stats`、`pageviews`、`visits`（见 [data_flow.md](data_flow.md) §SQLite）。

---

## news_store.py  （350 行）— 事件卡历史库 SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 35–115 | 初始化 `init_db` + `_migrate`（keywords 列 + 维度映射） |
| 116–239 | 写：`upsert_cards`（含 keywords + 实体归一化） |
| 240–350 | 读：`list_history_cards`/`count_history`/`search_history`/行投影 |

### 公开函数（被 dims.py / terms.py / app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 35 | 建 `news_cards` 表 + 索引 + WAL + 幂等迁移 |
| `_migrate(conn)` | 88 | 加 keywords 列；旧维度值 → 新 6 类映射 |
| `upsert_cards(cards)` | 117 | 刷新后 upsert 本轮全部 cards（含 keywords 落库） |
| `list_history_cards(limit, include_inactive, days)` | 241 | 合并历史库扩大内容池 |
| `count_history()` | 273 | 历史条数 |
| `search_history(query, lang, limit)` | 286 | 历史库 LIKE 搜索（含 keywords 字段） |

### SQLite 表
`news_cards`（含 `keywords` JSON 列；见 [data_flow.md](data_flow.md) §SQLite）。

---

## terms.py  （1319 行）— 词粒度聚合层（词维度重构，新增）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 42–149 | 词池规模控制 + words.json 文件缓存（复刻 dims.py） |
| 150–197 | SQLite `init_db`：`terms` / `term_snapshots` 表 + WAL |
| 198–437 | 关键词词典 `_LEXICON`（209）+ 热词解释 `_EXPLANATIONS`（311）+ `_ALIAS`/`_ASCII_PATTERNS`（版本感知词边界） |
| 440–663 | `normalize_term` / `extract_keywords_dict` / 历史关联匹配辅助 |
| 666–1058 | 词聚合 + 三榜打分 + 快照（`refresh_words`，dims 刷新锁内调） |
| 1060–1214 | 读：`get_word_cards` / `get_term_row` / `get_term_explanation` / `get_term_news` |
| 1216–1319 | `list_terms_for_sitemap` + 历史回填 `backfill_history` + CLI |

### 公开函数（被 app.py / dims.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 151 | 建 `terms`/`term_snapshots` 表 + WAL（失败 `_DB_OK=False`） |
| `normalize_term(s)` | 440 | 任意词形 → canonical 键（小写/别名/去复数） |
| `extract_keywords_dict(title)` | 464 | 词典匹配抽词（无 LLM key 降级 + 回填） |
| `refresh_words(all_cards, model_cards)` | 666 | 词池归并 + 热度/上升/新奇度打分 + 快照 + 写 words.json |
| `get_word_cards(sort, lang, limit)` | 1060 | `/api/stream?view=words` 数据源（读 words.json，先完整排序再截取再投影） |
| `get_term_row(term)` | 1106 | 查 terms 主表（canonical 键） |
| `get_term_explanation(term, lang)` | 1122 | 热词解释（`_EXPLANATIONS` 词典，zh/en 投影，未收录空串） |
| `get_term_news(term, limit, lang)` | 1136 | 词 → 关联报道（canonical/别名 + 标题边界兜底） |
| `list_terms_for_sitemap(limit)` | 1216 | sitemap 词表（热度降序） |
| `backfill_history(days, force)` | 1232 | 词典回填 keywords + 合成历史快照（幂等，--force 全量） |

### SQLite 表
`terms`（词主表：term/display/display_zh/origin/first_seen_at/total_mentions/hf_json/cur_hot/cur_rise/cur_novelty）、`term_snapshots`（(term,cycle) 周期快照支撑环比）。

---

## stream_utils.py  （70 行）— 统一信息流口径辅助

`card_identity`、`dedupe_cards`、`dimension_members`、`dimension_counts`、`dimension_list` 为后端 `/api/stream` 与测试共用的卡片去重、维度成员和计数规则；不依赖外部库。

---

## text_utils.py  （78 行）— RSS 文本/URL 实体解码

`decode_html_entities` 对文本做有界双层解码，`decode_url_entities` 只解一层并拒绝危险 URL scheme；供 `dims.py`、`news_store.py`、`terms.py` 统一处理历史缓存与新抓取数据。

---

## version.py  （23 行）— 版本号
- `_read_version()` (10) 读 `VERSION` 文件；`__version__` (22) / `version` 别名。
- 单一真相源：同目录 `VERSION` 文件（当前 `1.2.1`）。
