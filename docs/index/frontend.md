# 前端模板索引

> 8 个 Jinja2 模板，用途 / 区块 / 行号 / API 引用。配合 [INDEX.md](../INDEX.md) 使用。

## 通用机制

- **主题**：`localStorage["aitw_theme"]` → `document.documentElement.dataset.theme`（dark/light），各页都有 `#theme-btn` 切换按钮。CSS `[data-theme="light"]` 覆盖暗色默认。head 最前的主题初始化脚本（在 `<style>` 之前）同时给 `<html>` 设内联背景/文字色（`#f5f6f8`/`#1c2130` 或 `#0f1117`/`#e6e8ee`），避免亮色用户首帧「先暗后亮」闪烁；`#theme-btn` 切换时同步更新内联色（20260901 #12）。
- **i18n**（`index.html`、`term_detail.html`、`search.html`）：首页 `I18N` 对象（zh/en 双版本，`index.html:504`）+ `t(k)` 翻译函数（`index.html:550`）+ `LANG` 状态（`index.html:563`，SSR 注入 `default_lang`，可被 localStorage/`?lang=` 覆盖）；详情页和搜索页由服务端 `lang` 直接渲染对应语言。
- **SSR 数据注入**（仅 `index.html`）：`<script id="sponsor-data" type="application/json">`（`index.html:492`）+ `<script id="initial-terms-data">`（`index.html:493`）+ `<script id="initial-dimensions-data">`（`index.html:494`）+ `<script id="initial-dimension-counts-data">`（`index.html:495`）。
- **SEO**：全站统一 meta 标签体系（title/description/keywords/OG/Twitter Card/og:image）；`term_detail.html` 含最多 4 段 `application/ld+json` 结构化数据（`term_detail.html:164,175,196,211`：DefinedTerm + ItemList 通用词；SoftwareApplication + ScholarlyArticle 仅 HF 词）；`index.html` 有 WebSite+SearchAction + ItemList ld+json（`index.html:325,339`）；`hf.html` 有 CollectionPage + ItemList ld+json（`hf.html:216,228`）；搜索页 `noindex,follow` 防重复索引。所有页面引用 `/og-image.png` 社交分享图。

---

## templates/index.html  （1621 行）— 首页主单页（词视图为主）

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 14 | 读 localStorage 设 data-theme + `<html>` 内联背景/文字色（防首帧闪烁，head 最前） |
| 返回滚动恢复 head 脚本 | 82–86 | 首帧前加 `scroll-restoring` 隐藏内容（back_forward 恢复路径） |
| 🤗 HF 入口（header 右侧 `#hf-link`） | 355 | 与语言/主题按钮同排的 `.btn` 链接；href/title 由前端 `updateHfLink()`（676）跟随 `LANG` 动态生成，语言切换即时同步 |
| 样式 `<style>` | ~80–311 | 暗色默认 + light 覆盖 + 卡片/词卡/赞助位/响应式 |
| SEO ld+json | 325, 339 | 结构化数据 |
| AdSense 脚本 | 359 | `adsbygoogle.js`（`adsense_enabled` 时） |
| 视图切换 seg | 400 | 「🔤热词 / 📰逐条新闻」，`#view-seg` |
| SSR 数据注入 | 492–495 | sponsor-data / initial-terms-data（词卡） / initial-dimensions-data / initial-dimension-counts-data（维度元数据） |
| 主 JS `<script>` | 496–1564 | 全部前端逻辑 |
| ├ i18n 定义 | 504 | `I18N` zh/en 双版本（含 view_words/view_news/more_btn） |
| ├ `t(k)` | 550 | 翻译函数 |
| ├ `LANG`/`currentView` 状态 | 563 | `currentView=words\|news`，URL/localStorage 记忆 |
| ├ 埋点 `Analytics` | 623 | 埋点系统 v3（任务 9）：`track(eventType, eventData)`（658）→ `POST /api/event`（批量兼容），白名单事件类型；词卡展开/查看热词/视图切换/语言切换/排序切换/维度筛选/搜索 全链路埋点 |
| ├ `updateHfLink` | 676 | HF 入口链接跟随 LANG |
| ├ URL 状态恢复 | ~580–620 | `?view=&cat=&sort=&lang=` 可分享 |
| ├ 分类条 `renderCatBar` | 901 | 按后端 `dimensionList` 顺序渲染维度 pill；标准维度计数为 0 也保留，SSR 阶段用注入的维度计数稳定化 |
| ├ 词链接 `termHref` | 1044 | 携带当前非默认 view/sort/cat（20260901 #7 边界修复） |
| ├ 词卡渲染 `renderWordCard` | 1056 | 词名链详情页 + origin 徽标 + hot/rise/novelty + top-3 报道 + `.word-actions`（「展开更多」按钮条件出现，「查看热词」恒为 `word-detail-link link-btn official` 带框样式，两态一致） |
| ├ 词卡展开 `toggleWordExpand` | 1110 | 按需拉 `/api/word/<term>` 全量报道，独立 AbortController |
| ├ news 卡 `renderCard` | 1153 | 逐条新闻视图卡片渲染 |
| ├ `visibleData` | 1225 | words 视图成员资格分类过滤；news 视图保留服务端排序 |
| ├ `render` | 1241 | words 走 renderWordCard / news 走 renderCard，赞助每 8 卡插 1 |
| ├ `_fetchCtrl` | 1300 | 当前 /api/stream 请求的 AbortController，切语言/排序/视图时 abort 旧请求 |
| ├ 数据拉取 `fetchAll` | 1337 | `fetchJSON("/api/stream?lang=&sort=&view=")`；全量就位先 `unlockCatCounts()` 再 render，分类条一次更新到全量计数 |
| ├ 返回滚动恢复 `finalizeScrollRestore` | 1461 | 消费 `aitw_last_scroll` key + 清理 URL 标记（`scroll_back=1`），校准落位；配套 head 脚本（82）+ 词条页 `home_url` 回显（app.py:756） |
| └ Mock 数据 | 1488 | 后端不可用时的内置预览数据 |
| 悬浮回到顶部按钮（需求 6 改进：文字 + 箭头 + 移动端安全区/节流） | 1564–1619 | 页尾 3 块：`.back-top` 样式（1564–1590，fixed 右下胶囊形，文字 + ↑ 箭头 span（`.bt-arrow`），CSS 变量主题自适应，hover 高亮；≤560px 收窄并叠加 `env(safe-area-inset-*)` 安全区）+ 按钮元素（1592–1596，仅 aria-label 随 `is_en` 双语「回到顶部/Back to top」，箭头 span `aria-hidden`）+ IIFE 脚本（1598–1619，`matchMedia` 窄屏阈值 250 / 宽屏 400，rAF 节流滚动加 `.show`，点击 `scrollTo` 平滑回顶；独立作用域不与既有 JS 冲突） |

**引用 API**：`/api/stream`（主数据，`?view=words\|news`）、`/api/word/<term>`（词展开）、`/api/click/<slot_id>`（赞助位点击）、`/api/event`（埋点上报，任务 9）。
**渲染路由**：`/`（`app.py:658`）。
**SSR 首屏**：`initial_terms` 注入词卡（词名 + top-3 报道，爬虫可见），随后异步拉 `/api/stream?view=words` 全量替换。

---

## templates/hf.html  （434 行）— HuggingFace 独立排序页（开源动向）

| 区块 | 行号 | 说明 |
|------|------|------|
| pipeline_tag → 中文标签映射 | 9–31 | `pipe_emoji` / `pipe_zh` + `pipe_label` 宏：主徽标用可读名称（文生图/文本生成…）而非原始 key |
| 主题初始化 JS | 38 | 读 localStorage 设 data-theme + `<html>` 内联背景/文字色（防首帧闪烁，head 最前） |
| 样式 `<style>` | 82–211 | 暗色默认 + light 覆盖 + 排序 pill / 模型卡 / 响应式 |
| SEO ld+json | 216, 228 | CollectionPage + ItemList（模型榜 Top-20，爬虫可见） |
| 排序切换 pill | 261–266 | `?sort=trending\|likes\|downloads` 普通链接（服务端切换，零 fetch）+ 缓存更新时间 |
| 模型卡列表 | 270–307 | 排名 / 🤗 开源模型徽标 / pipeline_tag 主徽标 / tags / 趋势分·点赞·下载 / 官方 + 社区链接 / 相关论文 |
| 主 JS `<script>` | 315–357 | 主题切换 / 搜索跳转 `/search` / 更新时间渲染 |

**引用 API**：无（服务端 `_hf_models_for`（`app.py:925`）装配后 SSR；数据与 `/api/hf` 同一来源）。
**渲染路由**：`/hf`（`app.py:947`）。
**数据源**：`tracker.get_model_cards`（trending 文件缓存）→ 冷启动回退 `tracker.get_terms`（自带快速兜底，只抓 HF ~1s）；likes/downloads 在内存重排。

---

## templates/terms.html  （383 行）— 服务条款页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 4 | 同通用机制（data-theme + `<html>` 内联背景/文字色，head 最前） |
| 样式 | 42–111 | + `.lang-switch` |
| 语言切换 | 127 | `.lang-switch` 按钮 |
| 英文内容 | 133 | `#lang-en` |
| 中文内容 | 247 | `#lang-zh`（默认 hidden） |
| 切换 JS | 365 | `<script>` 双区块显隐 |

**引用 API**：无（静态文案）。
**渲染路由**：`/terms`（`app.py:805`）。

---

## templates/term_detail.html  （379 行）— 通用热词聚合页

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题初始化 JS | 5 | 读 localStorage 设 data-theme + `<html>` 内联背景/文字色（防首帧闪烁，head 最前） |
| 词头（名称/来源徽标/热度/环比/报道数） | ~237–244 | `word.term` 通用字段 |
| 词解释块（💡 双语） | ~245–248 | `{% if word.term.explain %}` 条件渲染，样式 `.term-explain`（109）。服务端三级取词（静态词典 → terms 表 LLM 解释 → 数据化模板兜底），恒非空，每个热词页都有解释 |
| 词元信息行 term-meta | ~250–257 | hot/rise/报道数/首次出现 |
| HF 区块（官方/社区/论文/标签，条件渲染） | ~257–284 | `{% if word.hf %}`，live 数据 `word.hf_detail` |
| 相关报道列表（SSR，SEO 主体） | ~286–302 | `word.news` 聚合卡 |
| SEO ld+json | 164, 175, 196, 211 | 四个块：DefinedTerm（164）/ ItemList（175）/ SoftwareApplication（196）/ ScholarlyArticle（211）。SoftwareApplication 无 aggregateRating（likes 非评分，GSC 范围报错修复） |

**引用 API**：无（服务端 `_word_detail`（`app.py:134`）同步装配，进程内 TTL 缓存）。
**渲染路由**：`/term/<name>`（`app.py:749`）。
**数据源**：词池 `terms` 表命中（任何词有页）→ 报道聚合；未命中回退 HF live；再无 → 404。

---

## templates/admin.html  （353 行）— 赞助位管理后台（已废弃，保留备用）

> ⚠️ 已合并到 `monitor.html`。`/admin` 路由重定向到 `/monitor#sponsors`。
> 此文件保留作为历史参考，不再被任何路由渲染。

---

## templates/search.html  （584 行）— 搜索结果页（含热词命中）

| 区块 | 行号 | 说明 |
|------|------|------|
| 热词命中卡区 | ~261–280 | `word_hits`：搜词命中热词实体时顶部渲染（词名链 `/term/<词>` + 报道数 + 热度） |
| 逐条结果 | ~281–325 | `terms`：模型/新闻卡，加权打分 + `<mark>` 高亮 |
| 建议补全 | ~305+ | `suggest` 热门搜索词 chips |

**引用 API**：`/api/search/suggest`、`/api/search/click`。
**渲染路由**：`/search`（`app.py:1228`）、SSR `word_hits` 由 `_do_search`（`app.py:1179`）返回（2026-09-04 需求 1：news 命中在评分排序后按归一化标题去重，镜像报道只留评分高者）。

---

## templates/admin_login.html  （68 行）— 管理员登录

| 区块 | 行号 | 说明 |
|------|------|------|
| 登录表单 | ~20–50 | POST token |

**引用 API**：`/admin/login`（表单 POST）。
**渲染路由**：`/admin/login`（`app.py:1531`）。

---

## templates/monitor.html  （1052 行）— 统一管理后台（流量监控 + 赞助位管理）

| 区块 | 行号 | 说明 |
|------|------|------|
| 主题 JS | 4–15 | 主题初始化（data-theme + `<html>` 内联背景/文字色，防闪烁，与首页同 key，head 最前） |
| 样式（含 chart + tab + 赞助位表单） | 30–260 | 设计 token + 柱状图 + Tab 导航 + 赞助位管理样式 |
| Tab 导航 | ~289 | `📊 流量监控` / `📋 赞助位管理` 两个 tab，URL hash 记忆 |
| Tab 1: 流量监控面板 | ~290–400 | PV/UV 概览 + 趋势图 + 地域分布 + 访问明细 + 搜索统计 + 漏斗 + **用户行为事件统计（v3 埋点：事件总数/类型分布/每日趋势/近期明细，353–370）** |
| Tab 2: 赞助位管理面板 | ~405–530 | 统计概览 + 赞助位列表 + 新增/编辑表单（从 admin.html 迁移） |
| 主 JS | ~540 | `<script>` |
| ├ Tab 切换 `switchTab` | 488 | hash 记忆 + 懒加载赞助位数据 |
| ├ 主题切换逻辑 | ~570 | |
| ├ 流量监控渲染 | 554–745 | `renderStats`(554)/`renderChart`(575)/`renderRegions`(598)/`renderRecent`(625)/`renderSearchStats`(665)/`renderFunnel`(716) |
| ├ 用户行为事件渲染 `renderEvents` | 762 | 事件总数/类型分布（776+）/每日趋势（~790）/近期明细（~820）；`/monitor/api/events` 数据源 |
| ├ 流量监控数据拉取 `loadMonitor` | 853 | `fetch("/monitor/api")` + `/monitor/api/search` + `/monitor/api/search/funnel` + `/monitor/api/events` |
| ├ 赞助位 CRUD | 908–1010 | `editSlot`(908)/`saveSlot`(930)/`toggleSlot`(950)/`deleteSlot`(957)/`loadSponsors`(987)/`loadSponsorStats`(1002) |
| └ 自动刷新 | 1047–1049 | 60s 轮询（仅 monitor tab 激活时） |

**引用 API**：`/monitor/api?days=N`、`/monitor/api/search`、`/monitor/api/search/funnel`、`/monitor/api/events`、`/admin/sponsors/list`、`/admin/sponsors`（POST）、`/admin/sponsors/<id>/{toggle,delete}`、`/admin/stats`。
**渲染路由**：`/monitor`（`app.py:1605`，需 admin）。
**旧 `/admin` 路由**：重定向到 `/monitor#sponsors`（`app.py:1555`）。
**数据**：PV/UV/地域分布（`store.monitor_stats`）+ 用户行为事件（`store.event_stats`）+ 赞助位 CRUD（`store.list_slots`/`upsert_slot`/`toggle_slot`/`delete_slot`）。
