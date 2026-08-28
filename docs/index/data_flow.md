# 数据流与外部依赖索引

> 外部数据源、SQLite schema、缓存产物、环境变量。配合 [INDEX.md](../INDEX.md) 使用。

## 外部数据源

### RSS 源（17 个，`dims.py:145` `RSS_SOURCES`）

并发抓取（`fetch_all_rss` `dims.py:302`，8 worker），按 url 去重，每源取前 `PER_SOURCE_LIMIT=6` 条。

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

**Google News 源**（`is_gnews=True`）：`<link>` 是中转页，`official_url` 取媒体域名；标题末尾 " - 媒体名" 被剥离（`dims.py:255`）。

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

### DeepSeek LLM（`dims.py:630` `_llm_classify_batch`）
- 用途：维度打标（模型发布/产品发布/投融资/研究论文/行业动态…）+ 双语翻译（title_zh/title_en/summary_zh/summary_en）。
- **降级**（无 `DEEPSEEK_API_KEY`）：用 RSS 源 `default_dim` 分类、双 slot 填原标题、summary_zh 取原标题前 30 字。零 token 消耗。天然 Mock 机制，无需代码开关。
- 批量调用：`enrich_with_llm(items)` 分批打标。
- 生产定点刷新：`DIMS_REFRESH_HOURS=(13,19,1,7)`，避开高峰段 + 命中硬盘缓存 TTL。

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

### news.db（`news_store.py:34` `init_db`，路径 `config.NEWS_DB_PATH`）

**`news_cards`** — 事件卡历史库
| 列 | 类型 | 说明 |
|----|------|------|
| url | TEXT PK | official_url，自然主键 |
| title/title_zh/title_en | TEXT | 原生+双语 |
| summary_zh/summary_en | TEXT | LLM 摘要 |
| dimension | TEXT | 维度 |
| source/region/published | TEXT | |
| hn_points/reddit_score/reddit_comments | INT | 社区热度信号 |
| score/trend/hot | INT | 累计热度/上升势头（每次刷新重算） |
| first_seen_at/last_refresh_at | TEXT | 首次入库/最近刷新 |
| active | INT | 0=历史归档（近期未刷新命中） |
- 索引：`idx_news_{score,trend,published,dim}`。

WAL 模式。DB 不可用 → `_DB_OK=False` 全程静默降级返空。

## 文件缓存产物（`cache/`，路径 `config.CACHE_DIR`）

| 文件 | 写入者 | 读者 | 内容 |
|------|--------|------|------|
| `terms.json` | `tracker._refresh_once` | `tracker.get_terms`/`get_model_cards` | HF 热词榜 + model 卡 |
| `dims.json` | `dims._dims_refresh_once` | `dims.get_dims`/`get_news_cards` | 维度事件卡分组 |
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
| `DEEPSEEK_API_KEY` | "" | LLM 打标；未设 → 走降级（default_dim+原标题） |
| `DEEPSEEK_MODEL` | deepseek-v4-flash | LLM 模型名 |
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
