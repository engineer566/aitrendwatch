---
name: git-merge-doc-line-refs
description: 多 worktree 分支合并后，docs 索引行号会被 auto-merge 静默写错（教训）
metadata:
  node_type: memory
  type: feedback
  originSessionId: 0fe0a26e-af5d-4e7a-9c51-e2200b44db43
---

合并两个同改一个模块的 worktree 分支（如 glm-switch + memory-opt 都改 `dims.py`）时，`docs/INDEX.md` / `docs/index/modules.md` 等索引里的**非冲突 hunk 行号会被 git 静默保留某一侧的旧值**（例如公开函数表沿用了 memory-opt 的行号 939/973，但合并后真实文件因叠加另一分支代码已偏移到 1032/1066）。只有文本重叠处才会显式冲突，行号漂移不会报冲突。

**Why**：本项目规则要求 docs 索引 `file:行号` 锚点与源码同步。两个分支各自更新行号到自己的基线，3-way merge 对不重叠 hunk 直接取一侧，产物是「两套代码合并 + 其中一侧的行号」，行号必然失真。

**How to apply**：合并完多分支后，**不要只解冲突**，要拿合并后真实文件重核被合并文件的索引：`wc -l` + `grep -n '^def '` 逐一比对 `docs/INDEX.md`、`docs/index/modules.md`、`docs/index/architecture.md`、`docs/index/data_flow.md` 里的全部 `file.py:行号` 引用，尤其公开函数表、分区边界、模块级常量锚点。验证方式：临时 `DATA_DIR`/`CACHE_DIR` + 无 key 跑 `terms.refresh_words`/`dims.enrich_with_llm` 断言降级路径（`dimension==default_dim`、`title_zh==原标题`、`summary_zh==前30字`），再 Flask test client 打首页/API。

相关：[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)、[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)
