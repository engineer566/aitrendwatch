# 路由索引

> 全部 42 条路由（含 errorhandler），按功能分组。每条带 `app.py:行号` 便于跳读。
> 配合 [INDEX.md](../INDEX.md) 使用。
> 注：词维度重构（2026-08）后 `/api/trending` `/api/top` `/api/term/<name>` 三个旧
> tracker JSON API 已删除（词聚合由 `/api/stream?view=words` 承担）。

## 页面路由（HTML）

| 路径 | 方法 | 函数 | 行号 | 功能 | 备注 |
|------|------|------|------|------|------|
| `/` | GET | `index` | `app.py:633` | 首页主单页（词视图为主 + 逐条新闻 tab） | 记 PV/visit/曝光，SSR 首批词卡，前端 JS 再拉 `/api/stream` |
| `/hf` | GET | `hf_page` | `app.py:910` | **HuggingFace 独立排序页（开源动向）**：按趋势分/点赞/下载量排序，每卡带 pipeline_tag 主徽标 + tags/官方/社区/论文 | 服务端渲染双语（zh/en），排序/语言切换零 fetch；数据复用 tracker 缓存 |
| `/term/<path:term_name>` | GET | `term_detail` | `app.py:712` | 通用热词聚合页（SEO 长尾）：任何词有页——相关报道聚合 + 词热度；HF 词额外含官方/社区/arXiv 区块 | 进程内 TTL 缓存；未找到 → 404 |
| `/terms` | GET | `terms` | `app.py:768` | 服务条款页（中英双语） | 静态文案，`SITE_TERMS_UPDATED` 常量 |
| `/admin/login` | GET,POST | `admin_login` | `app.py:1438` | 管理员登录 | `ADMIN_TOKEN` 未设 → 404 隐身，登录后默认跳 `/monitor` |
| `/admin` | GET | `admin_home` | `app.py:1462` | ~~赞助位管理后台~~ → 重定向到 `/monitor#sponsors` | 需 admin，合并后统一入口 |
| `/monitor` | GET | `monitor` | `app.py:1512` | **统一管理后台**（流量监控 + 赞助位管理 Tab 切换） | 需 admin |

## 数据 API（JSON）

### 通用热点聚合（旧主功能，8 直连源）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/sources` | GET | `api_sources` | `app.py:669` | 所有源元信息（`SOURCE_META`） |
| `/api/hot/<source>` | GET | `api_hot` | `app.py:674` | 单源热点（带硬性超时 `SOURCE_DEADLINE`） |
| `/api/all` | GET | `api_all` | `app.py:679` | 并发聚合所有 8 源 |

### 词维度层（词维度重构后主功能）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/dims` | GET | `api_dims` | `app.py:831` | 按 AI 维度分组的热点卡；`?dimension=` `?lang=zh/en` |
| `/api/stream` | GET | `api_stream` | `app.py:840` | **统一卡片流**，前端主数据源；`?view=words\|news`（默认 words）+ `?lang=` `?sort=rise/hot/new`。words 视图词卡（热度=报道聚合+HF likes、上升=环比、最新=新奇度新词发现）；news 视图 model+news 逐条（返回 count/dimension_counts 与稳定排序） |
| `/api/word/<term>` | GET | `api_word` | `app.py:999` | 单词聚合 JSON：词元信息 + 全量关联报道（≤50）；词卡「展开更多」与详情页共用 |
| `/api/hf` | GET | `api_hf` | `app.py:973` | **HuggingFace 模型排序 JSON**：`?sort=trending\|likes\|downloads` + `?lang=`；返回 `{ok, sort, lang, fetched_at, count, terms}`（每卡含 pipeline_tag/tags/likes/downloads/trending_score/community/papers） | 复用 tracker 文件缓存（`_hf_models_for`），秒回 |

### 系统

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/health` | GET | `health` | `app.py:951` | 健康检查 |
| `/api/click/<path:slot_id>` | GET | `sponsor_click` | `app.py:1346` | 赞助位点击计数 + 302 跳转 |
| `/admin/stats` | GET | `admin_stats` | `app.py:1505` | 赞助位 30 天统计（需 admin） |
| `/admin/sponsors/list` | GET | `admin_sponsors_list` | `app.py:1469` | 赞助位列表 JSON（供合并后 monitor 页 AJAX 加载，需 admin） |
| `/monitor/api` | GET | `monitor_api` | `app.py:1518` | 监控页数据（`?days=1..90`，需 admin） |
| `/monitor/api/search` | GET | `monitor_search_api` | `app.py:1529` | 搜索词统计（热门搜索 Top-N + 近期搜索，需 admin） |
| `/monitor/api/search/funnel` | GET | `monitor_search_funnel_api` | `app.py:1545` | 搜索→点击漏斗（需 admin） |

### Admin 写操作（需 admin，POST）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/admin/sponsors` | POST | `admin_upsert_sponsor` | `app.py:1477` | 新建/更新赞助位 |
| `/admin/sponsors/<slot_id>/toggle` | POST | `admin_toggle_sponsor` | `app.py:1487` | 上下架切换 |
| `/admin/sponsors/<slot_id>/delete` | POST | `admin_delete_sponsor` | `app.py:1496` | 删除赞助位 |

## SEO 路由

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/robots.txt` | GET | `robots` | `app.py:1320` | 爬虫规则（SEO 关 → 禁止索引） |
| `/sitemap.xml` | GET | `sitemap` | `app.py:1340` | 站点地图（首页 + 热词详情页，上限 `SITEMAP_MAX_URLS`） |
| `/favicon.ico` | GET | `favicon` | `app.py:1378` | favicon |
| `/favicon.svg` | GET | `favicon_svg` | `app.py:1387` | favicon SVG |
| `/apple-touch-icon.png` | GET | `apple_touch_icon` | `app.py:1399` | Apple 触屏图标 |

## 错误处理

| 类型 | 函数 | 行号 | 功能 |
|------|------|------|------|
| 404 | `not_found` | `app.py:817` | 简单 HTML + `noindex`，防爬虫索引不存在的 term 页 |

## 鉴权机制

- `admin_required` 装饰器（`app.py:1419`）：`ADMIN_TOKEN` 未设 → 所有 `/admin/*` 返回 404（隐身）。
- token 来源优先级：`Authorization: Bearer` → `?token=` → `session["admin_token"]`。
- `hmac.compare_digest` 防时序攻击；未登录页面请求 → 跳登录页（带 next），API 请求 → 401。
- `/monitor` 与 `/admin` 共用同一 `admin_required`。
