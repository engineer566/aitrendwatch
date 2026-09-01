# 模块函数索引

> 9 个 Python 模块的结构、函数签名、行号、职责。配合 [INDEX.md](../INDEX.md) 使用。
> 行号基于 dev 分支实读。`_` 前缀为模块内部函数，归组列出但不展开细节。

---

## app.py  （1689 行）— Flask 入口 + 路由 + 直连抓取

### 分区清单
| 行号范围 | 分区（`# ----------` 注释段） |
|----------|------------------------------|
| 55–85 | 通用配置（UA/HEADERS/TIMEOUT/CACHE_TTL/`_cache`） |
| 86–347 | SEO 辅助 + 词详情装配（`_explain_fallback`@104 解释模板兜底 / `_word_detail`@133） |
| 348–539 | 各数据源抓取函数（8 个 `fetch_*`） |
| 540–741 | 路由公共配置（`SOURCES`@543/`SOURCE_META`@554/region/ip 辅助） |
| 742–911 | 页面 + 词流路由（含语言参数、稳定分类计数） |
| 912–1018 | HuggingFace 独立排序页（`_hf_models_for`@924 / `/hf`@946 / `/api/hf`@975） |
| 1019–1322 | 全站搜索 v2 |
| 1323–1479 | SEO 路由（robots/sitemap/favicon/og-image） |
| 1480–1489 | 赞助位点击跳转 |
| 1490–1581 | 管理后台（admin_login/logout/home + sponsors CRUD） |
| 1582–1634 | 统一管理后台（流量监控 + 赞助位管理，Tab 切换） |
| 1635–1689 | 用户行为事件上报（埋点系统 v3）+ `__main__` 入口（1689） |

### 公开函数（被路由/外部调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `fetch_baidu()` | 349 | 百度热搜官方接口 |
| `fetch_bilibili()` | 371 | B站热门官方接口 |
| `fetch_toutiao()` | 391 | 今日头条热榜 |
| `fetch_hackernews()` | 408 | HN Firebase API（逐条拉取，慢） |
| `fetch_github()` | 436 | GitHub Trending HTML 抓取 |
| `fetch_zhihu()` | 466 | 知乎热榜（直连） |
| `fetch_douyin()` | 490 | 抖音热搜（直连） |
| `fetch_weibo()` | 512 | 微博热搜（需登录态，常失败） |
| `detect_region()` | 566 | Accept-Language → zh/global |
| `_client_ip()` | 586 | 取真实 IP（信任 X-Forwarded-For） |
| `_client_country(ip)` | 594 | 反代头优先 + GeoLite2 兜底 |
| `get_source(source)` | 608 | 带缓存单源抓取 |
| `get_source_timeout(source)` | 623 | 带硬性截止时间单源抓取 |
| 38 个 view 函数 | 见 [api_routes.md](api_routes.md) | 路由处理 |

### 模块级常量
`SOURCES`（source→fetcher 映射，`app.py:543`）、`SOURCE_META`（8 源元信息，`app.py:554`）、`WORD_STREAM_LIMIT=100`（`app.py:249`，2026-09-02 由 60 放宽，配合热窗新鲜度加权让今日热词稳定可见）、`SSR_INITIAL_LIMIT=20`（`app.py:267`）、`UA`/`HEADERS`/`TIMEOUT=5`/`SOURCE_DEADLINE=25`/`CACHE_TTL=300`。

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

## dims.py  （1611 行）— 维度事件层（RSS + 热度 + LLM）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 58–75 | LLM 配置（模型故障转移链） |
| 76–146 | 文件缓存（`cache/dims.json`） |
| 147–348 | RSS 源定义 `RSS_SOURCES`（36 源）+ RSS 解析 + 抓取（`fetch_all_rss`@329） |
| 345–654 | 社区热度增强（HN/Reddit/复合分/趋势分） |
| 656–1270 | LLM 批量打标（685–783 故障转移状态 + `_is_mixed_translation` 混杂检查；784–1098 `_llm_classify_batch`；1101–1141 `enrich_with_llm`；1142–1188 `_translate_terms`；1189–1269 `explain_terms` 热词解释生成/优化） |
| 1270–1458 | 顶层聚合（`get_dims`/`get_news_cards`/`_fetch_dims_raw`） |
| 1459–1611 | 后台预热线程 + 跨进程锁 + 定点刷新 |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_dims(dimension=None, lang="zh")` | 1370 | 维度热词分组（只读缓存）；`/api/dims` |
| `get_news_cards(lang="zh")` | 1404 | news 卡列表（读缓存 + 历史库）；`/api/stream` |
| `enrich_with_signals(items)` | 623 | 给事件卡加 HN/Reddit/复合分（公开，可外部调） |
| `start_background_dims_refresher()` | 1603 | 启动后台预热线程（app.py 启动时调） |

### 内部函数（按分区归组）
- 缓存：`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`
- RSS：`_norm_date`/`_strip_cdata`/`_parse_rss`/`fetch_one_rss`/`fetch_all_rss`
- 热度：`_has_cjk`/`_clean_title`/`_hn_points`/`_reddit_points`/`_buzz`/`_age_hours`/`_time_decay`/`_composite_score`/`_trend_score`
- LLM：`_active_llm`/`_llm_success`/`_llm_failure`（故障转移状态机）/`_llm_classify_batch`/`enrich_with_llm`/`_translate_terms`/`explain_terms`（热词双语解释生成/优化，供 terms.refresh_words 的 term_explainer 回调）/`_LLMTransientError`（瞬态错误类）/`_strip_llm_title_suffix`（剥翻译标题尾部 `| 来源` 噪音）/`_is_mixed_translation`（硬编码中英混杂检查，issue 11：中文翻译残留 CJK、或英文翻译 ASCII 字母占比 >60% 且长度 >15 → 该批按失败计换档）；`_llm_classify_batch` 的 payload 会经 `config.llm_reasoning_params` 给 GLM-5.2+ 附 `reasoning_effort`（默认 low 降思考强度），提示词已加防回显/非空/JSON-only/完整翻译禁中英混杂规则，keywords 抽取限高价值实体/概念（禁泛化词）；**需求 5**：LLM 抽词结果回填前过 `terms_mod.case_match_original` 硬编码大小写校验——关键词必须与原文大小写完全一致（命中原文表面形式取原文确切大小写，未命中保持 canonical）
- 聚合：`_to_card`/`_fetch_dims_raw`/`_project_card`
- 后台：`_cross_proc_lock`/`_persist_to_history`/`_dims_refresh_once`/`_seconds_until_next_refresh_hour`/`_bg_dims_refresher`

### 模块级常量
`RSS_SOURCES`（36 源，`dims.py:155`，含 4 个 Google News 关键词源：Anthropic/Meta AI/OpenClaw/Open Source AI）、`PER_SOURCE_LIMIT=6`、`DIMS_CACHE_TTL`、`DIMS_REFRESH_HOURS`、`LLM_BATCH=12`、`LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD`/`LLM_REASONING_EFFORT`（自 config 导入）、`_LLM_ACTIVE_IDX`/`_LLM_FAILS`（故障转移进程级状态）、`DIMENSIONS`（维度枚举，被 `/api/stream` 引用）。

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

## store.py  （819 行）— 赞助位/统计/GeoIP/用户行为事件 SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 27–78 | HTML 净化（`sanitize_banner_html`@32） |
| 79–190 | 初始化 + 连接（`init_db`@80 / `_conn`@168） |
| 191–313 | 赞助位 CRUD |
| 314–389 | 统计（PV/曝光/点击） |
| 390–421 | GeoLite2 离线地域查询 |
| 422–493 | 访问记录 + `monitor_stats` |
| 494–661 | 用户搜索记录（搜索功能 + 后台监控） |
| 662–801 | 通用用户行为事件（埋点系统 v3：`record_event`/`record_events_batch`/`event_stats`） |
| 802–819 | 降级回退 `_fallback_slots` |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 80 | 建表 + 开 WAL（失败 `_DB_OK=False`） |
| `list_slots(region, active_only)` | 192 | 列赞助位 |
| `get_slot(slot_id)` | 218 | 取单条 |
| `upsert_slot(data)` | 231 | 新建/更新 |
| `delete_slot(slot_id)` | 278 | 删除 |
| `toggle_slot(slot_id)` | 293 | 上下架切换 |
| `record_pageview()` | 315 | PV+1 |
| `record_impression(slot_id)` | 332 | 曝光+1 |
| `record_click(slot_id)` | 348 | 点击+1 |
| `stats_30d()` | 364 | 30 天统计 |
| `geoip_country(ip)` | 396 | GeoLite2 查国家码（无库返 Unknown） |
| `record_visit(ip, country, path)` | 423 | 写 visits 表（监控页数据源） |
| `monitor_stats(days=30)` | 445 | 监控页聚合（PV/UV/地域） |
| `record_event(event_type, event_data)` | 670 | 用户行为事件单条入库（`_VALID_EVENT_TYPES` 白名单） |
| `record_events_batch(events, ...)` | 709 | 批量事件入库（`/api/event` 批量兼容） |
| `event_stats(days=30)` | 750 | 事件量/类型分布统计（`/monitor/api/events`） |

### SQLite 表
`sponsor_slots`、`sponsor_stats`、`pageviews`、`visits`、`search_queries`、`search_clicks`、`user_events`（见 [data_flow.md](data_flow.md) §SQLite）。

---

## news_store.py  （433 行）— 事件卡历史库 SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 35–116 | 初始化 `init_db`@36 + `_migrate`@89（keywords 列 + 维度映射） |
| 117–322 | 写：`upsert_cards`@118（含 keywords + canonical 归一化 + **churn 防护**：降级子集不覆盖 LLM 抽取的丰富关键词）+ `_keywords_to_json`@271 / `_keyword_set`@297 |
| 323–433 | 读：`list_history_cards`@324/`count_history`@356/`search_history`@369/行投影 |

### 公开函数（被 dims.py / terms.py / app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 36 | 建 `news_cards` 表 + 索引 + WAL + 幂等迁移 |
| `_migrate(conn)` | 89 | 加 keywords 列；旧维度值 → 新 6 类映射 |
| `upsert_cards(cards)` | 118 | 刷新后 upsert 本轮全部 cards（含 keywords 落库 + churn 防护） |
| `list_history_cards(limit, include_inactive, days)` | 324 | 合并历史库扩大内容池 |
| `count_history()` | 356 | 历史条数 |
| `search_history(query, lang, limit)` | 369 | 历史库 LIKE 搜索（含 keywords 字段） |

### SQLite 表
`news_cards`（含 `keywords` JSON 列；见 [data_flow.md](data_flow.md) §SQLite）。

---

## terms.py  （1759 行）— 词粒度聚合层（词维度重构，新增）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 42–114 | 词池规模控制 + **热窗新鲜度加权**（`_hot_recency_weight`@52：≤1d ×3 / ≤3d ×1.5 / 更早 ×1.0，今日热词不被存量累计分埋没）+ 词卡身份/排序辅助（`_word_card_identity`@73 / `_dedupe_word_cards`@89 / `_sort_word_cards`@105） |
| 115–174 | words.json 文件缓存（`WORDS_CACHE_FILE`@116，复刻 dims.py） |
| 175–245 | SQLite `init_db`@176 / `_conn`@240：`terms` / `term_snapshots` 表 + WAL（幂等补列含 explain_zh/en/updated_at + **term_snapshots.win7_cnt**） |
| 246–349 | 关键词词典 `_LEXICON`@251 |
| 350–364 | 通用热词停用词表 `_TERM_STOPWORDS`@354（低价值通用词过滤，如 "AI"/"llm"/"model"） |
| 365–501 | 热词解释 `_EXPLANATIONS`@369 / `_ALIAS`@469 / `_ASCII_PATTERNS`@485（版本感知词边界） |
| 502–896 | 大写缩写 `_UPPER_ACRONYMS`@507（gpu/ui/glm 等统一大写）+ 归一化与抽词（`normalize_term`@555 / `is_stopword`@589 / `_ci_surface_in_text`@599 / `case_match_original`@617（需求 5：大小写不敏感定位关键词在原文中的确切片段，命中替换为原文大小写）/ `extract_keywords_dict`@645 / `_term_surfaces`@673 / `_title_key`@733 / `_compile_surface_patterns`@747 / `_title_matches_patterns`@762 / `_keyword_canons`@778 / `_news_row_canons`@798 / `_display_of`@816 / `_display_zh_of`@889） |
| 897–1430 | 词聚合 + 三榜打分 + 快照（`_match_hf_term`@898 / `_HF_SUFFIX_RE`@912 / `_hf_canon`@917 / `refresh_words`@926 / `_refresh_words_inner`@951；**rise 环比用近 7 天滑动窗口报道数 `win7_cnt` 口径**（2026-09-01：单刷新轮次 cur_cnt 环比会把「发布日已进池」的词——如 Openclaw 8-31 发布、9-1 轮 cur 从 2→1——误判为降温；改用窗口内报道数，语义＝近一周声量是否增长，`term_snapshots.win7_cnt` 列支撑）；停用词在 `_keyword_canons`（778）聚合入口与 HF 词（`_hf_canon` 917 后）两级剔除；top news 排序截断前按标题去重；**6.5 解释批次**（~1305）：词池即词典——非静态词新词生成解释、存量解释 >24h 低频优化，`term_explainer` 回调驱动） |
| 1431–1670 | 读：`get_word_cards`@1435 / `get_term_row`@1481 / `get_term_explanation`@1497（静态词典 → terms 表 explain_* → 空串三级取词）/ `get_term_news`@1529（limit 截断前按标题去重，同标题转载只留 score 最高者）/ `list_terms_for_sitemap`@1656 |
| 1671–1759 | 历史回填 `backfill_history`@1672 + CLI |

### 公开函数（被 app.py / dims.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 176 | 建 `terms`/`term_snapshots` 表 + WAL（失败 `_DB_OK=False`） |
| `normalize_term(s)` | 555 | 任意词形 → canonical 键（小写/别名/去复数/首尾 ASCII 标点归一/大写缩写校正），大小写无关 |
| `is_stopword(term)` | 589 | 通用热词停用判断：归一化后查 `_TERM_STOPWORDS`（低价值通用词，如 "AI"/"llm"） |
| `extract_keywords_dict(title)` | 645 | 词典匹配抽词（无 LLM key 降级 + 回填；命中停用词不返回；openclaw 等词典词可命中）；**需求 5**：返回与原文大小写一致的表面形式（canonical 词键经 `case_match_original` 对齐原文大小写，未命中保持 canonical），去重上限 3 |
| `case_match_original(keyword, text)` | 617 | 硬编码大小写校验（需求 5）：在原文中大小写不敏感查找关键词（含词典表面/空格变体），命中返回原文确切大小写片段，未命中保持原词；纯 CJK 原样返回；LLM/词典抽词收口 |
| `refresh_words(all_cards, model_cards, term_translator, term_explainer)` | 926 | 词池归并 + 热度/上升/新奇度打分 + 快照 + 写 words.json + 动态解释维护 |
| `get_word_cards(sort, lang, limit)` | 1435 | `/api/stream?view=words` 数据源（读 words.json，先完整排序再截取再投影） |
| `get_term_row(term)` | 1481 | 查 terms 主表（canonical 键） |
| `get_term_explanation(term, lang)` | 1497 | 热词解释三级取词：静态 `_EXPLANATIONS` → terms 表 explain_*（LLM 维护）→ 空串；详情页模板兜底 |
| `get_term_news(term, limit, lang)` | 1529 | 词 → 关联报道（canonical/别名 + 标题边界兜底；按归一化标题去重后按 hot 降序，hot 缺失回退 score，同 hot 按 published 降序，排序先于 limit 截断） |
| `list_terms_for_sitemap(limit)` | 1656 | sitemap 词表（热度降序） |
| `backfill_history(days, force)` | 1672 | 词典回填 keywords（同样产出原文大小写一致的表面形式）+ 合成历史快照（幂等，--force 全量） |

### SQLite 表
`terms`（词主表：term/display/display_zh/display_en/origin/first_seen_at/total_mentions/hf_json/cur_hot/cur_rise/cur_novelty + 动态解释列 explain_zh/explain_en/explain_updated_at——词池即词典资产；**display_en 在 LLM 翻译失败轮次保留旧值**）、`term_snapshots`（(term,cycle) 周期快照支撑环比）。

---

## stream_utils.py  （70 行）— 统一信息流口径辅助

`card_identity`、`dedupe_cards`、`dimension_members`、`dimension_counts`、`dimension_list` 为后端 `/api/stream` 与测试共用的卡片去重、维度成员和计数规则；不依赖外部库。

---

## text_utils.py  （78 行）— RSS 文本/URL 实体解码

`decode_html_entities` 对文本做有界双层解码，`decode_url_entities` 只解一层并拒绝危险 URL scheme；供 `dims.py`、`news_store.py`、`terms.py` 统一处理历史缓存与新抓取数据。

---

## version.py  （23 行）— 版本号
- `_read_version()` (10) 读 `VERSION` 文件；`__version__` (22) / `version` 别名。
- 单一真相源：同目录 `VERSION` 文件（当前 `1.8.0`）。
