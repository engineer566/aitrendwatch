# 路由索引

> 全部 37 条路由（含 errorhandler），按功能分组。每条带 `app.py:行号` 便于跳读。
> 配合 [INDEX.md](../INDEX.md) 使用。

## 页面路由（HTML）

| 路径 | 方法 | 函数 | 行号 | 功能 | 备注 |
|------|------|------|------|------|------|
| `/` | GET | `index` | `app.py:472` | 首页主单页 | 记 PV/visit/曝光，SSR 首批热词，前端 JS 再拉 `/api/stream` |
| `/term/<path:term_name>` | GET | `term_detail` | `app.py:542` | 单热词详情页（SEO 长尾） | 进程内 TTL 缓存（1800s）；未找到 → 404 |
| `/terms` | GET | `terms` | `app.py:571` | 服务条款页（中英双语） | 静态文案，`SITE_TERMS_UPDATED` 常量 |
| `/admin/login` | GET,POST | `admin_login` | `app.py:773` | 管理员登录 | `ADMIN_TOKEN` 未设 → 404 隐身 |
| `/admin` | GET | `admin_home` | `app.py:797` | 赞助位管理后台 | 需 admin |
| `/monitor` | GET | `monitor` | `app.py:839` | 流量监控页 | 需 admin，只读 PV/UV/地域 |

## 数据 API（JSON）

### 通用热点聚合（旧主功能，8 直连源）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/sources` | GET | `api_sources` | `app.py:497` | 所有源元信息（`SOURCE_META`） |
| `/api/hot/<source>` | GET | `api_hot` | `app.py:502` | 单源热点（带硬性超时 `SOURCE_DEADLINE`） |
| `/api/all` | GET | `api_all` | `app.py:507` | 并发聚合所有 8 源 |

### 热词追踪（tracker 层）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/trending` | GET | `api_trending` | `app.py:524` | 7 日上升最快热词（HF trendingScore） |
| `/api/top` | GET | `api_top` | `app.py:530` | 热度最高热词（HF likes） |
| `/api/term/<path:term_name>` | GET | `api_term` | `app.py:536` | 单热词详情 JSON（官方链接+社区+论文） |

### 维度事件层（dims 层）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/api/dims` | GET | `api_dims` | `app.py:600` | 按 AI 维度分组的热点卡；`?dimension=` `?lang=zh/en` |
| `/api/stream` | GET | `api_stream` | `app.py:608` | **统一卡片流**（model+news 合并），前端主数据源；`?lang=` `?sort=rise/hot/new` |

### 系统

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/health` | GET | `health` | `app.py:648` | 健康检查 |
| `/api/click/<path:slot_id>` | GET | `sponsor_click` | `app.py:744` | 赞助位点击计数 + 302 跳转 |
| `/admin/stats` | GET | `admin_stats` | `app.py:832` | 赞助位 30 天统计（需 admin） |
| `/monitor/api` | GET | `monitor_api` | `app.py:845` | 监控页数据（`?days=1..90`，需 admin） |

### Admin 写操作（需 admin，POST）

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/admin/sponsors` | POST | `admin_upsert_sponsor` | `app.py:804` | 新建/更新赞助位 |
| `/admin/sponsors/<slot_id>/toggle` | POST | `admin_toggle_sponsor` | `app.py:814` | 上下架切换 |
| `/admin/sponsors/<slot_id>/delete` | POST | `admin_delete_sponsor` | `app.py:823` | 删除赞助位 |

## SEO 路由

| 路径 | 方法 | 函数 | 行号 | 功能 |
|------|------|------|------|------|
| `/robots.txt` | GET | `robots` | `app.py:655` | 爬虫规则（SEO 关 → 禁止索引） |
| `/sitemap.xml` | GET | `sitemap` | `app.py:675` | 站点地图（首页 + 热词详情页，上限 `SITEMAP_MAX_URLS`） |
| `/favicon.ico` | GET | `favicon` | `app.py:714` | favicon |
| `/favicon.svg` | GET | `favicon_svg` | `app.py:723` | favicon SVG |
| `/apple-touch-icon.png` | GET | `apple_touch_icon` | `app.py:735` | Apple 触屏图标 |

## 错误处理

| 类型 | 函数 | 行号 | 功能 |
|------|------|------|------|
| 404 | `not_found` | `app.py:586` | 简单 HTML + `noindex`，防爬虫索引不存在的 term 页 |

## 鉴权机制

- `admin_required` 装饰器（`app.py:754`）：`ADMIN_TOKEN` 未设 → 所有 `/admin/*` 返回 404（隐身）。
- token 来源优先级：`Authorization: Bearer` → `?token=` → `session["admin_token"]`。
- `hmac.compare_digest` 防时序攻击；未登录页面请求 → 跳登录页（带 next），API 请求 → 401。
- `/monitor` 与 `/admin` 共用同一 `admin_required`。
