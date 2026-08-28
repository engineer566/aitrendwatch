# aitrendwatch 开发守则

> 本文件是项目级 agent 守则，所有在本仓库工作的 agent（含 worktree 隔离开发）
> 启动时自动加载，须严格遵守。

## LLM 调用纪律（重要）

本项目 `dims.py` 调用 DeepSeek 做 AI 事件维度打标 + 双语翻译。
**未设置 `DEEPSEEK_API_KEY` 时，`dims` 自动走降级**（用 RSS 源 `default_dim`
分类、双 slot 填原标题），功能正常但无翻译/无精确摘要，**零 token 消耗**。
这是天然的 Mock 机制，不需要任何代码开关。

- **worktree 开发 / 单特性测试**：**严禁设置 `DEEPSEEK_API_KEY`**。
  跑 `python app.py` 或单元测试时，`dims` 走 Mock 降级，不打 DeepSeek。
- **dev 分支完整回归验证**：才在 `.env` 填 `DEEPSEEK_API_KEY` 真调 LLM。
- **生产**：`.env` 填 `DEEPSEEK_API_KEY`，按定点 13/19/01/07 刷新。

任何 agent 在 worktree 里不得为了「让摘要/翻译出现」而临时注入真实 API key。
验证 dims 逻辑时，断言降级路径即可（`dimension == default_dim`、
`title_zh == 原标题`、`summary_zh == 原标题前 30 字`）。

## 代码索引（降低 token 理解成本）

项目在 `docs/INDEX.md` 维护了一套多级代码索引，**agent 进场应先读它**，
用「模块速查表」+「按任务跳转表」定位目标模块/函数/路由，再按需精读单文件，
避免逐文件全量扫描。L2 索引按主题分文件：`docs/index/architecture.md`（架构/
数据流/后台预热）、`api_routes.md`（37 条路由）、`modules.md`（6 模块函数）、
`frontend.md`（6 模板）、`data_flow.md`（外部源/SQLite/环境变量）。

- 索引条目均带 `file:行号` 锚点，可直接跳读对应源码段。
- **`vendor/`（~13 万行 vendored 依赖）永远不读、不索引、不改**——读它是巨量 token 噪音。
- 同理 `cache/`、`data/`、`__pycache__/` 是运行产物/字节码，不读。
- 改动代码后顺手更新对应索引条目，保持行号与描述同步。

## 工作流约定

- 多特性并行开发：各 agent 在独立 worktree（`.claude/worktrees/` 或
  `using-git-worktrees` skill 约定的目录）分支上开发，互不干扰。
- 开发完毕各自提 PR / 合并到 `dev`；在 `dev` 上设 key 做完整回归验证。
- `main` 为生产分支，仅从验证通过的 `dev` 合入。
