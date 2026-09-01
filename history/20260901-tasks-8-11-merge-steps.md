# 20260901 需求 8~11 点合并与验证步骤

## 前置条件

1. 确保 4 个 subagent 已完成并提交更改
2. 确保主仓库在 `dev` 分支且工作区干净
3. 确保已 fetch 最新的远程代码

## 合并步骤

### 1. 切换到 dev 分支并确保干净

```bash
cd /home/wuyuming/Projects/aitrendwatch
git checkout dev
git status  # 应显示 clean working tree（除了 history 文件）
git fetch origin
git pull --ff-only origin dev
```

### 2. 依次合并 4 个分支

使用提供的合并脚本：

```powershell
.\skills\aitrendwatch-task-workflow\scripts\merge-and-cleanup.ps1 -Branches codex/task8-case-normalize,codex/task9-analytics,codex/task10-merge-admin,codex/task11-seo
```

或者手动合并：

```bash
git merge --no-ff codex/task8-case-normalize -m "Merge branch 'codex/task8-case-normalize' into dev — Normalize common acronyms to uppercase (GPU/UI/GLM/etc.)"
git merge --no-ff codex/task9-analytics -m "Merge branch 'codex/task9-analytics' into dev — Add user behavior tracking with event logging"
git merge --no-ff codex/task10-merge-admin -m "Merge branch 'codex/task10-merge-admin' into dev — Merge monitor and admin pages into unified dashboard"
git merge --no-ff codex/task11-seo -m "Merge branch 'codex/task11-seo' into dev — Comprehensive SEO optimization with meta tags and structured data"
```

### 3. 解决冲突（如有）

如果合并过程中出现冲突：
1. 手工解决冲突
2. `git add <resolved files>`
3. `git commit` 完成合并
4. 用剩余分支重跑合并脚本

### 4. 重核索引行号

合并后必须重核 `docs/INDEX.md` 和 `docs/index/*.md` 中的 `file:行号` 引用：

```bash
# 检查各文件的行数
wc -l app.py config.py dims.py tracker.py terms.py store.py news_store.py stream_utils.py text_utils.py version.py

# 检查函数定义行号
grep -n '^def ' app.py config.py dims.py tracker.py terms.py store.py news_store.py stream_utils.py text_utils.py version.py

# 对比 docs/INDEX.md 和 docs/index/*.md 中的行号引用
# 如有不一致，手动更新索引文件
```

### 5. 本地回归测试

```bash
# 确保没有设置 DEEPSEEK_API_KEY
unset DEEPSEEK_API_KEY

# 运行 pytest
pytest -v

# 冒烟测试
python app.py &
curl http://localhost:5000/health
curl http://localhost:5000/api/stream?view=words | head -100
kill %1
```

### 6. 清理 worktree 和分支

如果合并没有使用 `-SkipCleanup`，脚本会自动清理。否则手动清理：

```bash
git worktree remove ~/.codex/worktrees/aitw-task8-case-normalize/aitrendwatch --force
git worktree remove ~/.codex/worktrees/aitw-task9-analytics/aitrendwatch --force
git worktree remove ~/.codex/worktrees/aitw-task10-merge-admin/aitrendwatch --force
git worktree remove ~/.codex/worktrees/aitw-task11-seo/aitrendwatch --force

git branch -d codex/task8-case-normalize
git branch -d codex/task9-analytics
git branch -d codex/task10-merge-admin
git branch -d codex/task11-seo

git worktree prune
```

## 部署到测试机

### 1. 导出 LF 干净树

```bash
# Windows 本机（或 Linux/Mac）
git -c core.autocrlf=false archive dev | tar -xf - -C /tmp/aitrendwatch-export
```

### 2. SCP 变更文件到测试机

识别本次合并变更的文件：

```bash
git diff --name-only origin/dev..dev
```

SCP 变更文件：

```bash
# Python 文件
scp -i /data/test_host.pem /tmp/aitrendwatch-export/*.py root@47.98.124.167:/opt/aitrendwatch/

# 模板文件
scp -i /data/test_host.pem /tmp/aitrendwatch-export/templates/*.html root@47.98.124.167:/opt/aitrendwatch/templates/

# 如果有新增模块，必须在测试机的 docker-compose.test.yml 中补挂载
# SSH 到测试机编辑 docker-compose.test.yml
ssh -i /data/test_host.pem root@47.98.124.167 "vi /opt/aitrendwatch/docker-compose.test.yml"
```

### 3. 重启容器

```bash
ssh -i /data/test_host.pem root@47.98.124.167 "cd /opt/aitrendwatch && docker compose -f docker-compose.test.yml up -d --force-recreate"
```

### 4. 等待预热完成

容器重启后约 15-25 分钟完成预热刷新，以 `cache/words.json` 更新为标志：

```bash
ssh -i /data/test_host.pem root@47.98.124.167 "cd /opt/aitrendwatch && watch ls -la cache/"
```

## 逐项验证

见 `history/20260901-tasks-8-11-verification.md` 中的详细验证清单。

### 快速验证命令

```bash
# 任务 8：检查大小写统一
curl http://47.98.124.167:8080/api/stream?view=words | grep -i '"term"' | head -20

# 任务 9：检查事件 API
curl -X POST http://47.98.124.167:8080/api/event -H "Content-Type: application/json" -d '{"event_type":"test","event_data":{"action":"click"}}'

# 任务 10：检查合并后的页面
curl http://47.98.124.167:8080/monitor | grep -E "<title|<h1"

# 任务 11：检查 SEO meta 标签
curl http://47.98.124.167:8080/ | grep -E "<(title|meta name=\"description\"|meta property=\"og:)"
```

## 修复问题（如有）

如果验证中发现任何问题：
1. 在 dev 分支直接修复
2. 提交更改：`git add . && git commit -m "fix: <描述>"`
3. 重新部署变更文件
4. 复验

## 推送到远程 dev

验证全部通过后：

```bash
# 核对待推提交
git log --oneline origin/dev..dev

# 推送
git push origin dev
```

## 收尾

1. 更新 `docs/memory/` 部署记录（版本/内容/验证结果/注意）
2. 是否合入 `main` 上生产由用户决定
