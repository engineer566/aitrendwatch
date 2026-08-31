# 动态 LLM 维护词典（词池即词典 + 解释资产化）设计

> 日期：2026-08-31　状态：已确认　实现分支：`worktree-feat-dynamic-lexicon`

## 背景与问题

- 生产热词池 60 词中 **43 个（72%）是词典外词**（LLM 从新闻抽取的新词，如 GLM-5.3-Flash、Ai训练、Openclaw），其详情页**无解释块**。
- 根因：解释只存在于静态 `_EXPLANATIONS`（93 条人工词条，挂在 `_LEXICON` canonical 上），词典外词查不到 → 页面按设计不渲染。
- 需求原文：「每个热词的点开页面添加对本热词的解释」。用户决策：**热词及其解释均为网站资产**，随刷新由 LLM 持续维护优化。

## 设计目标

1. **每个热词详情页都有解释**（含无 key / LLM 失败环境）。
2. **词池（SQLite `terms` 表）即动态词典**：解释随刷新自我维护（新词生成、旧解释低频优化）。
3. **解释对用户更有价值**：面向普通访客，定义 + 时效价值（结合关联报道），持续优化。
4. **抽词提示词优化**：抽取高价值实体/概念，热词池质量提升。

## 架构

### 数据层（terms.py）

- `terms` 表幂等新增列：`explain_zh TEXT`、`explain_en TEXT`、`explain_updated_at TEXT`（ISO 时间，幂等 ALTER，与 `display_en` 同模式）。
- `get_term_explanation(term, lang)` 改为**三级取词**：
  1. 静态 `_EXPLANATIONS`（人工精编，最高优先，存量不改）；
  2. `terms` 表 `explain_zh/explain_en`（LLM 生成/优化）；
  3. 未命中返回空串（调用方模板兜底）。
- `refresh_words(..., term_explainer=None)` 新增回调（与 `term_translator` 同模式），在 `_refresh_words_inner` 步骤 6（写 terms 主表）后执行**解释批次**：
  - 收集 `kept` 中**非静态词典词**（canon ∉ `_EXPLANATIONS`）：
    - **新词/无解释**（库内 explain 为空）→ 生成；
    - **已有解释且 `explain_updated_at` 距今 >24h** → 优化（附现有解释 + 最新代表报道标题作上下文）。
  - 批量调 `term_explainer` → 结果回写：
    - 文本变化或新词 → `UPDATE terms SET explain_zh, explain_en, explain_updated_at`；
    - 文本未变化（LLM 判定已足够优）→ 仅更新 `explain_updated_at`（标记已检查，保持 ≤1 次/天/词）。
  - 静态词典词不进入批次（「存量词不做更改」）。
  - 无 key / 回调为 None / 任一步失败 → 静默跳过，不影响刷新主流程。

### 解释生成（dims.py）

- 新增 `explain_terms(contexts)`（复用 `_translate_terms` 骨架：`_active_llm()` 故障转移链、`llm_reasoning_params`、`_llm_success/_llm_failure`）。
- 提示词要求：面向普通访客，**①一句话定义 ②为什么值得关注**（结合给定代表报道标题）；中英双语；已有解释时**只有明显更优才返回新文本，否则原样返回**；禁止编造。

### 抽词提示词优化（dims.py `_USER_PREFIX` 关键词段）

- 抽取 1-3 个**高价值实体/概念**：具体模型/产品/公司/组织名、核心技术（RAG/MoE/长上下文）、事件主体；
- **禁止**泛化词（AI、模型、技术、公司、行业、数据等单独出现）、纯形容词/动词；
- 英文术语规范拼写、中文概念用中文。

### 详情页（app.py + term_detail.html）

- `_word_detail` 的 `explain`：三级取词结果为空时，生成**数据化模板兜底**（用 term_info 已有数据）：
  - zh：「X」是近期 AI 热点词，与 N 篇相关报道关联（+ 热度/上升，如有）；
  - en：对应英文；
  - legacy_hf 分支：HF 社区热推模型文案。
- `term_detail.html` 不变（`{% if word.term.explain %}` 渲染；兜底后恒非空）。

### 测试（无 LLM key 降级路径）

- 更新 `tests/test_term_explanation.py`：无解释词现在渲染**兜底解释块**（原「不渲染」断言改为「渲染兜底」）。
- 新增 `tests/test_dynamic_lexicon.py`：
  - mock `term_explainer`：新词写库 → `get_term_explanation` 返回 LLM 解释；静态词典词不进批次；
  - 已有解释 <24h 不进批次、>24h 进批次（上下文含现有解释）；
  - 返回文本未变化 → 内容不重写但 `explain_updated_at` 更新；
  - 无 key / 回调 None → 全程降级不抛异常；
  - 提示词模板断言（禁泛化词规则、解释价值规则）。

## 成本与频率

- 首次部署：一次性为存量无解释词生成（约 43 词，1-2 批）；
- 之后：每轮刷新仅处理「新词 + 超过 24h 未检查的词」，单词解释调用 ≤1 次/天；
- 无 key 环境零调用。

## 验证路径

worktree 全量测试（无 key）→ 合并 dev → 测试环境部署 → 等一轮 GLM 刷新 → 验证词典外词有解释 → 生产部署 → 公网验证（抽查之前无解释的词，如 GLM-5.3-Flash、Ai训练）。
