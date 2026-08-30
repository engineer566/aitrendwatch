---
name: hot-aggregator-aitrendwatch
description: AI 热词追踪页核心架构、arXiv 检索关键经验、工作流约定
metadata:
  node_type: memory
  type: project
  originSessionId: 9940b4e2-c608-4fb4-9867-93b67017b944
---

`~/Projects/aitrendwatch`（原名 `web2`，2026-08-24 改名）是 **AI 热词追踪网页**。

**当前架构（2026-08-28 词维度重构后）**：主页以「词」为第一维度（词视图默认）——热词池 = DeepSeek 新闻抽词（每卡 1-3 个关键词；无 key 降级 `terms.extract_keywords_dict` 词典匹配）+ HF 模型名归并（`terms._hf_canon` 底模键归并变体）；三榜在词维度重构：**热度** = Σ 近 7 天关联报道 score + HF likes、**上升** = 活动量环比（`term_snapshots` 周期快照 (term,cycle)）、**最新 = 新奇度**（新词/罕见词发现，fresh(first_seen)×rarity(mentions)，非时间序）。词卡内嵌 top-3 报道可展开（按需拉 `/api/word/<term>`），保留「逐条新闻」tab（`/api/stream?view=news` 旧逻辑零回归）。分类体系改为 6 类：模型与技术/产品与应用/研究与论文/商业与投融资/政策与行业/其他。

**核心文件**：`app.py`（路由 + `_word_detail` 词聚合装配，`/api/stream?view=words|news` `/api/word/<term>` `/term/<name>`；**旧 `/api/trending` `/api/top` `/api/term/<name>` 已删**）、`terms.py`（**新增**：词典 `_LEXICON` 版本感知词边界 "GPT-5.5"≠gpt-5、`refresh_words` 词聚合+三榜打分+快照写 `words.json`、`backfill_history` 词典回填 CLI `python terms.py backfill [--force]`）、`tracker.py`（HF/arXiv 数据源）、`dims.py`（RSS+DeepSeek 打标/翻译/**抽词**，刷新锁内调 refresh_words）、`news_store.py`（`news_cards` 加 `keywords` JSON 列 + 维度旧值迁移）、`templates/index.html`（词卡/逐条双视图 + 展开）、`templates/term_detail.html`（通用热词聚合页）、`templates/search.html`（热词命中卡区）。

**三榜口径要点**：rise 环比冷启动 `ln(1+m)`；first_seen 自愈（存档日期不再被关联卡锚定时用最早报道回填）；backfill `--force` 全量重算并清空残留 keywords（非 force 幂等只处理空行）。

**arXiv 检索经验（2026-08 实测）**：

- API `https://export.arxiv.org/api/query` 必须 HTTPS；请求间隔需 ≥3s，否则 429（无 Retry-After，触发后约 3 分钟冷却才恢复，重试无用）。`tracker.py` 用 `_arxiv_throttle()` 限速。
- 检索用**族名不加引号**、去量化/规模/次版本后缀（Qwen3.8-27B→Qwen3，LTX-2.5→LTX）；全名检索返回 0 篇。
- 三重过滤保相关性：标题词边界匹配族名 + 分类限 `cs.|eess.|stat.ML`（剔除物理同名论文）+ 标题去重。只对 `ARXIV_ENRICH_LIMIT=8` 个热词检索（8×3s≈24s）。
- 前置过滤器 `_search_query_for`：去量化后缀（`-gguf|fp8|mlx|awq|...|abliterated`）→ 去规模后缀 → 去次版本（`(\d+)\.\d+$`，FLUX.1 保留）。
- `get_term_detail` 须 trending+likes **双榜合并**匹配模型，否则 trending 榜新热词详情报「未找到」。
- 旧多源聚合可用性：百度热搜/B站/知乎/抖音/HN Firebase API/GitHub Trending 可用（直连免 key）；微博需登录态不可用；社区聚合 API（vvhan/tenapi 等）网络不可达；HF 走 `hf-mirror.com` 镜像。

**Why**：arXiv 限速恢复期、检索语法、三重过滤策略都是反复试错得来，省得重新踩坑。

**How to apply**：调 arXiv 先确保 ≥3s 间隔；检索用族名不加引号；保相关性靠标题词边界 + CS 分类双过滤；详情接口须双榜匹配；HF 相关一律走镜像。

版本状态（2026-08-30 更新）：词维度重构 `e68b232` 已合入 dev 并以 release 1.0.0（`f237b26`）发布；后续 release 1.1.0（glm-switch 提供方切换 + memory-opt 内存优化，`5915705`）与生产 max-requests 修复（`8fd4476`）已上线；i18n 中英文页面分离（`52b3659`）已合入 dev；`codex/fixes-20260829`（流排序/HTML 实体/历史新闻/View 链接等 5 个修复）已合入 dev 并经测试机验证，随 **release 1.2.0（2026-08-30）** 发布。`main` 为生产分支，`dev` 验证后并入。**LLM 纪律**：worktree 严禁设 `DEEPSEEK_API_KEY`，关键词走降级词典匹配（`dimension==default_dim`、`keywords==extract_keywords_dict`）即可断言。

相关：[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)、[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)、[`aitrendwatch-test-host`](aitrendwatch-test-host.md)
