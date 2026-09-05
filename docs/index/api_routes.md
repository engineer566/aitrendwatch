# 路由索引

> 全部 38 条路由（含 errorhandler），按功能分组。每条带 `app.py:行号` 便于跳读。
> 配合 [INDEX.md](../INDEX.md) 使用。
> 注：词维度重构（2026-08）后 `/api/trending` `/api/top` `/api/term/<name>` 三个旧
> tracker JSON API 已删除（词聚合由 `/api/stream?view=words` 承担）。
> 2026-09-05 SEO P1~P5：词条详情页加 indexable 可索引门槛；sitemap 主语言改英文
> （只交 `?lang=en` 变体 + 达标词）；首页/词条/hf 页 head 输出 hreflang zh↔en。

## 页面路由（HTML）

| 路径 | 方法 | 函数 | 行号 | 功能 | 备注 |
|------|------|------|------|------|------|
| `/` | GET | `index` | `app.py:661` | 首页主单页（词视图为主 + 逐条新闻 tab） | 记 PV/visit/曝光，SSR 首批词卡，前端 JS 再拉 `/api/stream`；渲染参数含 hreflang（zh/en 变体 + x-default=en） |
| `/hf` | GET | `hf_page` | `app.py:973` | **HuggingFace 独立排序页（开源动向）**：按趋势分/点赞/下载量排序，每卡带 pipeline_tag 主徽标 + tags/官方/社区/论文 | 服务端渲染双语（zh/en），排序/语言切换零 fetch；数据复用 tracker 缓存；渲染参数含 hreflang |
| `/term/<path:term_name>` | GET | `term_detail` | `app.py:760` | 通用热词聚合页（SEO 长尾）：相关报道聚合 + 词热度 + 近 7 天活跃度趋势迷你图；HF 词额外含官方/社区/arXiv 区块 | 进程内 TTL 缓存；未找到 → 404；**indexable 门槛**（2026-09-05 P1）：`term_row_indexable` 判定（news_cnt<`TERM_INDEX_MIN_NEWS`、hot<`TERM_INDEX_MIN_HOT` 或词池外 HF 回退 → 页面照常渲染但 meta robots=noindex）；渲染参数含 hreflang |
| `/terms` | GET | `terms` | `app.py:831` | 服务条款页（中英双语） | 静态文案，`SITE_TERMS_UPDATED` 常量；单页内嵌双语，无 hreflang |
| `/search` | GET | `search_page` | `app.py:1261` | 搜索结果页（独立页，热词命中卡置顶 + 高亮 + 历史归档标记；2026-09-04 需求 1：news 结果按归一化标题去重，镜像报道不双显） | `?q=` `?lang=zh/en` |
| `/admin/login` | GET,POST | `admin_login` | `app.py:1569` | 管理员登录 | `ADMIN_TOKEN` 未设 → 404 隐身，登录后默认跳 `/monitor` |
| `/admin/logout` | GET | `admin_logout` | `app.py:1586` | 退出登录 | 清 session 回登录页 |
| `/admin` | GET | `admin_home` | `app.py:1593` | ~~赞助位管理后台~~ → 重定向到 `/monitor#sponsors` | 需 admin，合并后统一入口 |
| `/monitor` | GET | `monitor` | `app.py:1643` | **统一管理后台**（流量监控 + 赞助位管理 Tab 切换） | 需 admin |

## 数据 API（JSON）

### 通用热点聚合（旧主功能，8 直连源）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/sources` | GET | `api_sources` | `app.py:730` | 所有源元信息（`SOURCE_META`） |
| `/api/hot/<source>` | GET | `api_hot` | `app.py:735` | 单源热点（带硬性超时 `SOURCE_DEADLINE`） |
| `/api/all` | GET | `api_all` | `app.py:740` | 并发聚合所有 8 源 |

### 词维度层（词维度重构后主功能）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/dims` | GET | `api_dims` | `app.py:860` | 按 AI 维度分组的热点卡；`?dimension=` `?lang=zh/en` |
| `/api/stream` | GET | `api_stream` | `app.py:869` | **统一卡片流**，前端主数据源；`?view=words\|news`（默认 words）+ `?lang=` `?sort=rise/hot/new`。words 视图词卡（热度=报道聚合+HF likes、上升=环比、最新=新奇度新词发现）；news 视图 model+news 逐条（2026-09-04 需求 1：news 卡源 `dims.get_news_cards` 已做 id url 归一 + 标题级去重，同标题镜像只出现一次；返回 count/dimension_counts 与稳定排序） |
| `/api/word/<term>` | GET | `api_word` | `app.py:1035` | 单词聚合 JSON：词元信息 + 全量关联报道（≤50）+ trend 近 7 天活跃度序列；词卡「展开更多」与详情页共用 |
| `/api/hf` | GET | `api_hf` | `app.py:1009` | **HuggingFace 模型排序 JSON**：`?sort=trending\|likes\|downloads` + `?lang=`；返回 `{ok, sort, lang, fetched_at, count, terms}`（每卡含 pipeline_tag/tags/likes/downloads/trending_score/community/papers） | 复用 tracker 文件缓存（`_hf_models_for`），秒回 |

### 全站搜索 v2

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/search` | GET | `api_search` | `app.py:1344` | 搜索 JSON（加权打分 + 高亮 + 历史归档计数；2026-09-04 需求 1：news 命中按归一化标题去重，镜像报道只留评分高者） |
| `/api/search/suggest` | GET | `api_search_suggest` | `app.py:1307` | 搜索补全建议 |
| `/api/search/click` | POST | `api_search_click` | `app.py:1326` | 搜索→点击埋点（漏斗数据源） |

### 用户行为事件（埋点系统 v3）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/event` | POST | `api_event` | `app.py:1695` | 用户行为事件上报（批量兼容：单条/`{events:[...]}`；`event_type` 白名单校验） | 前端埋点统一入口（2026-09-01 任务 9） |

### 系统

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/health` | GET | `health` | `app.py:1049` | 健康检查 |
| `/api/click/<path:slot_id>` | GET | `sponsor_click` | `app.py:1540` | 赞助位点击计数 + 302 跳转 |
| `/admin/stats` | GET | `admin_stats` | `app.py:1636` | 赞助位 30 天统计（需 admin） |
| `/admin/sponsors/list` | GET | `admin_sponsors_list` | `app.py:1600` | 赞助位列表 JSON（供合并后 monitor 页 AJAX 加载，需 admin） |
| `/monitor/api` | GET | `monitor_api` | `app.py:1649` | 监控页数据（`?days=1..90`，需 admin） |
| `/monitor/api/search` | GET | `monitor_search_api` | `app.py:1660` | 搜索词统计（热门搜索 Top-N + 近期搜索，需 admin） |
| `/monitor/api/search/funnel` | GET | `monitor_search_funnel_api` | `app.py:1676` | 搜索→点击漏斗（需 admin） |
| `/monitor/api/events` | GET | `monitor_events_api` | `app.py:1732` | 用户行为事件统计（近 N 天事件量/类型分布，需 admin） |

### Admin 写操作（需 admin，POST）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/admin/sponsors` | POST | `admin_upsert_sponsor` | `app.py:1608` | 新建/更新赞助位 |
| `/admin/sponsors/<slot_id>/toggle` | POST | `admin_toggle_sponsor` | `app.py:1618` | 上下架切换 |
| `/admin/sponsors/<slot_id>/delete` | POST | `admin_delete_sponsor` | `app.py:1627` | 删除赞助位 |

## SEO 路由

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/robots.txt` | GET | `robots` | `app.py:1379` | 爬虫规则（SEO 关 → 禁止索引） |
| `/sitemap.xml` | GET | `sitemap` | `app.py:1399` | 站点地图（**主语言 en，2026-09-05 P4**：首页 `/?lang=en`、词条 `/term/<slug>?lang=en`、`/hf?lang=en`、`/terms` 裸 URL；词条仅达标词 `term_row_indexable`，上限 `SITEMAP_MAX_URLS`） |
| `/favicon.ico` | GET | `favicon` | `app.py:1443` | favicon |
| `/favicon.svg` | GET | `favicon_svg` | `app.py:1452` | favicon SVG |
| `/apple-touch-icon.png` | GET | `apple_touch_icon` | `app.py:1464` | Apple 触屏图标 |
| `/og-image.png` | GET | `og_image` | `app.py:1476` | Open Graph 分享图（动态生成，SEO 任务 11） |

## 错误处理

| 类型 | 函数 | 行号 | 功能 |
|------|------|------|------|
| 404 | `not_found` | `app.py:846` | 简单 HTML + `noindex`，防爬虫索引不存在的 term 页 |

## 鉴权机制

- `admin_required` 装饰器（`app.py:1550`）：`ADMIN_TOKEN` 未设 → 所有 `/admin/*` 返回 404（隐身）。
- token 来源优先级：`Authorization: Bearer` → `?token=` → `session["admin_token"]`。
- `hmac.compare_digest` 防时序攻击；未登录页面请求 → 跳登录页（带 next），API 请求 → 401。
- `/monitor` 与 `/admin` 共用同一 `admin_required`。
