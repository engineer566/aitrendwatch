# aitrendwatch 项目记忆

这是从 `CLAUDE_MEMORY_EXPORT.md` 导入并适配给 Codex 的项目记忆索引。

- 导出时间：2026-08-29
- 来源：原 Claude Code 项目记忆
- 记忆条目：5 条

`AGENTS.md` 是代理规则入口；本文件和 `docs/memory/` 是项目背景资料。进入项目先读本索引，再按当前任务读取对应条目。

## 迁移说明

- 来源：`CLAUDE_MEMORY_EXPORT.md`（2026-08-29 导出，5 条记忆 + 1 个索引；该导出文件迁移完成后已从仓库移除，内容以本目录为准）。
- 适配：`[[wiki-link]]` 改为仓库内 markdown 链接；原文「CLAUDE.md 强制」类表述改为「项目规则要求」（本项目入口为 `AGENTS.md`）。
- 未迁移：导出第一节的 Claude Code 专属恢复指引（重建 `/root/.claude/projects/...` 记忆目录的步骤），与本仓库 Codex 记忆无关，保留在导出文件中。
- 缺失文件：导出提到仓库根目录另有 `MIGRATION_NOTES.md`（2026-08-28，基于 v0.3.0 commit `f084127` 的「新服务器迁移笔记」），本仓库中不存在该文件，其内容未迁移；如需要可向用户索取原文件。
- 时效：记忆内容定格在导出日（2026-08-29）；个别版本状态条目已过期，以当前代码、`docs/INDEX.md` 和实际部署为准。

## 记忆条目

| 名称 | 类型 | 适用场景 |
|---|---|---|
| [`hot-aggregator-aitrendwatch`](memory/hot-aggregator-aitrendwatch.md) | project | 核心架构、词聚合、arXiv 检索、开发工作流 |
| [`aitrendwatch-server-stability`](memory/aitrendwatch-server-stability.md) | feedback | 1.6G 内存 OOM、gunicorn、多进程刷新锁、内存优化 |
| [`aitrendwatch-deploy-key`](memory/aitrendwatch-deploy-key.md) | reference | 生产主机 SSH、部署、版本和运行时注意事项 |
| [`aitrendwatch-test-host`](memory/aitrendwatch-test-host.md) | reference | 测试主机 SSH、部署、挂载和公网访问 |
| [`aitrendwatch-regression-checklist`](memory/aitrendwatch-regression-checklist.md) | reference | **上线前必过**的核心回归测试清单（自动化 pytest + 手工/线上逐项验证） |
| [`git-merge-doc-line-refs`](memory/git-merge-doc-line-refs.md) | feedback | 多 worktree 合并后的索引行号复核 |

## 按任务读取

- 改词聚合、LLM 抽词、arXiv 或榜单：读 `hot-aggregator-aitrendwatch`。
- 改后台刷新、gunicorn、锁、缓存扫描或小内存部署：读 `aitrendwatch-server-stability`。
- 生产部署：读 `aitrendwatch-deploy-key`。
- **上线生产前回归**：读 `aitrendwatch-regression-checklist`，逐项过清单（每次上线必做）。
- 测试机部署或容器挂载：读 `aitrendwatch-test-host`。
- 合并多个改动同一模块的分支：读 `git-merge-doc-line-refs`。

## 记忆关系

```text
hot-aggregator-aitrendwatch
├── aitrendwatch-server-stability
│   └── aitrendwatch-deploy-key
│       └── aitrendwatch-test-host
├── aitrendwatch-regression-checklist   # 上线前必过（deploy-key 部署前读取）
└── git-merge-doc-line-refs
```

记忆中的主机信息和历史版本是迁移上下文，不代表当前部署状态；执行部署前应以用户授权和现场检查为准。
