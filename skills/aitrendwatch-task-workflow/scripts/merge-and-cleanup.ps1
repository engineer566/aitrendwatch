<#
.SYNOPSIS
  aitrendwatch 需求闭环：把一批 codex/taskN-<slug> worktree 分支依次 --no-ff 合并进 dev，
  成功后删除对应 worktree 与分支（把「合并后清理」纪律机械执行，避免遗留 worktree/分支）。

.DESCRIPTION
  - 分支命名约定：codex/taskN-<slug>；worktree 路径约定：<WorktreeRoot>/aitw-taskN-<slug>/aitrendwatch
  - 任一分支合并冲突时立即停止（不做任何清理），由 agent 手工解决并 commit 后，带剩余分支重跑
  - 默认合并全部成功后执行清理；-SkipCleanup 只合并不清理（供行号重核/回归通过后再清）
  - -DryRun 只打印将执行的命令，不落地

.EXAMPLE
  .\skills\aitrendwatch-task-workflow\scripts\merge-and-cleanup.ps1 -Branches codex/task1-language,codex/task2-stopwords

.EXAMPLE
  .\skills\aitrendwatch-task-workflow\scripts\merge-and-cleanup.ps1 -Branches codex/task1-language -SkipCleanup -DryRun
#>
param(
  [Parameter(Mandatory = $true)][string[]]$Branches,
  [string]$TargetBranch = 'dev',
  [string]$WorktreeRoot = (Join-Path $env:USERPROFILE '.codex\worktrees'),
  [switch]$SkipCleanup,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repo = git rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0) { throw '当前不在 git 仓库内' }
Push-Location $repo

function Invoke-Git([string[]]$Args) {
  if ($DryRun) { Write-Host "[dry-run] git $($Args -join ' ')"; return }
  & git @Args
  if ($LASTEXITCODE -ne 0) { throw "git $($Args -join ' ') 失败（exit $LASTEXITCODE）" }
}

# --- 前置检查 ---
$cur = git branch --show-current
if ($cur -ne $TargetBranch) { throw "当前分支是 $cur，需先 checkout $TargetBranch" }
$dirty = @(git status --porcelain)
if ($dirty.Count -gt 0) { throw '工作区不干净，先提交或 stash' }

# --- 1) 按顺序合并 ---
foreach ($b in $Branches) {
  $msg = "Merge branch '$b' into $TargetBranch"
  Write-Host "==> 合并 $b"
  if ($DryRun) { Write-Host "[dry-run] git merge --no-ff $b -m `"$msg`""; continue }
  & git merge --no-ff $b -m $msg
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "合并 $b 失败（可能有冲突）。手工解决并 commit 后，用剩余分支重跑本脚本。未做任何清理。"
    Pop-Location
    exit 1
  }
}

# --- 2) 合并后必做提醒 ---
Write-Host ''
Write-Host '==> 合并完成。接下来必做（勿跳）：'
Write-Host '  1) 重核 docs/INDEX.md 与 docs/index/*.md 的 file:行号 锚点（git-merge-doc-line-refs 教训）'
Write-Host '  2) dev 全量回归：pytest（严禁 DEEPSEEK_API_KEY，断言降级路径）'
Write-Host '  3) 修复/索引改动在 dev 提交后，再执行清理'

# --- 3) 清理 worktree 与分支 ---
if ($SkipCleanup) { Write-Host '==> -SkipCleanup：跳过清理。'; Pop-Location; return }
foreach ($b in $Branches) {
  $name = $b -replace '^codex/', ''
  $wt = Join-Path $WorktreeRoot "aitw-$name\aitrendwatch"
  if (Test-Path $wt) {
    Write-Host "==> 删除 worktree $wt"
    Invoke-Git @('worktree', 'remove', '--force', $wt)
  }
  else {
    Write-Host "==> worktree 不存在（跳过）：$wt"
  }
  Write-Host "==> 删除分支 $b"
  Invoke-Git @('branch', '-D', $b)
}
Invoke-Git @('worktree', 'prune')
Write-Host '==> 清理完成。'
Pop-Location
