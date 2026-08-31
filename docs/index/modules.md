# 模块函数索引

> 9 个 Python 模块的结构、函数签名、行号、职责。配合 [INDEX.md](../INDEX.md) 使用。
> 行号基于 dev 分支实读。`_` 前缀为模块内部函数，归组列出但不展开细节。

---

## app.py  （1488 行）— Flask 入口 + 路由 + 直连抓取

### 分区清单
| 行号范围 | 分区（`# ----------` 注释段） |
|----------|------------------------------|
| 54–84 | 通用配置（UA/HEADERS/TIMEOUT/CACHE_TTL/`_cache`） |
| 85–319 | SEO 辅助 + 词详情装配（`_explain_fallback`@103 解释模板兜底 / `_word_detail`@131） |
| 320–511 | 各数据源抓取函数（8 个 `fetch_*`） |
| 512–688 | 路由公共配置（`SOURCES`@515/`SOURCE_META`@526/region/ip 辅助） |
| 689–839 | 页面 + 词流路由（含语言参数、稳定分类计数） |
| 840–946 | HuggingFace 独立排序页（`_hf_models_for`@852 / `/hf`@873 / `/api/hf`@902） |
| 947–1246 | 全站搜索 v2 |
| 1247–1335 | SEO 路由（robots/sitemap/favicon） |
| 1336–1345 | 赞助位点击跳转 |
| 1346–1429 | 管理后台 |
| 1430–1488 | 流量监控页 + `__main__` 入口（1483） |

### 公开函数（被路由/外部调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `fetch_baidu()` | 321 | 百度热搜官方接口 |
| `fetch_bilibili()` | 343 | B站热门官方接口 |
| `fetch_toutiao()` | 363 | 今日头条热榜 |
| `fetch_hackernews()` | 380 | HN Firebase API（逐条拉取，慢） |
| `fetch_github()` | 408 | GitHub Trending HTML 抓取 |
| `fetch_zhihu()` | 438 | 知乎热榜（直连） |
| `fetch_douyin()` | 462 | 抖音热搜（直连） |
| `fetch_weibo()` | 484 | 微博热搜（需登录态，常失败） |
| `detect_region()` | 538 | Accept-Language → zh/global |
| `_client_ip()` | 558 | 取真实 IP（信任 X-Forwarded-For） |
| `_client_country(ip)` | 566 | 反代头优先 + GeoLite2 兜底 |
| `get_source(source)` | 580 | 带缓存单源抓取 |
| `get_source_timeout(source)` | 595 | 带硬性截止时间单源抓取 |
| 39 个 view 函数 | 见 [api_routes.md](api_routes.md) | 路由处理 |

### 模块级常量
`SOURCES`（source→fetcher 映射，`app.py:515`）、`SOURCE_META`（8 源元信息，`app.py:526`）、`WORD_STREAM_LIMIT=60`、`SSR_INITIAL_LIMIT=20`、`UA`/`HEADERS`/`TIMEOUT=5`/`SOURCE_DEADLINE=25`/`CACHE_TTL=300`。

---

## config.py  （158 行）— 配置集中地

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 13–16 | Flask 会话签名 `SECRET_KEY` |
| 18–20 | 管理后台令牌 `ADMIN_TOKEN` |
| 22–27 | 站点信息 `SITE_NAME`/`BASE_URL`/`CONTACT_EMAIL` |
| 29–50 | 数据存储路径 + GeoIP + 缓存目录 |
| 52–112 | LLM 提供方（模型故障转移链 `LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD`/`LLM_CYCLE_ESCAPE` + 思考强度 `LLM_REASONING_EFFORT` + `llm_endpoint()`/`llm_reasoning_params()`） |
| 113–121 | dims 定点预热 `DIMS_REFRESH_HOURS` |
| 122–129 | 分析开关 |
| 130–137 | SEO 开关 |
| 138–147 | 第三方广告（AdSense/百度联盟） |
| 148–151 | 赞助位展示 |
| 152–158 | `ensure_data_dir()` |

### 公开函数
| 函数 | 行号 | 职责 |
|------|------|------|
| `_as_bool(v, default)` | 123 | 字符串→布尔 |
| `ensure_data_dir()` | 153 | 建 `DATA_DIR`（容器 /app/data，本地 ./data） |
| `llm_endpoint(model)` | 93 | 模型 ID → (url, api_key)：deepseek-* → DeepSeek，其余 → 智谱 BigModel |
| `llm_reasoning_params(model)` | 101 | 模型 ID → 思考强度参数 dict：glm-5.2+ 返回 `{"reasoning_effort": ...}`，glm-4.7/deepseek 返回 {}（不传未知参数） |

### 关键常量
`SECRET_KEY`、`ADMIN_TOKEN`（未设→admin 路由 404 隐身）、`DB_PATH`/`NEWS_DB_PATH`、`GEOIP_DB_PATH`、`CACHE_DIR`、`LLM_CHAIN`（默认 `glm-4.7-flash,glm-5.3-flash,deepseek-v4-flash`）、`LLM_FAILOVER_THRESHOLD=3`、`LLM_CYCLE_ESCAPE=4`、`LLM_REASONING_EFFORT=low`（可选 low/high/max，仅 glm-5.2+ 生效）、`DEEPSEEK_API_KEY`/`DEEPSEEK_URL`、`GLM_API_KEY`/`GLM_URL`（智谱 BigModel 免费档，高峰 429/1305 过载）、`DIMS_REFRESH_HOURS=(1,7,13,19)`、`ANALYTICS_ENABLED`、`SEO_ENABLED`/`SITEMAP_MAX_URLS`/`TERM_DETAIL_CACHE_TTL=1800`、`ADSENSE_ENABLED`/`ADSENSE_CLIENT`、`BAIDU_ADS_ENABLED`/`BAIDU_ADS_CPRO_ID`、`INLINE_SLOT_EVERY_N=8`、`NEWS_HISTORY_LIMIT=400`/`NEWS_HISTORY_DAYS=30`。

---

## dims.py  （1538 行）— 维度事件层（RSS + 热度 + LLM）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 58–75 | LLM 配置（模型故障转移链） |
| 76–146 | 文件缓存（`cache/dims.json`） |
| 147–343 | RSS 源定义 `RSS_SOURCES`（17 源）+ RSS 解析 + 抓取 |
| 344–653 | 社区热度增强（HN/Reddit/复合分/趋势分） |
| 654–1195 | LLM 批量打标（679–755 故障转移状态；756–1035 `_llm_classify_batch`；1037–1076 `enrich_with_llm`；1078–1123 `_translate_terms`；1125–1188 `explain_terms` 热词解释生成/优化） |
| 1196–1383 | 顶层聚合（`get_dims`/`get_news_cards`/`_fetch_dims_raw`） |
| 1384–1538 | 后台预热线程 + 跨进程锁 + 定点刷新 |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_dims(dimension=None, lang="zh")` | 1297 | 维度热词分组（只读缓存）；`/api/dims` |
| `get_news_cards(lang="zh")` | 1331 | news 卡列表（读缓存 + 历史库）；`/api/stream` |
| `enrich_with_signals(items)` | 623 | 给事件卡加 HN/Reddit/复合分（公开，可外部调） |
| `start_background_dims_refresher()` | 1530 | 启动后台预热线程（app.py 启动时调） |

### 内部函数（按分区归组）
- 缓存：`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`
- RSS：`_norm_date`/`_strip_cdata`/`_parse_rss`/`fetch_one_rss`/`fetch_all_rss`
- 热度：`_has_cjk`/`_clean_title`/`_hn_points`/`_reddit_points`/`_buzz`/`_age_hours`/`_time_decay`/`_composite_score`/`_trend_score`
- LLM：`_active_llm`/`_llm_success`/`_llm_failure`（故障转移状态机）/`_llm_classify_batch`/`enrich_with_llm`/`_translate_terms`/`explain_terms`（热词双语解释生成/优化，供 terms.refresh_words 的 term_explainer 回调）/`_LLMTransientError`（瞬态错误类）/`_strip_llm_title_suffix`（剥翻译标题尾部 `| 来源` 噪音）；`_llm_classify_batch` 的 payload 会经 `config.llm_reasoning_params` 给 GLM-5.2+ 附 `reasoning_effort`（默认 low 降思考强度），提示词已加防回显/非空/JSON-only 规则，keywords 抽取限高价值实体/概念（禁泛化词）
- 聚合：`_to_card`/`_fetch_dims_raw`/`_project_card`
- 后台：`_cross_proc_lock`/`_persist_to_history`/`_dims_refresh_once`/`_seconds_until_next_refresh_hour`/`_bg_dims_refresher`

### 模块级常量
`RSS_SOURCES`（17 源，`dims.py:155`）、`PER_SOURCE_LIMIT=6`、`DIMS_CACHE_TTL`、`DIMS_REFRESH_HOURS`、`LLM_BATCH=12`、`LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD`/`LLM_REASONING_EFFORT`（自 config 导入）、`_LLM_ACTIVE_IDX`/`_LLM_FAILS`（故障转移进程级状态）、`DIMENSIONS`（维度枚举，被 `/api/stream` 引用）。

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

## terms.py  （1500 行）— 词粒度聚合层（词维度重构，新增）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 42–148 | 词池规模控制 + words.json 文件缓存（复刻 dims.py） |
| 150–207 | SQLite `init_db`（155）/ `_conn`（207）：`terms` / `term_snapshots` 表 + WAL（幂等补列含 explain_zh/en/updated_at） |
| 209–463 | 关键词词典 `_LEXICON`（218）/ 通用热词停用词表 `_TERM_STOPWORDS`（320，低价值通用词过滤，如 "AI"/"llm"/"model"）/ 热词解释 `_EXPLANATIONS`（335）/ `_ALIAS`（434）/ `_ASCII_PATTERNS`（450，版本感知词边界） |
| 464–716 | `normalize_term`（464）/ `is_stopword`（488，通用热词停用判断）/ `extract_keywords_dict`（498，抽词剔除停用词）/ 标题关联匹配辅助（含 `_title_key` 584 标题归一化去重键） |
| 719–1205 | 词聚合 + 三榜打分 + 快照（`refresh_words` 719 / `_refresh_words_inner` 744，dims 刷新锁内调；停用词在 `_keyword_canons`（629）聚合入口与 HF 词（`_hf_canon` 710 后）两级剔除；top news 排序截断前按标题去重；**6.5 解释批次**（1076）：词池即词典——非静态词新词生成解释、存量解释 >24h 低频优化，`term_explainer` 回调驱动） |
| 1206–1409 | 读：`get_word_cards`（1206）/ `get_term_row`（1252）/ `get_term_explanation`（1268，静态词典 → terms 表 explain_* → 空串三级取词）/ `get_term_news`（1300，limit 截断前按标题去重，同标题转载只留 score 最高者） |
| 1411–1500 | `list_terms_for_sitemap`（1397）+ 历史回填 `backfill_history`（1413）+ CLI |

### 公开函数（被 app.py / dims.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 155 | 建 `terms`/`term_snapshots` 表 + WAL（失败 `_DB_OK=False`） |
| `normalize_term(s)` | 464 | 任意词形 → canonical 键（小写/别名/去复数） |
| `is_stopword(term)` | 488 | 通用热词停用判断：归一化后查 `_TERM_STOPWORDS`（低价值通用词，如 "AI"/"llm"） |
| `extract_keywords_dict(title)` | 498 | 词典匹配抽词（无 LLM key 降级 + 回填；命中停用词不返回） |
| `refresh_words(all_cards, model_cards, term_translator, term_explainer)` | 719 | 词池归并 + 热度/上升/新奇度打分 + 快照 + 写 words.json + 动态解释维护 |
| `get_word_cards(sort, lang, limit)` | 1206 | `/api/stream?view=words` 数据源（读 words.json，先完整排序再截取再投影） |
| `get_term_row(term)` | 1252 | 查 terms 主表（canonical 键） |
| `get_term_explanation(term, lang)` | 1268 | 热词解释三级取词：静态 `_EXPLANATIONS` → terms 表 explain_*（LLM 维护）→ 空串；详情页模板兜底 |
| `get_term_news(term, limit, lang)` | 1300 | 词 → 关联报道（canonical/别名 + 标题边界兜底；按归一化标题去重后按 hot 降序，hot 缺失回退 score，同 hot 按 published 降序，排序先于 limit 截断） |
| `list_terms_for_sitemap(limit)` | 1397 | sitemap 词表（热度降序） |
| `backfill_history(days, force)` | 1413 | 词典回填 keywords + 合成历史快照（幂等，--force 全量） |

### SQLite 表
`terms`（词主表：term/display/display_zh/origin/first_seen_at/total_mentions/hf_json/cur_hot/cur_rise/cur_novelty + 动态解释列 explain_zh/explain_en/explain_updated_at——词池即词典资产）、`term_snapshots`（(term,cycle) 周期快照支撑环比）。

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
