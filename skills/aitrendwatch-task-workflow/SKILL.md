---
name: aitrendwatch-task-workflow
description: aitrendwatch 需求开发闭环：读取 history/ 需求文件 → 每项任务建独立 worktree 分支并行开发 → 全部完成后合并回 dev 并清理 worktree/分支 → 部署测试机逐项验证，失败在 dev 修复 → 验证通过后推送到远程 dev。用户要求「按 history/ 需求文件开发/合并/上线」时使用。
---

# aitrendwatch 需求开发闭环

把 `history/YYYYMMDD.txt` 里的需求清单走完「并行开发 → 合回 dev → 测试机验证 → 推送远程 dev」的闭环。流程固定五步：

1. 读取需求文件，拆成任务清单
2. 每项任务一个 worktree + 独立分支，并行开发
3. 全部完成后在 dev 依次合并，删除 worktree 与分支
4. dev 部署测试机，逐项验证需求；有问题在 dev 修复并复验
5. 验证全部通过后把 dev 推送到远程，闭环完成

## 硬约束（违反会出事）

- **LLM 纪律**：worktree / 单特性测试严禁设置 `DEEPSEEK_API_KEY`。验证降级路径：`dimension == default_dim`、`title_zh == 原标题`、`summary_zh == 原标题前 30 字`。仅 dev 完整回归时才可在 `.env` 配真实 key。
- **进场先读** `docs/INDEX.md` → `docs/PROJECT_MEMORY.md` → 按需精读 `docs/index/*.md` 与目标源码；`vendor/`、`cache/`、`data/`、`__pycache__/` 永不读改。
- **索引同步**：改代码后顺手更新 `docs/INDEX.md` / `docs/index/*.md` 的 `file:行号` 锚点；多分支合并后必须重核行号（见 `docs/memory/git-merge-doc-line-refs.md`）。
- **远程主机**：测试机与生产机是两台独立机器，SSH key 不混用；部署细节一律以 `docs/memory/aitrendwatch-test-host.md`（测试）和 `aitrendwatch-deploy-key.md`（生产）为准。

## 步骤 1：读取需求文件

- 目标文件 `history/<YYYYMMDD>.txt`，每行一条需求（`编号. 描述`）。用户未指定日期时先向用户确认。
- 解析为任务表：编号 / 摘要 / 影响模块（查 `docs/INDEX.md` 按任务跳转表）/ 验证方式。需求含糊时先精读代码再开工。

## 步骤 2：worktree 并行开发

- 先保证主仓库在 `dev` 且干净：`git status`、`git fetch origin`、`git pull --ff-only origin dev`。
- 每项任务：`git worktree add <WT>/aitw-task<N>-<slug>/aitrendwatch -b codex/task<N>-<slug> dev`
  - `<slug>` 为英文短横线命名（沿用 `codex/task1-language` 先例）；`<WT>` 默认 `$env:USERPROFILE\.codex\worktrees`（本机 Codex 约定目录）。
- 在 worktree 内开发：先读索引定位，改动同步更新索引行号，每任务独立提交（`feat/fix/docs/perf/refactor(scope): 描述`），跑 pytest 相关用例（无 key，降级断言）。
- 各 worktree 互不干扰，可并行推进。

## 步骤 3：合并到 dev 并清理

- 主仓库 `git checkout dev` 后按任务顺序依次合并：`git merge --no-ff codex/task<N>-<slug> -m "Merge branch 'codex/task<N>-<slug>' into dev — <一句话摘要>"`；冲突就地解决（docs 冲突先例：`via HEAD/theirs`）。
- **合并后必做**：
  1. 重核行号索引：`wc -l` + `grep -n '^def '` 比对 `docs/INDEX.md`、`docs/index/*.md` 中全部 `file:行号` 引用；
  2. dev 全量回归：`pytest`（无 key 降级断言）+ 冒烟 `python app.py`；
  3. 修复/补索引改动直接在 dev 提交。
- 清理：`git worktree remove <path>`（脏则 `--force`）→ `git branch -d codex/task<N>-<slug>` → `git worktree prune`。
- 可用 `scripts/merge-and-cleanup.ps1` 机械执行「合并 + 清理」：`.\skills\aitrendwatch-task-workflow\scripts\merge-and-cleanup.ps1 -Branches codex/task1-x,codex/task2-y`（任一分支冲突时停在失败分支，解决后带剩余分支重跑；`-SkipCleanup` 只合并不清理）。

## 步骤 4：部署测试机 + 逐项验证

- 先读 `docs/memory/aitrendwatch-test-host.md`（47.98.124.167 / key `/data/test_host.pem` / `/opt/aitrendwatch` / `docker-compose.test.yml` / `0.0.0.0:8080`）。
- **测试机不是 git clone**：文件拷贝 + bind mount。新增模块必须补 `docker-compose.test.yml` 挂载，否则容器内 import 失败静默降级。
- Windows 本机（无 rsync）：`git -c core.autocrlf=false archive dev` 导出 LF 干净树 → scp 变更文件 → ssh 执行 `docker compose -f docker-compose.test.yml up -d`（必要时 `--force-recreate`）；主机有 rsync 则用 `rsync --checksum`（默认快速检查会跳过同尺寸文件）。
- **逐项验证**：按步骤 1 的任务表逐条验证，记录「需求# / 验证方式 / 结果 / 证据」；用公网 `http://47.98.124.167:8080` 页面与 `/api/*` 接口 + 容器日志。测试机 `.env` 只有 GLM key（无 DeepSeek），GLM 限流（1302/1305）是临时现象，等窗口重置，不是代码问题。
- **失败处理**：任一项不过 → 在主仓库 `dev` 直接修复提交 → 重新部署变更文件 → 复验该项及关联项。

## 步骤 5：推送到远程 dev

- 测试机逐项验证全部通过后，在主仓库把本地 `dev` 推送到远程：`git push origin dev`。
- 推送前核对：`git status` 干净、当前分支为 `dev`、`git log --oneline origin/dev..dev` 列出的待推提交与本次需求一致（含合并提交与修复提交）。
- 推送被拒（远程 dev 有新提交）时：`git fetch origin` → `git pull --rebase origin dev` 就地解决冲突 → 重跑步骤 3 的回归（pytest + 冒烟）→ 再次 `git push origin dev`。
- 推送成功后，远程 `dev` 即成为下一轮 worktree 开发（步骤 2 的 `git pull --ff-only origin dev`）与后续合入 `main` 的基准。

## 收尾

- 推送成功后按项目先例更新 `docs/memory/` 部署记录（版本/内容/验证结果/注意）。
- 是否合入 `main` 上生产由用户决定，本 skill 不自动动 `main`；需要时读 `docs/memory/aitrendwatch-deploy-key.md` 并取得授权。
