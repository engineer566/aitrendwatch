# 前端模板索引

> 6 个 Jinja2 模板，用途 / 区块 / 行号 / API 引用。配合 [INDEX.md](../INDEX.md) 使用。

## 通用机制

- **主题**：`localStorage["aitw_theme"]` → `document.documentElement.dataset.theme`（dark/light），各页都有 `#theme-btn` 切换按钮。CSS `[data-theme="light"]` 覆盖暗色默认。
- **i18n**（`index.html`、`term_detail.html`、`search.html`）：首页 `I18N` 对象（zh/en 双版本，`index.html:445`）+ `t(k)` 翻译函数（`index.html:491`）+ `LANG` 状态（`index.html:506`，SSR 注入 `default_lang`，可被 localStorage/`?lang=` 覆盖）；详情页和搜索页由服务端 `lang` 直接渲染对应语言。
- **SSR 数据注入**（仅 `index.html`）：`<script id="sponsor-data" type="application/json">`（`index.html:436`）+ `<script id="initial-terms-data">`（`index.html:437`）。
- **SEO**：`term_detail.html` 含两段 `application/ld+json` 结构化数据（`term_detail.html:126,141`）；`index.html` 也有 ld+json（`index.html:281,290`）。

---

## templates/index.html  （1276 行）— 首页主单页（词视图为主）

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 28 | 读 localStorage 设 data-theme |
| 样式 `<style>` | ~40–250 | 暗色默认 + light 覆盖 + 卡片/词卡/赞助位/响应式 |
| SEO ld+json | 281, 290 | 结构化数据 |
| AdSense 脚本 | 310 | `adsbygoogle.js`（`adsense_enabled` 时） |
| SSR 数据注入 | 436, 437 | sponsor-data / initial-terms-data（词卡） |
| 主 JS `<script>` | 438–1198 | 全部前端逻辑 |
| ├ i18n 定义 | 445 | `I18N` zh/en 双版本（含 view_words/view_news/more_btn） |
| ├ `t(k)` | 491 | 翻译函数 |
| ├ `LANG`/`currentView` 状态 | 501–506 | `currentView=words\|news`，URL/localStorage 记忆 |
| ├ URL 状态恢复 | 534–558 | `?view=&cat=&sort=&lang=` 可分享 |
| ├ 视图切换 seg | 790 | 「🔤热词 / 📰逐条新闻」，`#view-seg` |
| ├ 词卡渲染 `renderWordCard` | 887 | 词名链详情页 + origin 徽标 + hot/rise/novelty + top-3 报道 + 展开按钮 |
| ├ 词卡展开 `toggleWordExpand` | 940 | 按需拉 `/api/word/<term>` 全量报道，独立 AbortController |
| ├ `visibleData` | 1054 | words 视图成员资格分类过滤；news 视图保留服务端排序 |
| ├ `render` | 1070 | words 走 renderWordCard / news 走 renderCard，赞助每 8 卡插 1 |
| ├ 数据拉取 `fetchAll` | 1142 | `fetchJSON("/api/stream?lang=&sort=&view=")` |
| ├ AbortController | 1128–1130 | `_fetchCtrl`，切语言/排序/视图时 abort 旧请求 |
| └ Mock 数据 | 1200 | 后端不可用时的内置预览数据 |

**引用 API**：`/api/stream`（主数据，`?view=words\|news`）、`/api/word/<term>`（词展开）、`/api/click/<slot_id>`（赞助位点击）。
**渲染路由**：`/`（`app.py:590`）。
**SSR 首屏**：`initial_terms` 注入词卡（词名 + top-3 报道，爬虫可见），随后异步拉 `/api/stream?view=words` 全量替换。

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
**渲染路由**：`/terms`（`app.py:693`）。

---

## templates/term_detail.html  （310 行）— 通用热词聚合页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 28 | |
| 词头（名称/来源徽标/热度/环比/报道数） | ~180–200 | `word.term` 通用字段 |
| 词解释块（💡 双语） | ~238–241 | `{% if word.term.explain %}` 条件渲染，样式 `.term-explain`（109），未收录词不显示 |
| 相关报道列表（SSR，SEO 主体） | ~210–225 | `word.news` 聚合卡 |
| HF 区块（官方/社区/论文/标签，条件渲染） | ~185–210 | `{% if word.hf %}`，live 数据 `word.hf_detail` |
| SEO ld+json | ~135–185 | DefinedTerm + ItemList（通用词）；SoftwareApplication + ScholarlyArticle（HF 词） |

**引用 API**：无（服务端 `_word_detail`（`app.py:103`）同步装配，进程内 TTL 缓存）。
**渲染路由**：`/term/<name>`（`app.py:656`）。
**数据源**：词池 `terms` 表命中（任何词有页）→ 报道聚合；未命中回退 HF live；再无 → 404。

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
**渲染路由**：`/admin`（`app.py:1256`，需 admin）。

---

## templates/search.html  （498 行）— 搜索结果页（含热词命中）

| 区块 | 行号 | 说明 |
|------|------|------|
| 热词命中卡区 | ~256–275 | `word_hits`：搜词命中热词实体时顶部渲染（词名链 `/term/<词>` + 报道数 + 热度） |
| 逐条结果 | ~276–320 | `terms`：模型/新闻卡，加权打分 + `<mark>` 高亮 |
| 建议补全 | ~300+ | `suggest` 热门搜索词 chips |

**引用 API**：`/api/search/suggest`、`/api/search/click`。
**渲染路由**：`/search`（`app.py:1001`）、SSR `word_hits` 由 `_do_search`（`app.py:971`）返回。

---

## templates/admin_login.html  （60 行）— 管理员登录

| 区块 | 行号 | 说明 |
|------|------|------|
| 登录表单 | ~20–50 | POST token |

**引用 API**：`/admin/login`（表单 POST）。
**渲染路由**：`/admin/login`（`app.py:1232`）。

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
**渲染路由**：`/monitor`（`app.py:1298`，需 admin）。
**数据**：PV/UV/地域分布（来自 `visits` 表，`store.monitor_stats`）。
