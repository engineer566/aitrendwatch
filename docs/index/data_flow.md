# 数据流与外部依赖索引

> 外部数据源、SQLite schema、缓存产物、环境变量。配合 [INDEX.md](../INDEX.md) 使用。

## 外部数据源

### RSS 源（36 个，`dims.py:155` `RSS_SOURCES`，其中 4 个 Google News 关键词源：Anthropic/Meta AI/OpenClaw/Open Source AI）

并发抓取（`fetch_all_rss` `dims.py:329`，8 worker），按 url 去重，每源取前 `PER_SOURCE_LIMIT=6` 条；RSS 标题使用有界双层实体解码，URL 只解一层 XML entity。

| 名称 | feed | region | default_dim | lang | 备注 |
|------|------|--------|-------------|------|------|
| OpenAI | openai.com/news/rss.xml | 国际 | 产品发布 | en | 厂商官方 |
| TechCrunch AI | techcrunch.com/.../ai/feed/ | 国际 | 行业动态 | en | 媒体 |
| HF Blog | hf-mirror.com/blog/feed.xml | 国际 | 模型发布 | en | Atom 格式 |
| arXiv cs.AI | export.arxiv.org/rss/cs.AI | 国际 | 研究论文 | en | |
| Microsoft AI | microsoft.com/en-us/ai/blog/rss/ | 国际 | 产品发布 | en | |
| DeepMind | blog.google/technology/ai/rss/ | 国际 | 研究论文 | en | URL 修正后可用 |
| NVIDIA | blogs.nvidia.com/feed/ | 国际 | 行业动态 | en | |
| Stability AI | stability.ai/news-updates/rss.xml | 国际 | 模型发布 | en | |
| Databricks | databricks.com/rss.xml | 国际 | 行业动态 | en | |
| MIT TechReview | technologyreview.com/.../ai/feed | 国际 | 行业动态 | en | 媒体 |
| VentureBeat AI | venturebeat.com/category/ai/feed/ | 国际 | 投融资 | en | UTF-8 强制解码 |
| The Gradient | thegradient.pub/rss/ | 国际 | 研究论文 | en | 媒体 |
| IEEE Spectrum AI | spectrum.ieee.org/.../ai.rss | 国际 | 行业动态 | en | 权威科技媒体 AI 专题 |
| MIT News AI | news.mit.edu/rss/topic/ai2 | 国际 | 研究论文 | en | 高校官方新闻 |
| ZDNet AI | zdnet.com/topic/ai/rss.xml | 国际 | 行业动态 | en | 主流科技媒体 |
| The Decoder | the-decoder.com/feed/ | 国际 | 产品发布 | en | 专注 AI 的媒体 |
| The Rundown AI | therundown.ai/feed | 国际 | 产品发布 | en | AI 日报媒体 |
| Anthropic (GN) | news.google.com/rss/search?q=Anthropic | 国际 | 产品发布 | en | Google News 聚合，无官方 RSS |
| Meta AI (GN) | news.google.com/rss/search?q="Meta AI" | 国际 | 产品发布 | en | Google News 聚合 |
| 量子位 | qbitai.com/feed | 国内 | 行业动态 | zh | |
| InfoQ中文 | infoq.cn/feed | 国内 | 行业动态 | zh | |
| 极客公园 | geekpark.net/rss | 国内 | 产品发布 | zh | CDATA 标题 |
| 少数派 | sspai.com/feed | 国内 | 产品发布 | zh | |

**Google News 源**（`is_gnews=True`）：`<link>` 是 GN 中转页（`/rss/articles/...`，302 跳转到原文），直接保留为 url；标题末尾 " - 媒体名" 被剥离，source 标注实际媒体名（`dims.py:280`）。

### HuggingFace 模型榜（`tracker.py:111`）
- 端点：`HF_BASE="https://hf-mirror.com"`（官方 HF 本网络不可达，走镜像）。
- `fetch_hf_models(sort, limit=30)`：trendingScore / likes 两种 sort。
- 降级：镜像不可达 → 读旧缓存 → 内存兜底。

### arXiv 论文（`tracker.py:260` `search_arxiv_papers`）
- 端点：`ARXIV_API="https://export.arxiv.org/api/query"`（必须 HTTPS）。
- **限速**：`ARXIV_GAP=3.0` 秒/请求（官方要求，否则 429）。
- **配额控制**：`ARXIV_ENRICH_LIMIT=8`，只对榜单前 8 热词检索（8×3s≈24s）。
- 检索式：`_search_query_for(term)` 按模型名逐词 `all:` 全文检索。
- 只在后台预热 + `get_term_detail` 同步调用（详情页慢根因）。

### LLM（模型故障转移链，`dims.py:959` `_llm_classify_batch`）
- **故障转移链** `config.LLM_CHAIN`（默认 `glm-4.7-flash → glm-5.3-flash → deepseek-v4-flash`）：首档默认 GLM-4.7-Flash，**可用性失败**（429/5xx/超时/key 无效等）每档连续 `LLM_FAILOVER_THRESHOLD`（默认 3）次顺链切下一档（单向熔断式，成功只清零计数不回退首档；**2026-09-02 起 `_dims_refresh_once` 每轮起始 `_llm_cycle_reset` 复位回链首**——故障转移只限当轮逃生）；**质量失败**（混杂/缺翻译/JSON）2026-09-03 起走独立高阈值熔断 `_llm_quality_failure`（连续 6/周期累计 12 才换档）——零星 1-2/6 混杂不再快速换档（换档救不了质量：DeepSeek 被拒率同样 ~50%，只会把账单抬到 3 倍价档；09-03 仍费用异常的根因），GLM-5.3 只要在线就整轮主扛、DeepSeek 只兜底；无 key 的 provider 档不烧重试、直接顺链跳过。当前档端点由 `config.llm_endpoint(model)` 解析（glm-* → 智谱 BigModel，deepseek-* → DeepSeek）。
- **思考强度控制**（2026-08-31，针对 GLM-5.3-Flash）：GLM-5.2+ 的 thinking 不可关闭（`thinking.type=disabled` 会报错），只能经 `reasoning_effort` 调强度；`config.llm_reasoning_params(model)` 对 glm-5.2+ 返回 `{"reasoning_effort": LLM_REASONING_EFFORT}`（默认 low），glm-4.7/deepseek 返回 {}。low 减少 thinking token 挤占 max_tokens，降低 length 截断导致的 content 空/缺翻译。提示词同步加了防回显（翻译字段不得照抄原标题）、非空（翻译字段禁止空字符串）、数组长度与输入一致、禁 Markdown 代码块等规则。
- **中英混杂防线**（2026-09-01，issue 11）：提示词显式要求「翻译必须完整、不得保留原文片段、禁止中英混杂输出」（title 与 summary 都要求）；批完整性检查再加硬编码兜底 `_is_mixed_translation`——中文原文翻英文残留任一 CJK、或英文原文翻中文长文本 ASCII 字母占比 >60%，该条按坏计，不静默回退成原文标题。**2026-09-02：逐条校验回填**——过者即回填、坏者计数（好条目不白烧）。**2026-09-03：坏条目「二次提示」修正**——带失败原因（缺翻译/混杂）喂回当前档模型继续修正最多 `LLM_REPAIR_ROUNDS`（默认 2）轮（`_llm_apply_output`@864 逐条校验/回填），仍失败按质量失败计（不快速换档）。
- 用途：维度打标（模型与技术/产品与应用/研究与论文/商业与投融资/政策与行业/其他）+ 双语翻译（title_zh/title_en/summary_zh/summary_en）+ **抽取关键词 keywords**（1-3 个高价值 AI 实体/技术词——具体模型/产品/公司名、核心技术、事件主体；禁止泛化词/纯形容词，词维度重构核心）。
- **热词解释生成**（动态词典资产，`dims.py:1361` `explain_terms`）：供 `terms.refresh_words` 的 `term_explainer` 回调；面向普通访客的「定义 + 为什么值得关注」双语解释，携带代表报道标题作上下文；已有解释仅明显更优才返回新文本。失败降级返回空映射（详情页模板兜底）。
- **热词翻译**（display_en，`dims.py:1309` `_translate_terms`，供 refresh_words 的 term_translator 回调）：2026-09-02 起增量翻译（缺 en 词优先 + `TRANSLATE_BATCH_MAX_WORDS=100` 上限，预算内回译已有 en 词允许更优更新）——不再每轮全量重译池内中文词（当日观察 ~35 次 LLM 调用/轮）。
- **降级**（无 LLM key：`GLM_API_KEY` 与 `DEEPSEEK_API_KEY` 均未设）：用 RSS 源 `default_dim` 分类、双 slot 填原标题、summary_zh 取原标题前 30 字；**keywords 走 `terms.extract_keywords_dict` 词典匹配**。零 token 消耗。天然 Mock 机制，无需代码开关。
- 瞬态容错：`_post` 对连接重置/超时 + HTTP 429/5xx + 错误体 1305（GLM 免费档过载）重试 3 次；永久错误直接抛 → `_llm_failure()` 计一次并顺链。**2026-09-02：HTTP 402（余额不足）归为账户级限流 `_LLMAccountRateLimit`**（非瞬态不重试，顺链跳过）。**2026-09-03：解析/混杂类异常归 `_LLMQualityError` → `_llm_quality_failure()`（质量熔断），不再计 provider 快速换档**。
- 批量调用：`enrich_with_llm(items)` 分批打标（6 条子批；质量失败不重复重试、provider 故障才收进末尾重试集）。
- 生产定点刷新：`DIMS_REFRESH_HOURS=(1,7,13,19)`，避开高峰段 + 命中硬盘缓存 TTL。

### 关键词词典（`terms.py:256` `_LEXICON`）
- canonical → 表面形式列表（ASCII 词边界匹配 + CJK 子串匹配），版本感知词边界（"GPT-5.5" 不命中 gpt-5）。
- 用途：无 LLM key 降级抽词、历史库零成本回填、常见异形归一、display_zh 来源。
- 通用热词停用词表（`terms.py:359` `_TERM_STOPWORDS`）：低价值通用词（"AI"/"llm"/"model" 等，canonical 键）在抽词（`extract_keywords_dict`）、聚合（`_keyword_canons`）、HF 词（`_hf_canon` 后）三级被剔除，不作为独立热词。
- 热词解释：三级取词——① `terms.py:374` `_EXPLANATIONS`（canonical → zh/en 人工精编解释）→ ② `terms` 表 `explain_zh/en`（LLM 每轮刷新生成/优化，动态词典资产）→ ③ 详情页模板兜底（`_explain_fallback`，保证每词有解释块）。入口 `terms.get_term_explanation`（`terms.py:1513`）。

## SQLite Schema

### sponsors.db（`store.py:80` `init_db`，路径 `config.DB_PATH`，含用户行为事件表 v3）

**`sponsor_slots`** — 赞助位
| 列 | 类型 | 说明 |
|----|------|------|
| slot_id | TEXT PK | |
| name/text/subtext | TEXT | 展示文案 |
| link_url/image_url | TEXT | 跳转/图片 |
| banner_html | TEXT | 自定义 HTML（`sanitize_banner_html` 净化） |
| region | TEXT | all/zh/global，默认 all |
| active | INT | 1/0 |
| sort_order/start_date/end_date/cta_text | | 排序/档期/按钮文案 |
| created_at/updated_at | TEXT | |

**`sponsor_stats`** — (slot_id, date) → impressions/clicks，PK(slot_id,date)。
**`pageviews`** — date → count，PK(date)。
**`visits`** — 访问明细（监控页数据源）
| 列 | 类型 | 说明 |
|----|------|------|
| id | INT PK AUTO | |
| ip | TEXT | |
| country | TEXT | ISO 国家码或 Unknown |
| path | TEXT | 默认 / |
| ts | TEXT | 完整 ISO 时间戳 |
| date | TEXT | YYYY-MM-DD，索引化 |
- 索引：`idx_visits_date`、`idx_visits_ip_date`。
- PV = 行数，UV = `COUNT(DISTINCT ip)`。

**`user_events`** — 通用用户行为事件（埋点系统 v3，`store.py:670`）
| 列 | 类型 | 说明 |
|----|------|------|
| id | INT PK AUTO | |
| event_type | TEXT | page_view / click / search / word_expand / word_detail / view_switch / lang_switch / sort_switch / cat_filter / link_click / search_click |
| event_data | TEXT | JSON 附加数据（如 {term, url, from_view, ...}），限长 2000 |
| ip | TEXT | 客户端 IP |
| country | TEXT | ISO 国家码或 Unknown |
| session_id | TEXT | 前端生成的会话 ID（localStorage），串联同一用户轨迹 |
| path | TEXT | 当前页面路径 |
| ts | TEXT | 完整 ISO 时间戳 |
| date | TEXT | YYYY-MM-DD，索引化 |
- 索引：`idx_events_date`、`idx_events_type_date`。
- 写入函数：`record_event()`（单条）、`record_events_batch()`（批量事务）。
- 查询函数：`event_stats(days)` → 按类型计数 + 每日趋势 + 近期明细。

### news.db（`news_store.py:36` `init_db`，路径 `config.NEWS_DB_PATH`）

**`news_cards`** — 事件卡历史库
| 列 | 类型 | 说明 |
|----|------|------|
| url | TEXT PK | official_url，自然主键 |
| title/title_zh/title_en | TEXT | 原生+双语 |
| summary_zh/summary_en | TEXT | LLM 摘要 |
| dimension | TEXT | 维度（新 6 类枚举） |
| source/region/published | TEXT | |
| hn_points/reddit_score/reddit_comments | INT | 社区热度信号 |
| score/trend/hot | INT | 累计热度/上升势头（每次刷新重算） |
| keywords | TEXT | JSON 数组（canonical 词键，每卡 0-3 个；词维度重构新增，幂等迁移列） |
| first_seen_at/last_refresh_at | TEXT | 首次入库/最近刷新 |
| active | INT | 0=历史归档（近期未刷新命中） |
- 索引：`idx_news_{score,trend,published,dim}`。
- 迁移 `_migrate`（`news_store.py:89`）：加 keywords 列、修复 NULL；旧维度值（模型发布/产品发布/...）→ 新 6 类幂等映射。

**`terms`** — 词主表（`terms.py:176` `init_db`）
| 列 | 类型 | 说明 |
|----|------|------|
| term | TEXT PK | canonical 键（小写归一），如 "gpt-5" |
| display / display_zh / display_en | TEXT | 最佳展示形 / 中文别名 / 英文展示名（中文热词的 LLM 翻译；LLM 限流轮次保留旧值） |
| origin | TEXT | news / hf / both（热词来源归并） |
| first_seen_at / last_seen_at | TEXT | 首次进入词池 / 最近见 |
| total_mentions | INT | 累计关联报道数（url 去重） |
| hf_json | TEXT | HF 模型词元数据 JSON 快照 |
| cur_hot / cur_rise / cur_novelty | INT/REAL/REAL | 本周期热度/环比增速/新奇度 |
| explain_zh / explain_en | TEXT | 动态词典解释（LLM 生成/优化；静态 `_EXPLANATIONS` 词不写，取词时静态优先） |
| explain_updated_at | TEXT | 解释新鲜度（>24h 才进优化批次，≤1 次/天/词） |

**`term_snapshots`** — 词周期快照（支撑环比）
- `(term, cycle)` 复合主键；`cycle` 形如 `2026-08-28-13`（Asia/Shanghai 定点小时）。
- 列：`news_cnt` / `score_sum` / `signal_sum`。

WAL 模式。DB 不可用 → `_DB_OK=False` 全程静默降级返空。

## 文件缓存产物（`cache/`，路径 `config.CACHE_DIR`）

| 文件 | 写入者 | 读者 | 内容 |
|------|--------|------|------|
| `terms.json` | `tracker._refresh_once` | `tracker.get_terms`/`get_model_cards` | HF 热词榜 + model 卡 |
| `dims.json` | `dims._dims_refresh_once` | `dims.get_dims`/`get_news_cards` | 维度事件卡分组 |
| `words.json` | `terms.refresh_words`（dims 刷新锁内调） | `terms.get_word_cards` | 词卡榜（热度/上升/新奇度，词维度重构新增；hot 按报道新鲜度加权——≤1d ×3 / ≤3d ×1.5，2026-09-02 优化） |
| `.tracker.refresh.lock` | `tracker._cross_proc_lock` | — | fcntl 跨进程锁 |
| `.dims.refresh.lock` | `dims._cross_proc_lock` | — | fcntl 跨进程锁 |

容器内 `CACHE_DIR=/app/cache`；本地裸跑退化到 `./cache`。

## 环境变量清单（`config.py`，标注默认值 + 降级行为）

| 变量 | 默认 | 作用 / 降级 |
|------|------|-------------|
| `SECRET_KEY` | 进程级随机 | Flask 会话签名；生产必须显式设置 |
| `ADMIN_TOKEN` | "" | 未设 → 所有 `/admin/*` 返回 404（隐身） |
| `SITE_NAME` | ModelRadar | 站点名 |
| `BASE_URL` | "" | 站点绝对 URL（SEO canonical/OG） |
| `CONTACT_EMAIL` | "" | 条款页联系邮箱；未设 → 占位文案 |
| `DATA_DIR` | /app/data | SQLite 目录 |
| `NEWS_DB_PATH` | $DATA_DIR/news.db | 事件库路径 |
| `GEOIP_DB_PATH` | $DATA_DIR/GeoLite2-Country.mmdb | 离线地域库；缺失 → Unknown |
| `CACHE_DIR` | /app/cache 或 ./cache | 文件缓存目录 |
| `LLM_CHAIN` | glm-4.7-flash,glm-5.3-flash,deepseek-v4-flash | 模型故障转移链（逗号分隔，按序尝试；首档即默认）；单档测试如 `glm-5.3-flash` |
| `LLM_FAILOVER_THRESHOLD` | 3 | 每档连续失败次数达此值 → 顺链切下一档 |
| `LLM_CYCLE_ESCAPE` | 4 | 单刷新周期累计失败达此值 → 跳当前 provider 剩余档 |
| `LLM_REASONING_EFFORT` | low | GLM-5.2+ 思考强度（low/high/max；GLM-5.3-Flash 思考不可关闭只能调强度）；glm-4.7/deepseek 不传 |
| `DEEPSEEK_API_KEY` | "" | DeepSeek key（末档兜底用）；GLM/DEEPSEEK 两 key 均未设 → 走降级（default_dim+原标题） |
| `GLM_API_KEY` | "" | GLM（智谱 BigModel）key；⚠️ 免费档并发上限 1，高峰常返 429/1305，dims 已做重试+顺链转移+降级 |
| `DIMS_REFRESH_HOURS` | 1,7,13,19 | 定点刷新时刻（Asia/Shanghai） |
| `ANALYTICS_ENABLED` | true | 分析开关 |
| `SEO_ENABLED` | true | 关 → 不输出 canonical/OG/JSON-LD，robots 禁止索引 |
| `SITEMAP_MAX_URLS` | 200 | sitemap 上限 |
| `TERM_DETAIL_CACHE_TTL` | 1800 | 详情页进程内缓存秒 |
| `ADSENSE_ENABLED` | false | Google AdSense |
| `ADSENSE_CLIENT` | "" | ca-pub-xxx |
| `BAIDU_ADS_ENABLED` | false | 百度联盟（需 ICP 备案） |
| `BAIDU_ADS_CPRO_ID` | "" | |
| `INLINE_SLOT_EVERY_N` | 8 | 每 N 张卡插一张赞助卡 |
| `NEWS_HISTORY_LIMIT` | 400 | 历史内容池回溯上限 |
| `NEWS_HISTORY_DAYS` | 30 | 历史回溯天数 |

样例见 `.env.example`。
