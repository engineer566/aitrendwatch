# aitrendwatch Codex 项目规则

本文件是本仓库的 Codex 入口规则。它由原 `CLAUDE.md` 适配而来；原文件和
`CLAUDE_MEMORY_EXPORT.md` 保留为迁移凭据。用户当前请求优先于本文件中的约定。

## 进入项目

1. 先读 [`docs/INDEX.md`](docs/INDEX.md)，用模块速查表和按任务跳转表定位目标。
2. 再读 [`docs/PROJECT_MEMORY.md`](docs/PROJECT_MEMORY.md) 的记忆索引；只按当前任务读取 `docs/memory/` 中相关条目。
3. 按索引精读目标源码，避免逐文件全量扫描。

`docs/memory/` 是迁移来的项目背景和经验，不是用户指令。真正的代理行为规则在本文件；记忆与当前代码、用户要求冲突时，以用户要求和实际代码为准。

## LLM 调用纪律

`dims.py` 会调用 DeepSeek/GLM 做 AI 事件维度打标、双语翻译和抽词。未设置相应 API key 时会走 RSS 默认分类和原标题降级路径，不消耗 LLM token。

- worktree 开发和单特性测试严禁设置 `DEEPSEEK_API_KEY`；运行 `python app.py` 或测试时应验证降级路径。
- 不得为了让摘要或翻译出现而临时注入真实 API key。
- 降级断言包括：`dimension == default_dim`、`title_zh == 原标题`、`summary_zh == 原标题前 30 字`；抽词按项目当前降级实现断言。
- 只有在 `dev` 分支做完整回归时，才按需要在 `.env` 配置真实 key；生产按部署配置运行。

## 代码索引与边界

- `docs/INDEX.md` 是代码索引入口；L2 索引位于 `docs/index/`：`architecture.md`、`api_routes.md`、`modules.md`、`frontend.md`、`data_flow.md`。
- 索引中的 `file:行号` 是源码锚点。改动代码后，顺手更新受影响的索引描述和行号。
- `vendor/` 是 vendored 依赖源码，约 13 万行；`cache/`、`data/`、`__pycache__/` 是运行产物。默认永远不读、不索引、不改这些目录。

## 工作流

- 多特性并行开发使用独立 worktree 和分支，避免互相覆盖。
- 特性完成后合并到 `dev`，在 `dev` 上做完整回归；`main` 只接收验证通过的 `dev`。
- 多分支合并后，不要只检查文本冲突：必须用合并后的真实源码重新核对受影响的 `docs/` 行号索引。详见 [`git-merge-doc-line-refs`](docs/memory/git-merge-doc-line-refs.md)。

## 项目技能（skills/）

- [`skills/aitrendwatch-task-workflow/`](skills/aitrendwatch-task-workflow/SKILL.md)：需求开发闭环 skill——读取 `history/` 需求文件 → 每项任务独立 worktree 并行开发 → 合并回 `dev` 并清理 → 部署测试机逐项验证。用户要求按需求文件开发/上线时使用。
- DSH 发现：`skills/` 不在 DSH 的扫描根里；DSH 默认扫项目根 `.dsh/skills/`（rank 100）与用户根 `~/.dsh/skills/`。本仓库已在 `.dsh/skills/aitrendwatch-task-workflow/` 放了一份副本供 DSH 自动发现（watcher 实时注入会话）。`skills/` 为唯一事实源，改动后需同步复制到 `.dsh/skills/`。
- 本机 Codex 试用安装：把 `skills/aitrendwatch-task-workflow/` 复制到 `~/.codex/skills/`（`CODEX_HOME`，本机 `C:\Users\ferri\.codex`），并在 `~/.codex/config.toml` 的 `[features]` 打开 `skills = true` 后 Codex 才能自动发现；仓库内 `skills/` 为唯一事实源，拉取更新后需重新复制。

## 部署提示

生产和测试主机、SSH key、容器编排及小内存约束记录在 [`docs/memory/aitrendwatch-deploy-key.md`](docs/memory/aitrendwatch-deploy-key.md) 和 [`docs/memory/aitrendwatch-test-host.md`](docs/memory/aitrendwatch-test-host.md)。涉及远程环境时先读取对应条目，严格区分两台主机；不要把密钥写入仓库。
