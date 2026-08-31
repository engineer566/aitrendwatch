# 数据流与外部依赖索引

> 外部数据源、SQLite schema、缓存产物、环境变量。配合 [INDEX.md](../INDEX.md) 使用。

## 外部数据源

### RSS 源（17 个，`dims.py:153` `RSS_SOURCES`）

并发抓取（`fetch_all_rss` `dims.py:327`，8 worker），按 url 去重，每源取前 `PER_SOURCE_LIMIT=6` 条；RSS 标题使用有界双层实体解码，URL 只解一层 XML entity。

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
| Anthropic (GN) | news.google.com/rss/search?q=Anthropic | 国际 | 产品发布 | en | Google News 聚合，无官方 RSS |
| Meta AI (GN) | news.google.com/rss/search?q="Meta AI" | 国际 | 产品发布 | en | Google News 聚合 |
| 量子位 | qbitai.com/feed | 国内 | 行业动态 | zh | |
| InfoQ中文 | infoq.cn/feed | 国内 | 行业动态 | zh | |
| 极客公园 | geekpark.net/rss | 国内 | 产品发布 | zh | CDATA 标题 |
| 少数派 | sspai.com/feed | 国内 | 产品发布 | zh | |

**Google News 源**（`is_gnews=True`）：`<link>` 是中转页，`official_url` 取媒体域名；标题末尾 " - 媒体名" 被剥离（`dims.py:272`）。

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

### LLM（模型故障转移链，`dims.py:713` `_llm_classify_batch`）
- **故障转移链** `config.LLM_CHAIN`（默认 `glm-4.7-flash → glm-4.6v-flash → glm-4.6v-flashx → glm-4.7-flashx → deepseek-v4-flash`）：首档默认 GLM-4.7-Flash，每档连续 `LLM_FAILOVER_THRESHOLD`（默认 10）次失败顺链切下一档（单向熔断式，成功只清零计数不回退首档）；无 key 的 provider 档不烧重试、直接顺链跳过。当前档端点由 `config.llm_endpoint(model)` 解析（glm-* → 智谱 BigModel，deepseek-* → DeepSeek）。
- 用途：维度打标（模型与技术/产品与应用/研究与论文/商业与投融资/政策与行业/其他）+ 双语翻译（title_zh/title_en/summary_zh/summary_en）+ **抽取关键词 keywords**（1-3 个 AI 实体/技术词，词维度重构核心）。
- **降级**（无 LLM key：`GLM_API_KEY` 与 `DEEPSEEK_API_KEY` 均未设）：用 RSS 源 `default_dim` 分类、双 slot 填原标题、summary_zh 取原标题前 30 字；**keywords 走 `terms.extract_keywords_dict` 词典匹配**。零 token 消耗。天然 Mock 机制，无需代码开关。
- 瞬态容错：`_post` 对连接重置/超时 + HTTP 429/5xx + 错误体 1305（GLM 免费档过载）重试 3 次；永久错误直接抛 → `_llm_failure()` 计一次并顺链。
- 批量调用：`enrich_with_llm(items)` 分批打标（6 条子批）。
- 生产定点刷新：`DIMS_REFRESH_HOURS=(13,19,1,7)`，避开高峰段 + 命中硬盘缓存 TTL。

### 关键词词典（`terms.py:209` `_LEXICON`）
- canonical → 表面形式列表（ASCII 词边界匹配 + CJK 子串匹配），版本感知词边界（"GPT-5.5" 不命中 gpt-5）。
- 用途：无 LLM key 降级抽词、历史库零成本回填、常见异形归一、display_zh 来源。
- 热词解释：`terms.py:311` `_EXPLANATIONS`（canonical → zh/en 解释），`terms.get_term_explanation`（`terms.py:1122`）供详情页「这是什么」块，未收录词空串。

## SQLite Schema

### sponsors.db（`store.py:80` `init_db`，路径 `config.DB_PATH`）

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

### news.db（`news_store.py:35` `init_db`，路径 `config.NEWS_DB_PATH`）

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
- 迁移 `_migrate`（`news_store.py:88`）：加 keywords 列、修复 NULL；旧维度值（模型发布/产品发布/...）→ 新 6 类幂等映射。

**`terms`** — 词主表（`terms.py:151` `init_db`）
| 列 | 类型 | 说明 |
|----|------|------|
| term | TEXT PK | canonical 键（小写归一），如 "gpt-5" |
| display / display_zh | TEXT | 最佳展示形 / 中文别名 |
| origin | TEXT | news / hf / both（热词来源归并） |
| first_seen_at / last_seen_at | TEXT | 首次进入词池 / 最近见 |
| total_mentions | INT | 累计关联报道数（url 去重） |
| hf_json | TEXT | HF 模型词元数据 JSON 快照 |
| cur_hot / cur_rise / cur_novelty | INT/REAL/REAL | 本周期热度/环比增速/新奇度 |

**`term_snapshots`** — 词周期快照（支撑环比）
- `(term, cycle)` 复合主键；`cycle` 形如 `2026-08-28-13`（Asia/Shanghai 定点小时）。
- 列：`news_cnt` / `score_sum` / `signal_sum`。

WAL 模式。DB 不可用 → `_DB_OK=False` 全程静默降级返空。

## 文件缓存产物（`cache/`，路径 `config.CACHE_DIR`）

| 文件 | 写入者 | 读者 | 内容 |
|------|--------|------|------|
| `terms.json` | `tracker._refresh_once` | `tracker.get_terms`/`get_model_cards` | HF 热词榜 + model 卡 |
| `dims.json` | `dims._dims_refresh_once` | `dims.get_dims`/`get_news_cards` | 维度事件卡分组 |
| `words.json` | `terms.refresh_words`（dims 刷新锁内调） | `terms.get_word_cards` | 词卡榜（热度/上升/新奇度，词维度重构新增） |
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
| `LLM_CHAIN` | glm-4.7-flash,glm-4.6v-flash,glm-4.6v-flashx,glm-4.7-flashx,deepseek-v4-flash | 模型故障转移链（逗号分隔，按序尝试；首档即默认） |
| `LLM_FAILOVER_THRESHOLD` | 10 | 每档连续失败次数达此值 → 顺链切下一档 |
| `DEEPSEEK_API_KEY` | "" | DeepSeek key（末档兜底用）；GLM/DEEPSEEK 两 key 均未设 → 走降级（default_dim+原标题） |
| `GLM_API_KEY` | "" | GLM（智谱 BigModel）key；⚠️ 免费档并发上限 1，高峰常返 429/1305，dims 已做重试+顺链转移+降级 |
| `DIMS_REFRESH_HOURS` | 13,19,1,7 | 定点刷新时刻（Asia/Shanghai） |
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
