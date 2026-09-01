# 前端模板索引

> 8 个 Jinja2 模板，用途 / 区块 / 行号 / API 引用。配合 [INDEX.md](../INDEX.md) 使用。

## 通用机制

- **主题**：`localStorage["aitw_theme"]` → `document.documentElement.dataset.theme`（dark/light），各页都有 `#theme-btn` 切换按钮。CSS `[data-theme="light"]` 覆盖暗色默认。
- **i18n**（`index.html`、`term_detail.html`、`search.html`）：首页 `I18N` 对象（zh/en 双版本，`index.html:468`）+ `t(k)` 翻译函数（`index.html:514`）+ `LANG` 状态（`index.html:529`，SSR 注入 `default_lang`，可被 localStorage/`?lang=` 覆盖）；详情页和搜索页由服务端 `lang` 直接渲染对应语言。
- **SSR 数据注入**（仅 `index.html`）：`<script id="sponsor-data" type="application/json">`（`index.html:459`）+ `<script id="initial-terms-data">`（`index.html:460`）。
- **SEO**：`term_detail.html` 含最多 4 段 `application/ld+json` 结构化数据（`term_detail.html:159,170,191,206`：DefinedTerm + ItemList 通用词；SoftwareApplication + ScholarlyArticle 仅 HF 词）；`index.html` 也有 ld+json（`index.html:309,318`）。

---

## templates/index.html  （1372 行）— 首页主单页（词视图为主）

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 46 | 读 localStorage 设 data-theme |
| 🤗 HF 入口（header 右侧 `#hf-link`） | 327 | 与语言/主题按钮同排的 `.btn` 链接；href/title 由前端 `updateHfLink()`（563）跟随 `LANG` 动态生成，语言切换即时同步 |
| 样式 `<style>` | ~54–286 | 暗色默认 + light 覆盖 + 卡片/词卡/赞助位/响应式 |
| SEO ld+json | 285, 294 | 结构化数据 |
| AdSense 脚本 | 314 | `adsbygoogle.js`（`adsense_enabled` 时） |
| SSR 数据注入 | 435, 436 | sponsor-data / initial-terms-data（词卡） |
| 主 JS `<script>` | 455–1291 | 全部前端逻辑 |
| ├ i18n 定义 | 445 | `I18N` zh/en 双版本（含 view_words/view_news/more_btn） |
| ├ `t(k)` | 491 | 翻译函数 |
| ├ `LANG`/`currentView` 状态 | 501–506 | `currentView=words\|news`，URL/localStorage 记忆 |
| ├ URL 状态恢复 | 534–558 | `?view=&cat=&sort=&lang=` 可分享 |
| ├ 视图切换 seg | 790 | 「🔤热词 / 📰逐条新闻」，`#view-seg` |
| ├ 词卡渲染 `renderWordCard` | 929 | 词名链详情页 + origin 徽标 + hot/rise/novelty + top-3 报道 + `.word-actions`（「展开更多」按钮条件出现，「查看热词」恒为 `word-detail-link link-btn official` 带框样式，两态一致） |
| ├ 词卡展开 `toggleWordExpand` | 983 | 按需拉 `/api/word/<term>` 全量报道，独立 AbortController |
| ├ `visibleData` | 1097 | words 视图成员资格分类过滤；news 视图保留服务端排序 |
| ├ `render` | 1113 | words 走 renderWordCard / news 走 renderCard，赞助每 8 卡插 1 |
| ├ 数据拉取 `fetchAll` | 1185 | `fetchJSON("/api/stream?lang=&sort=&view=")` |
| ├ AbortController | 1172 | `_fetchCtrl`，切语言/排序/视图时 abort 旧请求 |
| ├ 返回滚动恢复 | 1236–1292 | 点击 `/term/` 链接前记 `window.scrollY` 到 sessionStorage（`aitw_last_scroll`）；后退/前进（back_forward）或经词条页「返回首页」链接（`scroll_back=1`）回首页时恢复——head 脚本（55，首帧前加 `scroll-restoring` 隐藏内容）→ SSR 首屏立即落位 → `/api/stream` 全量渲染后校准并 `finalizeScrollRestore` 消费 key + 清理 URL 标记，全程无顶部闪现；**词链接 `termHref`（917）携带当前非默认 view/sort/cat，词条页 `home_url` 回显（app.py:755）——返回后榜单状态不丢失（20260901 #7 边界修复）** |
| └ Mock 数据 | 1304 | 后端不可用时的内置预览数据 |

**引用 API**：`/api/stream`（主数据，`?view=words\|news`）、`/api/word/<term>`（词展开）、`/api/click/<slot_id>`（赞助位点击）。
**渲染路由**：`/`（`app.py:633`）。
**SSR 首屏**：`initial_terms` 注入词卡（词名 + top-3 报道，爬虫可见），随后异步拉 `/api/stream?view=words` 全量替换。

---

## templates/hf.html  （355 行）— HuggingFace 独立排序页（开源动向）

| 区块 | 行号 | 说明 |
|------|------|------|
| pipeline_tag → 中文标签映射 | 9–31 | `pipe_emoji` / `pipe_zh` + `pipe_label` 宏：主徽标用可读名称（文生图/文本生成…）而非原始 key |
| 主题初始化 JS | 70 | 读 localStorage 设 data-theme（防闪烁） |
| 样式 `<style>` | 78–206 | 暗色默认 + light 覆盖 + 排序 pill / 模型卡 / 响应式 |
| SEO ld+json | 209 | ItemList（模型榜 Top-20，爬虫可见） |
| 排序切换 pill | 257–262 | `?sort=trending\|likes\|downloads` 普通链接（服务端切换，零 fetch）+ 缓存更新时间 |
| 模型卡列表 | 266–303 | 排名 / 🤗 开源模型徽标 / pipeline_tag 主徽标 / tags / 趋势分·点赞·下载 / 官方 + 社区链接 / 相关论文 |
| 主 JS `<script>` | 311–353 | 主题切换 / 搜索跳转 `/search` / 更新时间渲染 |

**引用 API**：无（服务端 `_hf_models_for`（`app.py:888`）装配后 SSR；数据与 `/api/hf` 同一来源）。
**渲染路由**：`/hf`（`app.py:910`）。
**数据源**：`tracker.get_model_cards`（trending 文件缓存）→ 冷启动回退 `tracker.get_terms`（自带快速兜底，只抓 HF ~1s）；likes/downloads 在内存重排。

---

## templates/terms.html  （371 行）— 服务条款页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 22 | 同通用机制 |
| 样式 | ~36–110 | + `.lang-switch` |
| 语言切换 | 114 | `.lang-switch` 按钮 |
| 英文内容 | 120 | `#lang-en` |
| 中文内容 | 234 | `#lang-zh`（默认 hidden） |
| 切换 JS | 352 | `<script>` 双区块显隐 |

**引用 API**：无（静态文案）。
**渲染路由**：`/terms`（`app.py:768`）。

---

## templates/term_detail.html  （310 行）— 通用热词聚合页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 39 | 读 localStorage 设 data-theme |
| 词头（名称/来源徽标/热度/环比/报道数） | ~232–239 | `word.term` 通用字段 |
| 词解释块（💡 双语） | ~240–243 | `{% if word.term.explain %}` 条件渲染，样式 `.term-explain`（109）。服务端三级取词（静态词典 → terms 表 LLM 解释 → 数据化模板兜底），恒非空，每个热词页都有解释 |
| 词元信息行 term-meta | ~245–252 | hot/rise/报道数/首次出现 |
| HF 区块（官方/社区/论文/标签，条件渲染） | ~252–279 | `{% if word.hf %}`，live 数据 `word.hf_detail` |
| 相关报道列表（SSR，SEO 主体） | ~281–297 | `word.news` 聚合卡 |
| SEO ld+json | 159, 170, 191, 206 | 四个块：DefinedTerm（159）/ ItemList（170）/ SoftwareApplication（191）/ ScholarlyArticle（206）。SoftwareApplication 无 aggregateRating（likes 非评分，GSC 范围报错修复） |

**引用 API**：无（服务端 `_word_detail`（`app.py:132`）同步装配，进程内 TTL 缓存）。
**渲染路由**：`/term/<name>`（`app.py:712`）。
**数据源**：词池 `terms` 表命中（任何词有页）→ 报道聚合；未命中回退 HF live；再无 → 404。

---

## templates/admin.html  （353 行）— 赞助位管理后台

| 区块 | 行号 | 说明 |
|------|------|------|
| 样式 + 表单 | ~1–227 | 赞助位列表 + 编辑表单 |
| 主 JS | 228 | `<script>` |
| ├ upsert | 275 | `fetch("/admin/sponsors", {method:POST, body})` |
| ├ toggle | 290 | `fetch("/admin/sponsors/<id>/toggle", {method:POST})` |
| ├ delete | 298 | `fetch("/admin/sponsors/<id>/delete", {method:POST})` |
| └ stats | 307 | `fetch("/admin/stats")` |

**引用 API**：`/admin/sponsors`、`/admin/sponsors/<id>/{toggle,delete}`、`/admin/stats`。
**渲染路由**：`/admin`（`app.py:1427`，需 admin）。

---

## templates/search.html  （498 行）— 搜索结果页（含热词命中）

| 区块 | 行号 | 说明 |
|------|------|------|
| 热词命中卡区 | ~256–275 | `word_hits`：搜词命中热词实体时顶部渲染（词名链 `/term/<词>` + 报道数 + 热度） |
| 逐条结果 | ~276–320 | `terms`：模型/新闻卡，加权打分 + `<mark>` 高亮 |
| 建议补全 | ~300+ | `suggest` 热门搜索词 chips |

**引用 API**：`/api/search/suggest`、`/api/search/click`。
**渲染路由**：`/search`（`app.py:1172`）、SSR `word_hits` 由 `_do_search`（`app.py:1142`）返回。

---

## templates/admin_login.html  （68 行）— 管理员登录

| 区块 | 行号 | 说明 |
|------|------|------|
| 登录表单 | ~20–50 | POST token |

**引用 API**：`/admin/login`（表单 POST）。
**渲染路由**：`/admin/login`（`app.py:1403`）。

---

## templates/monitor.html  （582 行）— 流量监控页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 18–25 | 主题初始化（防闪烁，与首页同 key） |
| 样式（含 chart） | 118–132 | `.chart` 柱状图样式 |
| 主题切换 | 299–309 | `#theme-btn` |
| 30 天趋势图 | 242–247 | `#chart` + `#chart-x`（纯 CSS 柱状图，按 UV 口径） |
| 主 JS | 297 | `<script>` |
| ├ 主题切换逻辑 | 299 | |
| ├ 图表渲染 | 335, 364–386 | `chartEl`/`chartXEl`/`chartSub`；`renderChart`（UV 柱高 + 峰值 UV 副标题） |
| └ 数据拉取 | 543–579 | `fetch("/monitor/api", {headers:{Accept:application/json}})` |

**引用 API**：`/monitor/api?days=N`。
**渲染路由**：`/monitor`（`app.py:1469`，需 admin）。
**数据**：PV/UV/地域分布（来自 `visits` 表，`store.monitor_stats`）。
