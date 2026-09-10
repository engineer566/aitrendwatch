# 模块函数索引

> 9 个 Python 模块的结构、函数签名、行号、职责。配合 [INDEX.md](../INDEX.md) 使用。
> 行号基于 dev 分支实读。`_` 前缀为模块内部函数，归组列出但不展开细节。

---

## app.py  （1829 行）— Flask 入口 + 路由 + 直连抓取

### 分区清单
| 行号范围 | 分区（`# ----------` 注释段） |
|----------|------------------------------|
| 55–104 | 通用配置（UA/HEADERS/TIMEOUT/CACHE_TTL/`_cache`/`_cached`/`_set_cache`/`_detail_cached`/`_detail_set_cache`） |
| 105–357 | SEO 辅助 + 词详情装配（`_explain_fallback`@106 解释模板兜底 / `_word_detail`@135——2026-09-05 P2：返回 dict 顶层附 `trend` 近 7 天活跃度序列；2026-09-05：`_hf_live` 内 community 按页面语言分流）+ stream/SSR 辅助（`_stream_number`@262 / `_initial_terms_for_ssr`@280 / `_initial_dimension_meta_for_ssr`@299）+ `SITE_PRIVACY_UPDATED`@356 |
| 361–~577 | 各数据源抓取函数（8 个 `fetch_*`：baidu@361/bilibili@383/toutiao@403/hackernews@420/github@448/zhihu@478/douyin@502/weibo@524） |
| ~580–~664 | 路由公共配置（`SOURCES`/`SOURCE_META`/region/ip 辅助：detect_region@578/_client_ip@598/_client_country@606/_rate_limit_deny@620（2026-09-07 P1 限流 429 响应）/get_source@628/get_source_timeout@643） |
| 665–955 | 页面 + 词流路由（`index`@677 含 hreflang 传参 / `term_detail`@776 含 **indexable 可索引门槛** + hreflang + 趋势上下文 / `terms`@847 / `privacy`@862 + `/privacy-policy`@879 301（2026-09-07 P1）/ 404@885 / **500@899（2026-09-07 P2）** / `api_dims`@925 / `api_stream`@934） |
| ~1000–~1100 | HuggingFace 独立排序页（`_hf_models_for`@1016——**community 按页面语言分流（2026-09-05）：zh 知乎/B站/GitHub（中文名），en YouTube/Reddit/X/GitHub** / `hf_page`@1041 含 hreflang / `api_hf`@1077） |
| ~1103–~1446 | 单词聚合 + 全站搜索 v2（`api_word`@1103 / `health`@1117 / `search_page`@1329 / `api_search_suggest`@1375 / `api_search_click`@1394 / `api_search`@1412） |
| ~1447–~1608 | SEO 路由（`robots`@1447 / `sitemap`@1467——**主语言 en：只交 `?lang=en` 变体 + 达标词；2026-09-07 追加 /privacy** / favicon 三件套 / `og_image`@1545） |
| ~1609–~1637 | 赞助位点击跳转 `sponsor_click`@1609 + `admin_required`@1619 |
| ~1638–~1718 | 管理后台（`admin_login`@1638——**2026-09-07 P1 POST 按 IP 限流** /logout@1662/home@1669 + sponsors list@1676/CRUD@1684-1703/stats@1712） |
| ~1719–~1770 | 统一管理后台（`monitor`@1719 + `monitor/api*`@1725-1752） |
| ~1771–1829 | 用户行为事件上报（`api_event`@1771 埋点 v3——**2026-09-07 P1 按 IP 限流** + `monitor_events_api`@1814）+ `__main__` 入口 |

### 公开函数（被路由/外部调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `fetch_baidu()` | 361 | 百度热搜官方接口 |
| `fetch_bilibili()` | 383 | B站热门官方接口 |
| `fetch_toutiao()` | 403 | 今日头条热榜 |
| `fetch_hackernews()` | 420 | HN Firebase API（逐条拉取，慢） |
| `fetch_github()` | 448 | GitHub Trending HTML 抓取 |
| `fetch_zhihu()` | 478 | 知乎热榜（直连） |
| `fetch_douyin()` | 502 | 抖音热搜（直连） |
| `fetch_weibo()` | 524 | 微博热搜（需登录态，常失败） |
| `detect_region()` | 578 | Accept-Language → zh/global |
| `_client_ip()` | 598 | 取真实 IP（信任 X-Forwarded-For） |
| `_client_country(ip)` | 606 | 反代头优先 + GeoLite2 兜底 |
| `_rate_limit_deny(retry_after)` | 620 | 限流 429 响应（JSON + Retry-After，2026-09-07 P1） |
| `get_source(source)` | 628 | 带缓存单源抓取 |
| `get_source_timeout(source)` | 643 | 带硬性截止时间单源抓取 |
| 39 个 view 函数 + 404/500 | 见 [api_routes.md](api_routes.md) | 路由处理 |

### 模块级常量
`SOURCES`（source→fetcher 映射，`app.py:552`）、`SOURCE_META`（8 源元信息，`app.py:563`）、`WORD_STREAM_LIMIT=100`（`app.py:258`，2026-09-02 由 60 放宽，配合热窗新鲜度加权让今日热词稳定可见）、`SSR_INITIAL_LIMIT=20`（`app.py:276`）、`UA`/`HEADERS`/`TIMEOUT=5`/`SOURCE_DEADLINE=25`/`CACHE_TTL=300`。

---

## config.py  （197 行）— 配置集中地

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 13–16 | Flask 会话签名 `SECRET_KEY` |
| 18–20 | 管理后台令牌 `ADMIN_TOKEN` |
| 22–27 | 站点信息 `SITE_NAME`/`BASE_URL`/`CONTACT_EMAIL` |
| 29–50 | 数据存储路径 + GeoIP + 缓存目录 |
| 52–134 | LLM 提供方（模型故障转移链 `LLM_CHAIN`@67/`LLM_FAILOVER_THRESHOLD`@71/`LLM_CYCLE_ESCAPE`@75 + **质量/可用性分离熔断 `LLM_QUALITY_FAILOVER_THRESHOLD`@83/`LLM_QUALITY_CYCLE_ESCAPE`@85 + 坏条目二次提示轮数 `LLM_REPAIR_ROUNDS`@90（2026-09-03）** + 思考强度 `LLM_REASONING_EFFORT`@98 + `llm_endpoint()`@111/`llm_reasoning_params()`@119；链每轮刷新复位回链首——DeepSeek 只做当轮逃生舱） |
| 135–139 | dims 定点预热 `DIMS_REFRESH_HOURS`@136 |
| 140–147 | 分析开关 |
| 148–158 | **公开端点限流（2026-09-07 P1）**：`EVENT_RATE_LIMIT`@153=300/分/IP（/api/event）、`LOGIN_RATE_LIMIT`@156=5/15分/IP（/admin/login，窗口 900s），进程内 ratelimit.py 固定窗口计数 |
| 159–178 | SEO 开关 |
| 179–188 | 第三方广告（AdSense/百度联盟） |
| 189–192 | 赞助位展示 |
| 193–197 | `ensure_data_dir()` |

### 公开函数
| 函数 | 行号 | 职责 |
|------|------|------|
| `_as_bool(v, default)` | 141 | 字符串→布尔 |
| `ensure_data_dir()` | 192 | 建 `DATA_DIR`（容器 /app/data，本地 ./data） |
| `llm_endpoint(model)` | 111 | 模型 ID → (url, api_key)：deepseek-* → DeepSeek，其余 → 智谱 BigModel |
| `llm_reasoning_params(model)` | 119 | 模型 ID → 思考强度参数 dict：glm-5.2+ 返回 `{"reasoning_effort": ...}`，glm-4.7/deepseek 返回 {}（不传未知参数） |

### 关键常量
`SECRET_KEY`、`ADMIN_TOKEN`（未设→admin 路由 404 隐身）、`DB_PATH`/`NEWS_DB_PATH`、`GEOIP_DB_PATH`、`CACHE_DIR`、`CONTACT_EMAIL`（站点联系/DMCA 统一入口，`/terms`+`/privacy` 共用，未配→占位文案）、`LLM_CHAIN`（默认 `glm-4.7-flash,glm-5.3-flash,deepseek-v4-flash`）、`LLM_FAILOVER_THRESHOLD=3`、`LLM_CYCLE_ESCAPE=4`、`LLM_QUALITY_FAILOVER_THRESHOLD=6`/`LLM_QUALITY_CYCLE_ESCAPE=12`/`LLM_REPAIR_ROUNDS=2`（2026-09-03：质量失败与 provider 故障分离 + 坏条目二次提示轮数）、`LLM_REASONING_EFFORT=low`（可选 low/high/max，仅 glm-5.2+ 生效）、`DEEPSEEK_API_KEY`/`DEEPSEEK_URL`、`GLM_API_KEY`/`GLM_URL`（智谱 BigModel 免费档，高峰 429/1305 过载）、`DIMS_REFRESH_HOURS=(1,7,13,19)`、`ANALYTICS_ENABLED`、`EVENT_RATE_LIMIT=300`/`LOGIN_RATE_LIMIT=5`（2026-09-07 P1 公开端点限流，env 可覆盖）、`SEO_ENABLED`/`SITEMAP_MAX_URLS`/`TERM_DETAIL_CACHE_TTL=1800`、`ADSENSE_ENABLED`/`ADSENSE_CLIENT`、`BAIDU_ADS_ENABLED`/`BAIDU_ADS_CPRO_ID`、`INLINE_SLOT_EVERY_N=8`、`NEWS_HISTORY_LIMIT=400`/`NEWS_HISTORY_DAYS=30`。

---

## dims.py  （1956 行）— 维度事件层（RSS + 热度 + LLM）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 63–80 | LLM 配置（模型故障转移链） |
| 81–159 | 文件缓存（`cache/dims.json` 81–159；**news 内容池 `NEWS_STREAM_CACHE_FILE`@93 = cache/news.json，2026-09-07 P0**） |
| 160–290 | **news 视图内容池（P0 性能修复）**：`_load_news_pool_file`@167（mtime 感知读盘）/`_build_news_pool_neutral`@196（dims.json 当轮 + news.db 历史库现装配，写盘与冷启动回退共用同一实现）/`_write_news_pool_file`@250（刷新锁内写 tmp + os.replace）/`_news_pool_cards`@278（池文件优先、缺失回退现装配，请求路径零 DB 读） |
| 291–487 | RSS 源定义 `RSS_SOURCES`@299（36 源）+ RSS 解析 + 抓取（`fetch_all_rss`@473） |
| 488–797 | 社区热度增强（HN/Reddit/复合分/趋势分） |
| 798–1614 | LLM 批量打标（807–835 异常类：`_LLMTransientError`@807 / `_LLMAccountRateLimit`@811 / `_LLMQualityError`@816（2026-09-03 质量失败类）；836–1041 故障转移状态机：`_llm_quality_failure`@906（质量/可用性分离高阈值熔断：连续 6/周期累计 12 才换档）+ `_llm_cycle_reset`@957（每轮刷新起始复位链首）+ `_llm_apply_output`@1004 逐条校验+回填（好条目保留、坏条目记 `_llm_fail` 原因）；1099–1287 **`_USER_PREFIX`@1116 user 前缀模块常量**（keywords/翻译规则全在这，稳定前缀缓存单元；2026-09-04 需求 4：中文标题里的公司/机构/产品专名保持中文原词，仅标题原文含官方英文名/英文拼写时才用英文，禁拼音化/自译）+ `_llm_classify_batch`@1149（2026-09-02 逐条校验 + 2026-09-03 坏条目「二次提示」修正 repair pass（LLM_REPAIR_ROUNDS 轮，带失败原因喂回当前档）+ 质量失败不快速换档、429/5xx/402 才换档）；1288–1400 `_item_missing_llm_out`@1388 + `enrich_with_llm`@1401（质量失败不重试、provider 故障才收进末尾重试）；1401–1614 **`_TRANSLATE_SYS_MSG`@1465**（热词翻译 system 提示词常量；2026-09-04 需求 4：公司/机构/产品专名必须用官方英文名（如 创通联达→Thundercomm、中科创达→ThunderSoft）、无官方英文名的中文专名保留中文原词、禁拼音音译/自造英文（反例 Qujing Tech 钉在提示词里））+ `_translate_terms`@1475 + `explain_terms`@1525 热词解释生成/优化） |
| 1615–1794 | 顶层聚合 + 逐条流去重（`_to_card`@1616 / `_fetch_dims_raw`@1651 / `_project_card`@1691 / `get_dims`@1716 / `get_news_cards`@1750——2026-09-04 需求 1：卡 id 按 `text_utils.normalize_url_key` 归一到与历史库存储键同口径（实体解码+去片段+去 utm_*），返回前经 `_dedupe_news_titles`@1770 标题级去重（同口径归一标题键，title_zh→title_en→title，同标题镜像只留首条）；words 视图不经过这里，不受影响；**2026-09-07 P0**：get_news_cards 改读 `cache/news.json` 内容池（见 160–290 分区），不再每次请求扫 news.db 现装配——修复 `/api/stream?view=news` 线上 7-21s |
| 1795–1956 | 后台预热线程 + 跨进程锁 + 定点刷新（`_cross_proc_lock`@1807 / `_persist_to_history`@1825 / `_dims_refresh_once`@1844 起始 `_llm_cycle_reset`、尾部 `_write_news_pool_file`（2026-09-07 P0）/ `_seconds_until_next_refresh_hour`@1901 / `_bg_dims_refresher`@1925 / `start_background_dims_refresher`@1948） |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_dims(dimension=None, lang="zh")` | 1716 | 维度热词分组（只读缓存）；`/api/dims` |
| `get_news_cards(lang="zh")` | 1750 | news 卡列表（读 `cache/news.json` 内容池 + 语言投影；**2026-09-07 P0：请求路径零 DB 读**，池缺失回退现装配；池内已做 id url 归一 + `_dedupe_news_titles` 标题级去重）；`/api/stream?view=news` |
| `enrich_with_signals(items)` | 767 | 给事件卡加 HN/Reddit/复合分（公开，可外部调） |
| `start_background_dims_refresher()` | 1948 | 启动后台预热线程（app.py 启动时调） |

### 内部函数（按分区归组）
- 缓存：`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`（dims.json）+ news 内容池 `_load_news_pool_file`/`_build_news_pool_neutral`/`_write_news_pool_file`/`_news_pool_cards`（cache/news.json，2026-09-07 P0）
- RSS：`_norm_date`/`_strip_cdata`/`_parse_rss`/`fetch_one_rss`/`fetch_all_rss`
- 热度：`_has_cjk`/`_clean_title`/`_hn_points`/`_reddit_points`/`_buzz`/`_age_hours`/`_time_decay`/`_composite_score`/`_trend_score`
- LLM：`_active_llm`/`_llm_success`/`_llm_failure`（可用性失败状态机）/`_llm_quality_failure`（质量失败高阈值熔断）/`_llm_skip_provider`/`_llm_cycle_reset`（每轮刷新复位回链首）/`_llm_classify_batch`/`_llm_apply_output`（逐条校验回填）/`enrich_with_llm`/`_translate_terms`/`explain_terms`（热词双语解释生成/优化，供 terms.refresh_words 的 term_explainer 回调）/`_LLMTransientError`/`_LLMAccountRateLimit`/`_LLMQualityError`（异常类）/`_strip_llm_title_suffix`（剥翻译标题尾部 `| 来源` 噪音）/`_is_mixed_translation`（硬编码中英混杂检查，issue 11：中文翻译残留 CJK、或英文翻译 ASCII 字母占比 >60% 且长度 >15 → 该条按坏计）；`_llm_classify_batch` 的 payload 会经 `config.llm_reasoning_params` 给 GLM-5.2+ 附 `reasoning_effort`（默认 low 降思考强度），提示词已加防回显/非空/JSON-only/完整翻译禁中英混杂规则，keywords 抽取限高价值实体/概念（禁泛化词）；**需求 5**：LLM 抽词结果回填前过 `terms_mod.case_match_original` 硬编码大小写校验——关键词必须与原文大小写完全一致；**2026-09-02（DeepSeek 用量事故修复）**：逐条校验回填 + 每轮复位链首 + HTTP 402 归账户级限流；**2026-09-03（DeepSeek 费用仍异常修复）**：质量失败（混杂/缺翻译/JSON）与 provider 故障（429/5xx/超时）分离计数——零星 1-2/6 混杂不再快速换档（换档救不了质量，只把账单抬到 3 倍价档），坏条目经「二次提示」（带失败原因喂回当前档，`LLM_REPAIR_ROUNDS` 轮）修正，GLM-5.3 只要在线就整轮主扛、DeepSeek 只兜底；**需求 4（2026-09-04，中文公司名英译优化）**：`_USER_PREFIX`（@977）keywords 规则要求中文标题里的公司/机构/产品专名保持中文原词（仅当标题原文含官方英文名/英文拼写时才用英文，严禁拼音化/自译成英文关键词）；`_translate_terms` 的 system 提示词（`_TRANSLATE_SYS_MSG`@1326）要求公司/机构/产品专名必须用官方英文名（如 创通联达→Thundercomm、中科创达→ThunderSoft）、无官方英文名的中文专名保留中文原词（禁拼音音译/自造英文，反例 Qujing Tech 钉在提示词里）——与 terms `_COMPANY_EN_GLOSSARY`（terms.py:957）词典优先配合，词典未收录词才走 LLM 兜底
- 聚合：`_to_card`/`_fetch_dims_raw`/`_project_card`
- 后台：`_cross_proc_lock`/`_persist_to_history`/`_dims_refresh_once`（每轮起始 `_llm_cycle_reset`，落库后写 news 内容池）/`_seconds_until_next_refresh_hour`/`_bg_dims_refresher`

### 模块级常量
`RSS_SOURCES`（36 源，`dims.py:299`，含 4 个 Google News 关键词源：Anthropic/Meta AI/OpenClaw/Open Source AI）、`NEWS_STREAM_CACHE_FILE`（`dims.py:93`，news 内容池文件 cache/news.json，2026-09-07 P0）、`PER_SOURCE_LIMIT=6`、`DIMS_CACHE_TTL`、`DIMS_REFRESH_HOURS`、`LLM_BATCH=12`、`LLM_CHAIN`/`LLM_FAILOVER_THRESHOLD`/`LLM_QUALITY_FAILOVER_THRESHOLD`/`LLM_QUALITY_CYCLE_ESCAPE`/`LLM_REPAIR_ROUNDS`（自 config 导入）、`_LLM_ACTIVE_IDX`/`_LLM_FAILS`/`_LLM_CYCLE_FAILS`/`_LLM_QUALITY_FAILS`/`_LLM_QUALITY_CYCLE_FAILS`（故障转移进程级状态：可用性与质量分开计数，每轮 `_llm_cycle_reset` 复位）、`DIMENSIONS`（维度枚举，被 `/api/stream` 引用）、`_USER_PREFIX`（分类/抽词 user 前缀常量 @1115，逐字稳定构成 LLM 缓存前缀单元）、`_TRANSLATE_SYS_MSG`（热词翻译 system 提示词常量 @1465）。

---

## tracker.py  （621 行）— 热词追踪层（HF + arXiv）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 34–109 | 文件缓存（`cache/terms.json`）+ 内存缓存 |
| 110–192 | HF 模型热词抓取 + 社区链接语言分流（`community_links`@152——2026-09-05：zh 知乎/B站/GitHub（中文名），en YouTube/Reddit/X/GitHub / `localize_model_cards`@173 读取时按 lang 投影） |
| 193–354 | arXiv 论文检索（限速 + 检索式构造） |
| 355–505 | 顶层聚合（`get_terms`/`get_model_cards`） |
| 506–621 | 后台预热线程 + 跨进程锁 + 单词详情 |

### 公开函数（被 app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `get_terms(sort="trending")` | 427 | 热词榜（trending/top 两种 sort，读缓存）；`/api/trending` `/api/top` |
| `get_model_cards(lang="zh")` | 452 | model 卡列表（读缓存，community 按 lang 分流）；`/api/stream` |
| `get_term_detail(term_name)` | 582 | 单热词详情：live HF + 同步 arXiv（~1-4s）；`/api/term/` `/term/` |
| `start_background_refresher()` | 571 | 启动后台预热线程（app.py 启动时调） |

### 内部函数
- 缓存：`_cached`/`_set_cache`/`_load_file_cache`/`_save_file_cache`/`_file_cache_get`/`_file_cache_set`
- HF：`fetch_hf_models`/`_model_to_term`/`community_links`（按语言分流）/`localize_model_cards`（读取时投影）
- arXiv：`_base_model_key`/`_dedupe_by_base_model`/`_arxiv_throttle`/`_search_query_for`/`search_arxiv_papers`/`enrich_with_papers`
- 聚合：`_fetch_terms_raw`/`_fetch_terms_quick`
- 后台：`_cross_proc_lock`/`_refresh_once`/`_bg_refresher`

### 模块级常量
`HF_BASE="https://hf-mirror.com"`（官方 HF 不可达走镜像）、`ARXIV_API`、`ARXIV_GAP=3.0`（限速）、`ARXIV_ENRICH_LIMIT=8`（只检索前 N 热词）、`UA`/`HEADERS`/`TIMEOUT=8`。

---

## store.py  （826 行）— 赞助位/统计/GeoIP/用户行为事件 SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 27–78 | HTML 净化（`sanitize_banner_html`@32） |
| 79–190 | 初始化 + 连接（`init_db`@80 / `_conn`@168） |
| 191–313 | 赞助位 CRUD |
| 314–389 | 统计（PV/曝光/点击） |
| 390–421 | GeoLite2 离线地域查询 |
| 422–493 | 访问记录 + `monitor_stats` |
| 494–668 | 用户搜索记录（搜索功能 + 后台监控） |
| 669–808 | 通用用户行为事件（埋点系统 v3：`record_event`/`record_events_batch`/`event_stats`） |
| 809–826 | 降级回退 `_fallback_slots` |

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
| `record_event(event_type, event_data)` | 677 | 用户行为事件单条入库（`_VALID_EVENT_TYPES` 白名单） |
| `record_events_batch(events, ...)` | 716 | 批量事件入库（`/api/event` 批量兼容） |
| `event_stats(days=30)` | 757 | 事件量/类型分布统计（`/monitor/api/events`） |

### SQLite 表
`sponsor_slots`、`sponsor_stats`、`pageviews`、`visits`、`search_queries`、`search_clicks`、`user_events`（见 [data_flow.md](data_flow.md) §SQLite）。

---

## news_store.py  （574 行）— 事件卡历史库 SQLite

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 45–127 | 初始化 `init_db`@182（建表 + 索引 + WAL + 幂等迁移 + **需求 1 自愈 `_heal_dup_urls`**）+ `_migrate`@100（keywords 列 + 维度映射） |
| 128–464 | 写：`upsert_cards`@129——**2026-09-04 需求 1（同一词条下相同的报道）**：url 写库前经 `text_utils.normalize_url_key` 归一（实体单层解码 + 去 #fragment + 去 utm_*），批次内同归一键只留一份（`_merge_rows`@389：保留带 keywords/score 高者 + keywords 并集）；churn 防护（降级子集不覆盖 LLM 抽取的丰富关键词）；upsert 后 `_heal_dup_urls`@406 自愈存量孪生行（同归一键多行保留一行：优先归一键行/数据更全者，keywords 并集、first_seen_at 取更早，其余行**删除**——terms 扫描不过滤 active，删除才根治计数膨胀与双显；组内仅一行且非归一键则重写为归一键）+ `_keywords_to_json`@298 / `_keyword_set`@324 / `_union_keyword_json`@375 |
| 465–574 | 读：`list_history_cards`@465/`count_history`@497/`search_history`@510/行投影 `_row_to_card`@540 |

### 公开函数（被 dims.py / terms.py / app.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 44 | 建 `news_cards` 表 + 索引 + WAL + 幂等迁移 + 启动自愈孪生行 |
| `_migrate(conn)` | 100 | 加 keywords 列；旧维度值 → 新 6 类映射 |
| `upsert_cards(cards)` | 129 | 刷新后 upsert 本轮全部 cards（url 归一 + 批次内去重 + keywords 落库 + churn 防护 + 自愈存量孪生行） |
| `list_history_cards(limit, include_inactive, days)` | 465 | 合并历史库扩大内容池 |
| `count_history()` | 497 | 历史条数 |
| `search_history(query, lang, limit)` | 510 | 历史库 LIKE 搜索（含 keywords 字段） |

### SQLite 表
`news_cards`（url 主键为**归一键**（`normalize_url_key`：实体解码+去片段+去 utm_*）；含 `keywords` JSON 列；见 [data_flow.md](data_flow.md) §SQLite）。

---

## terms.py  （2336 行）— 词粒度聚合层（词维度重构，新增）

### 分区清单
| 行号范围 | 分区 |
|----------|------|
| 43–120 | 词池规模控制 + **热窗新鲜度加权**（`_hot_recency_weight`@58：≤1d ×3 / ≤3d ×1.5 / 更早 ×1.0，今日热词不被存量累计分埋没）+ 词卡身份/排序辅助（`_word_card_identity`@79 / `_dedupe_word_cards`@95 / `_sort_word_cards`@111） |
| 121–180 | words.json 文件缓存（`WORDS_CACHE_FILE`@122，复刻 dims.py） |
| 181–251 | SQLite `init_db`@182 / `_conn`@246：`terms` / `term_snapshots` 表 + WAL（幂等补列含 explain_zh/en/updated_at + **term_snapshots.win7_cnt**） |
| 252–361 | 关键词词典 `_LEXICON`@257（2026-09-10：收录 `apple`（表面 apple/苹果）——跨语言孪生归并依赖词典人工收录，此前未收录导致「苹果」与 apple 各成一词、生产榜两个 Apple） |
| 362–376 | 通用热词停用词表 `_TERM_STOPWORDS`@366（低价值通用词过滤，如 "AI"/"llm"/"model"） |
| 377–520 | 热词解释 `_EXPLANATIONS`@381 / `_ALIAS`@482（2026-09-10：手工别名 可折叠iphone/可折叠-iphone/折叠-iphone → 折叠iphone，同概念中文措辞归并） / `_ASCII_PATTERNS`@504（版本感知词边界） |
| 521–1164 | 大写缩写 `_UPPER_ACRONYMS`@526（gpu/ui/glm 等统一大写）+ 归一化与抽词（`_ASCII_PUNCT`@519 / `normalize_term`@574（**需求 2：词典治理的紧凑孪生折叠**——ASCII canonical 去 '-' 的 compact 若是治理词（_LEXICON 键/缩写表值/_LEXICON_DISPLAY 键/_OVERRIDES 键，如 huggingface）则折叠，'hugging-face'→'huggingface'；非治理词紧凑孪生 ai-agent/aiagent 不动）/ `is_stopword`@621 / `_ci_surface_in_text`@631 / `case_match_original`@649（需求 5：命中返回原文确切大小写片段）/ `extract_keywords_dict`@694 / `_term_surfaces`@722（**需求 2：补 '-'/空格/紧凑分隔变体表面**）/ `_title_matches_term`@775 / `_title_key`@802（**2026-09-04 需求 1 加严**：委托 `text_utils.normalized_title_key`——剥常见全角/半角标点与引号变体、`·/・` 按空白等价、残余空白全去除；连字符等有语义字符保留不误压真实不同标题；空/纯标点返回 None）/ `_compile_surface_patterns`@817 / `_title_matches_patterns`@832 / `_keyword_canons`@848 / `_news_row_canons`@868）+ display 名决策与中文公司词典（模块级展示名表 `_OVERRIDES`@889 / `_LEXICON_DISPLAY`@918——原为 `_display_of` 局部，需求5 改进上提供权威判定共用；**936–1164 需求 4：中文公司/机构官方英名词典区**——`_COMPANY_EN_GLOSSARY`@957（37 键，CJK display → 官方英文名 display_en 确定性映射）+ `_company_glossary_en`@1005（display/display_zh 双形态精确键查）；`_display_of`@1022 / `_is_dictionary_governed`@1056 / **需求 2 归并辅助 `_compact_group_key`@1070 / `_merge_old_rows`@1086 / `_merge_agg_rows`@1113** / `_surface_upper_trusted`@1139 / `_display_zh_of`@1157）——**需求5 改进**：词典外词 display 优先原文表面形态（WorkBuddy 不被 capitalize 美化抹成 Workbuddy；词典权威词 OpenAI/Hugging Face 等仍由词典决定，不被标题表面偶然大小写污染） |
| 1165–1918 | 词聚合 + 三榜打分 + 快照（`_match_hf_term`@1166 / `_HF_SUFFIX_RE`@1180 / `_hf_canon`@1185 / `refresh_words`@1194 / `_refresh_words_inner`@1219；**rise 环比用近 7 天滑动窗口报道数 `win7_cnt` 口径**（2026-09-01：单刷新轮次 cur_cnt 环比会把「发布日已进池」的词——如 Openclaw 8-31 发布、9-1 轮 cur 从 2→1——误判为降温；改用窗口内报道数，语义＝近一周声量是否增长，`term_snapshots.win7_cnt` 列支撑）；停用词在 `_keyword_canons`（848）聚合入口与 HF 词（`_hf_canon` 1185 后）两级剔除；top news 排序截断前按标题去重（**2026-09-04 需求 1 起去重键剥标点加严**，且当轮 `cur_urls`/`cur_signal_by_url` 按 `normalize_url_key` 归一到存储键同口径，防止孪生 url 漏计/双计）；**display_en 增量翻译（5.6 @1491）**：`TRANSLATE_BATCH_MAX_WORDS`@55，2026-09-02 缺 en 词优先/预算内回译（不再每轮全量重译）；**2026-09-04 需求 4**：5.6 段先做词典预写——display/display_zh 命中 `_COMPANY_EN_GLOSSARY`@957 的公司专名确定性写官方英文名、不进 LLM 翻译批次（不受限流/预算影响，存量拼音脏值随刷新回归；判定独立于 term_translator，无 key 降级环境同样生效），未收录中文词才走 LLM 兜底；**6.5 解释批次**（~1802）：词池即词典——非静态词新词生成解释、存量解释 >24h 低频优化，`term_explainer` 回调驱动；**需求5 改进（display 原文大小写）**：第 2 步收集当轮卡 keywords 表面（`cur_kw_surfaces`），第 6 步词典外词（`_is_dictionary_governed` 判定）display 优先表面形态（全大写标题党形态不入选）；词典权威词（OpenAI/Hugging Face/GLM/xAI）仍由词典决定展示——顺带修正存量脏 display（SaaS/DevOps 曾顶成 Saas/Devops，随刷新回归）；**需求 2（4.5 孪生归并@1389 + 4.7 旧行视图@1475 + 第 6 步残留行清理@1576）**：按「去 '-' 紧凑形式」分组选代表键（治理 > 旧词池 > mentions > 字典序），聚合/HF/表面全并——ai-agent/aiagent 不再同展示名两行；旧 terms 表孪生/折叠残留行删除、term_snapshots 迁移（同 cycle 相加）、first_seen_at 取组内最早、解释列随归并保留） |
| 1919–2226 | 读：`get_word_cards`@1932 / `get_term_row`@1978 / `term_row_indexable`@1994（**2026-09-05 SEO P1**：词条可索引判定，sitemap 与详情页共用——origin hf/both 且 hf_json 非空放行，否则需 total_mentions≥`TERM_INDEX_MIN_NEWS` 且 cur_hot≥`TERM_INDEX_MIN_HOT`）/ `get_term_explanation`@2027（静态词典 → terms 表 explain_* → 空串三级取词）/ `get_term_news`@2059（limit 截断前按标题去重——需求 1 起去重键剥标点加严，全角/半角标点镜像标题同样只留 score 最高者；同标题转载不占 limit 位；keywords LIKE 候选覆盖孪生分隔拼写，Python 侧权威归一校验）/ `get_term_trend`@2186（**2026-09-05 SEO P2**：term_snapshots 按日聚合近 7 天活跃度——同日取末 cycle，<2 点或全 0 返回 []） |
| 2227–2349 | 读：`list_terms_for_sitemap`@2236（**2026-09-05 P1**：按 `term_row_indexable` 过滤达标词后取前 limit，热度降序）+ 历史回填 `backfill_history`@2262 + CLI |

### 公开函数（被 app.py / dims.py 调用）
| 函数 | 行号 | 职责 |
|------|------|------|
| `init_db()` | 182 | 建 `terms`/`term_snapshots` 表 + WAL（失败 `_DB_OK=False`） |
| `normalize_term(s)` | 574 | 任意词形 → canonical 键（小写/别名/去复数/首尾 ASCII 标点归一/大写缩写校正），大小写无关；**需求 2**：词典治理的紧凑孪生折叠（ASCII canonical 去 '-' 的 compact 是治理词则返回 compact，'Hugging Face'/'Hugging-Face'/'HuggingFace' 全归 'huggingface'）；非治理词紧凑孪生（ai-agent/aiagent）不折叠 |
| `is_stopword(term)` | 621 | 通用热词停用判断：归一化后查 `_TERM_STOPWORDS`（低价值通用词，如 "AI"/"llm"） |
| `extract_keywords_dict(title)` | 694 | 词典匹配抽词（无 LLM key 降级 + 回填；命中停用词不返回；openclaw 等词典词可命中）；**需求 5**：返回与原文大小写一致的表面形式（canonical 词键经 `case_match_original` 对齐原文大小写，未命中保持 canonical），去重上限 3 |
| `case_match_original(keyword, text)` | 649 | 硬编码大小写校验（需求 5）：在原文中大小写不敏感查找关键词（含词典表面/空格变体），命中返回原文确切大小写片段，未命中保持原词；纯 CJK 原样返回；LLM/词典抽词收口 |
| `refresh_words(all_cards, model_cards, term_translator, term_explainer)` | 1194 | 词池归并 + 热度/上升/新奇度打分 + 快照 + 写 words.json + 动态解释维护（display_en 增量翻译 + 解释批次均带词数上限）；**需求5 改进**：词典外词 display 优先原文表面形态（当轮卡 keywords / top 标题命中片段，如 WorkBuddy），词典权威词仍由词典决定；**2026-09-04 需求 1**：当轮 url 按 `normalize_url_key` 归一后与存量行比对（cur_cnt/cur_signal 不漏计孪生行）；**需求 2**：4.5 分隔符孪生归并（去 '-' 紧凑分组 → 代表键：治理 > 旧词池 > mentions > 字典序，ai-agent/aiagent 归并单行）+ 第 6 步残留行清理（删孪生/折叠行、快照迁移、first_seen 取最早）——榜单无同词两行；**需求 4**：中文公司/机构专名 display_en 先查 `_COMPANY_EN_GLOSSARY`@957 确定性写官方英文名（不进 LLM 批次、不拼音化；存量拼音脏值随刷新回归），词典未收录中文词才走 term_translator 兜底 |
| `get_word_cards(sort, lang, limit)` | 1932 | `/api/stream?view=words` 数据源（读 words.json，先完整排序再截取再投影） |
| `get_term_row(term)` | 1978 | 查 terms 主表（canonical 键；'hugging-face'/'huggingface' 归一后同键） |
| `term_row_indexable(row)` | 1994 | **2026-09-05 SEO P1**：词条可索引判定——origin hf/both 且 hf_json 非空 → True；否则 `total_mentions >= TERM_INDEX_MIN_NEWS` 且 `cur_hot >= TERM_INDEX_MIN_HOT`；None/缺键 → False，永不抛 |
| `get_term_explanation(term, lang)` | 2027 | 热词解释三级取词：静态 `_EXPLANATIONS` → terms 表 explain_*（LLM 维护）→ 空串；详情页模板兜底 |
| `get_term_news(term, limit, lang)` | 2059 | 词 → 关联报道（canonical/别名 + 标题边界兜底；按归一化标题去重后按 hot 降序——2026-09-04 需求 1 起去重键剥标点加严，全角/半角标点差异镜像标题同样去重；hot 缺失回退 score，同 hot 按 published 降序，排序先于 limit 截断；LIKE 候选覆盖孪生分隔拼写） |
| `get_term_trend(term, days)` | 2186 | **2026-09-05 SEO P2**：term_snapshots 按日聚合近 7 天活跃度（同日取最后 cycle 行，升序返回 {date, win7_cnt, news_cnt}；<2 点或全 0 → []） |
| `list_terms_for_sitemap(limit)` | 2236 | sitemap 词表（**按 `term_row_indexable` 过滤达标词**后按热度降序取前 limit） |
| `backfill_history(days, force)` | 2262 | 词典回填 keywords（同样产出原文大小写一致的表面形式）+ 合成历史快照（幂等，--force 全量） |

### SQLite 表
`terms`（词主表：term/display/display_zh/display_en/origin/first_seen_at/total_mentions/hf_json/cur_hot/cur_rise/cur_novelty + 动态解释列 explain_zh/explain_en/explain_updated_at——词池即词典资产；**display_en：中文公司词由 `_COMPANY_EN_GLOSSARY`@957 词典确定性写入官方英文名优先，词典外词走 LLM 增量翻译，LLM 翻译失败轮次保留旧值**）、`term_snapshots`（(term,cycle) 周期快照支撑环比）。

---

## ratelimit.py  （87 行）— 进程内固定窗口限流（2026-09-07 P1，新）

纯 stdlib 的按 `(bucket, key)` 固定窗口计数限流，被 app.py 用于两个公开/半公开端点：
`/api/event`（埋点上报，`EVENT_RATE_LIMIT`/分/IP）与 `/admin/login`（登录尝试，
`LOGIN_RATE_LIMIT`/15 分/IP）；超限由 `app._rate_limit_deny` 返回 429 + Retry-After。

- 公开函数：`allow(bucket, key, limit, window)` → `(allowed, retry_after)`（`ratelimit.py:47`，`limit<=0`/空 key 恒放行）；`reset()`@84（测试用）。
- 线程安全（进程内锁）；多 worker 各自计数（单 IP 刷量已被收敛）。
- 内存有界 fail-open：全表 `_MAX_KEYS=4096`@26 满后新 key 放行，防伪造 X-Forwarded-For 打爆内存；过期条目惰性清理。
- 测试 `tests/test_rate_limit.py`（7 用例）。

---

## stream_utils.py  （70 行）— 统一信息流口径辅助

`card_identity`、`dedupe_cards`、`dimension_members`、`dimension_counts`、`dimension_list` 为后端 `/api/stream` 与测试共用的卡片去重、维度成员和计数规则；不依赖外部库。

---

## text_utils.py  （147 行）— RSS 文本/URL 实体解码 + 去重归一键

`decode_html_entities` 对文本做有界双层解码，`decode_url_entities` 只解一层并拒绝危险 URL scheme；**2026-09-04 需求 1 新增**：`normalize_url_key`（url 归一键：实体单层解码 + 去 #fragment + 去 `utm_*` 跟踪参数，仅对含 `://` 的真实 URL 生效；供 news_store 存储键/孪生行自愈、dims 逐条流 id、terms 当轮 url 比对共用同口径）与 `normalized_title_key`（标题归一化去重键：strip + casefold + 空白折叠 + 剥常见全角/半角标点引号 + `·/・` 按空白等价 + 残余空白全去除；空/纯标点返回 None；terms._title_key 与 dims 逐条流标题去重的唯一实现源）；供 `dims.py`、`news_store.py`、`terms.py`、`app.py` 统一处理历史缓存与新抓取数据。

---

## version.py  （23 行）— 版本号
- `_read_version()` (10) 读 `VERSION` 文件；`__version__` (22) / `version` 别名。
- 单一真相源：同目录 `VERSION` 文件（当前 `1.11.0`，2026-09-07 release：MVP P0~P2）。
