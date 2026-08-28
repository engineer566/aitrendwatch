# 前端模板索引

> 6 个 Jinja2 模板，用途 / 区块 / 行号 / API 引用。配合 [INDEX.md](../INDEX.md) 使用。

## 通用机制

- **主题**：`localStorage["aitw_theme"]` → `document.documentElement.dataset.theme`（dark/light），各页都有 `#theme-btn` 切换按钮。CSS `[data-theme="light"]` 覆盖暗色默认。
- **i18n**（仅 `index.html`）：`I18N` 对象（zh/en 双版本，`index.html:371`）+ `t(k)` 翻译函数（`index.html:410`）+ `LANG` 状态（`index.html:424`，SSR 注入 `default_lang`，可被 localStorage/`?lang=` 覆盖）。`terms.html` 用 `#lang-en`/`#lang-zh` 双区块切换。
- **SSR 数据注入**（仅 `index.html`）：`<script id="sponsor-data" type="application/json">`（`index.html:363`）+ `<script id="initial-terms-data">`（`index.html:364`）。
- **SEO**：`term_detail.html` 含两段 `application/ld+json` 结构化数据（`term_detail.html:126,141`）；`index.html` 也有 ld+json（`index.html:248,257`）。

---

## templates/index.html  （970 行）— 首页主单页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 28 | 读 localStorage 设 data-theme |
| 样式 `<style>` | ~40–230 | 暗色默认 + light 覆盖 + 卡片/赞助位/响应式 |
| SEO ld+json | 248, 257 | 结构化数据 |
| AdSense 脚本 | 277 | `adsbygoogle.js`（`adsense_enabled` 时） |
| SSR 数据注入 | 363, 364 | sponsor-data / initial-terms-data |
| 主 JS `<script>` | 365–969 | 全部前端逻辑 |
| ├ i18n 定义 | 371 | `I18N` zh/en 双版本 |
| ├ `t(k)` | 410 | 翻译函数 |
| ├ `LANG` 状态 | 424 | SSR 注入 `default_lang`，localStorage/`?lang=` 覆盖 |
| ├ URL 状态恢复 | 427 | `?cat=&sort=&lang=` 可分享 |
| ├ 赞助位渲染 | ~540–560 | 读 `#sponsor-data`，点击 `/api/click/<slot_id>`（`index.html:553`） |
| ├ 数据拉取 | 838–902 | **核心**：`fetchJSON("/api/stream?lang=&sort=")` |
| ├ AbortController | 856 | `_fetchCtrl`，切语言/排序时 abort 旧请求 |
| └ 百度联盟脚本 | 967 | `cpro.baidustatic.com`（`baidu_ads_enabled` 时） |

**引用 API**：`/api/stream`（主数据）、`/api/click/<slot_id>`（赞助位点击）。
**渲染路由**：`/`（`app.py:472`）。
**SSR 首屏**：`initial_terms` 注入少量卡（`SSR_INITIAL_LIMIT`），随后异步拉 `/api/stream` 全量替换。

---

## templates/terms.html  （363 行）— 服务条款页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 22 | 同通用机制 |
| 样式 | ~36–110 | + `.lang-switch` |
| 语言切换 | 114 | `.lang-switch` 按钮 |
| 英文内容 | 120 | `#lang-en` |
| 中文内容 | 234 | `#lang-zh`（默认 hidden） |
| 切换 JS | 352 | `<script>` 双区块显隐 |

**引用 API**：无（静态文案）。
**渲染路由**：`/terms`（`app.py:571`）。

---

## templates/term_detail.html  （209 行）— 单热词详情页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 28 | |
| 官方链接/社区讨论/论文 | ~40–125 | 服务端渲染 `term` 数据 |
| SEO ld+json | 126, 141 | 结构化数据（2 段） |

**引用 API**：无（服务端 `tracker.get_term_detail` 同步渲染）。
**渲染路由**：`/term/<name>`（`app.py:542`）。

---

## templates/admin.html  （345 行）— 赞助位管理后台

| 区块 | 行号 | 说明 |
|------|------|------|
| 样式 + 表单 | ~1–227 | 赞助位列表 + 编辑表单 |
| 主 JS | 228 | `<script>` |
| ├ upsert | 275 | `fetch("/admin/sponsors", {method:POST, body})` |
| ├ toggle | 290 | `fetch("/admin/sponsors/<id>/toggle", {method:POST})` |
| ├ delete | 298 | `fetch("/admin/sponsors/<id>/delete", {method:POST})` |
| └ stats | 307 | `fetch("/admin/stats")` |

**引用 API**：`/admin/sponsors`、`/admin/sponsors/<id>/{toggle,delete}`、`/admin/stats`。
**渲染路由**：`/admin`（`app.py:797`，需 admin）。

---

## templates/admin_login.html  （60 行）— 管理员登录

| 区块 | 行号 | 说明 |
|------|------|------|
| 登录表单 | ~20–50 | POST token |

**引用 API**：`/admin/login`（表单 POST）。
**渲染路由**：`/admin/login`（`app.py:773`）。

---

## templates/monitor.html  （390 行）— 流量监控页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 10 | |
| 样式（含 chart） | 36–130 | `.chart` 柱状图样式 |
| 主题切换 | 175, 217 | `#theme-btn` |
| 30 天趋势图 | 193–195 | `#chart` + `#chart-x`（纯 CSS 柱状图） |
| 主 JS | 215 | `<script>` |
| ├ 主题切换逻辑 | 221 | |
| ├ 图表渲染 | 253 | `chartEl`/`chartXEl` |
| └ 数据拉取 | 367 | `fetch("/monitor/api", {headers:{Accept:application/json}})` |

**引用 API**：`/monitor/api?days=N`。
**渲染路由**：`/monitor`（`app.py:839`，需 admin）。
**数据**：PV/UV/地域分布（来自 `visits` 表，`store.monitor_stats`）。
