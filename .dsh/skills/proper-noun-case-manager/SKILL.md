---
name: proper-noun-case-manager
description: Use when aitrendwatch 项目中专有名词/技术缩写/品牌名的大小写显示需要统一修正（如 "Gpu"→"GPU"、"openai"→"OpenAI"），或需要把新的专有名词大小写规则同步到测试/生产环境并永久生效。
---

# 专有名词大小写固定管理

管理 aitrendwatch 项目中所有专有名词（技术缩写、品牌名、产品名）的大小写固定组合写法，确保前端展示、词卡、详情页中的词名显示符合行业规范。

## 核心机制

项目已具备大小写归一化基础设施（`terms.py`）：
- `_UPPER_ACRONYMS`：技术缩写统一大写（GPU/UI/API/JSON 等）
- `_LEXICON` + `_ALIAS`：词典归并（openai → OpenAI）
- `_display_of()`：展示名选择逻辑
- `normalize_term()`：canonical 键生成

**本 skill 解决的是：当上述机制覆盖不到或需要覆盖已有规则时，如何安全地添加新规则并同步到所有环境。**

## 适用场景

| 场景 | 处理方式 |
|------|----------|
| 新缩写未收录（如新出的 "NPU" 显示为 "npu"） | 加到 `_UPPER_ACRONYMS` |
| 品牌名大小写错误（如 "openai" 应显示 "OpenAI"） | 加到 `_LEXICON` + 调 `_display_of` 逻辑 |
| 已有规则冲突（如某词同时命中两条规则） | 调优先级或合并规则 |
| 多环境同步（dev→test→prod） | 用本 skill 的同步流程 |

## 配置规范

所有专有名词大小写规则集中管理在 **`config/terms_canonical.json`**：

```json
{
  "_meta": {
    "version": "1.0",
    "updated_at": "2026-09-02T10:00:00+08:00",
    "description": "专有名词大小写固定规则表"
  },
  "upper_acronyms": {
    "npu": "NPU",
    "sdk": "SDK",
    "ide": "IDE"
  },
  "lexicon_display": {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic"
  },
  "display_overrides": {
    "gpt-5": "GPT-5",
    "gpt-4o": "GPT-4o"
  }
}
```

字段说明：
- `upper_acronyms`：技术缩写 → 大写 canonical（对应 `_UPPER_ACRONYMS`）
- `lexicon_display`：词典词 → 指定展示名（对应 `_LEXICON` canonical 键的显示形式）
- `display_overrides`：强制覆盖 `_display_of()` 输出（最高优先级）

## 修改流程

### 1. 本地 dev 分支修改

```bash
# 确保在 dev 分支
git checkout dev
git pull --ff-only origin dev

# 编辑规则文件
vim config/terms_canonical.json

# 同步到 terms.py（运行同步脚本）
python scripts/sync_terms_canonical.py
```

同步脚本会：
1. 读取 `config/terms_canonical.json`
2. 更新 `terms.py` 中的 `_UPPER_ACRONYMS`、`_LEXICON`、`_display_of` 逻辑
3. 生成变更摘要

### 2. 本地验证

```bash
# 运行相关测试
python -m pytest tests/test_acronym_normalize.py -v
python -m pytest tests/test_case_insensitive.py -v

# 冒烟测试
python app.py &
curl http://localhost:5000/api/stream?view=words | head -20
```

### 3. 提交到 dev 分支

```bash
git add config/terms_canonical.json terms.py tests/test_count_and_failover.py scripts/sync_terms_canonical.py
git commit -m "fix: 专有名词大小写规则更新（xxx/yyy）"
```

### 4. 部署到测试环境

```bash
# 方式 A：rsync（本机有 rsync 时）
rsync -avz --checksum -e "ssh -i /home/wuyuming/Projects/test_host.pem" \
  terms.py config/terms_canonical.json \
  root@47.98.124.167:/opt/aitrendwatch/

# 方式 B：git archive（Windows 本机无 rsync 时）
git -c core.autocrlf=false archive dev | \
  ssh -i /home/wuyuming/Projects/test_host.pem root@47.98.124.167 \
  "cd /opt/aitrendwatch && tar -x --wildcards 'terms.py' 'config/terms_canonical.json'"

# 重启容器（模板/配置换 inode 后必须 --force-recreate 生效）
ssh -i /home/wuyuming/Projects/test_host.pem root@47.98.124.167 \
  "cd /opt/aitrendwatch && docker compose -f docker-compose.test.yml up -d --force-recreate"
```

### 5. 测试环境验证

访问 `http://47.98.124.167:8080` 检查：
- 热词榜中目标词的显示名是否正确
- 词卡点击后的详情页标题是否正确
- `/api/stream?view=words` 返回的 JSON 中 `term` 字段

**注意**：容器重启后缓存清空，需等待 15-25 分钟 dims 刷新完成。如需立即验证，可手动更新 `terms` 表 display 列并重新生成 `words.json`：

```bash
# SSH 进入测试机后执行
ssh -i /home/wuyuming/Projects/test_host.pem root@47.98.124.167
docker exec aitrendwatch python -c "
import sqlite3
conn = sqlite3.connect('/app/data/news.db')
cursor = conn.cursor()
updates = [('GLM', 'GLM'), ('GPU', 'GPU'), ('AWS', 'AWS'), ('MoE', 'MoE'),
           ('openai', 'OpenAI'), ('huggingface', 'Hugging Face'),
           ('chatgpt', 'ChatGPT'), ('aqua', 'AQuA'),
           ('ross-harness', 'ROSS Harness'), ('deepseek', 'DeepSeek'),
           ('agents.md', 'AGENTS.md'), ('openclaw', 'OpenClaw')]
for term, display in updates:
    cursor.execute('UPDATE terms SET display = ? WHERE term = ?', (display, term))
conn.commit()
conn.close()
"
```

### 6. dev → main 合并

测试验证通过后：

```bash
git checkout main
git merge --no-ff dev -m "Merge dev: 专有名词大小写规则更新"
```

### 7. 部署到生产环境

```bash
# 生产部署（与测试机同构，但用 docker-compose.prod.yml）
rsync -avz --checksum -e "ssh -i ~/Projects/work.pem" \
  terms.py config/terms_canonical.json \
  root@47.89.243.229:/opt/aitrendwatch/

ssh -i ~/Projects/work.pem root@47.89.243.229 \
  "cd /opt/aitrendwatch && docker compose -f docker-compose.prod.yml up -d --force-recreate"
```

### 8. 生产环境验证

- 生产验证：`https://aitrendwatch.top/api/stream?view=words`
- 检查目标词的 `term` 字段显示是否正确
- 生产同样需等待 dims 刷新（定点 1/7/13/19）或手动更新 `terms` 表

## 存量数据清理

修改规则后，**已有词池数据不会自动更新**（`terms` 表中的 `display` 列是刷新时写入的）。需要：

1. **等待自然刷新**：下次 dims 刷新（定点 1/7/13/19）时会重新计算 display
2. **手动触发刷新**（如需立即生效）：
   ```bash
   # 测试机/生产机内执行
   docker exec aitrendwatch python -c "
   import terms, dims
   # 触发一次完整刷新
   dims.start_background_dims_refresher()
   "
   ```
3. **直接改库**（紧急时，容器内执行）：
   ```bash
   docker exec aitrendwatch python -c "
   import sqlite3
   conn = sqlite3.connect('/app/data/news.db')
   cursor = conn.cursor()
   cursor.execute(\"UPDATE terms SET display='NPU' WHERE term='npu'\")
   conn.commit()
   conn.close()
   "
   ```
   
   **批量更新多个词示例**：
   ```bash
   docker exec aitrendwatch python -c "
   import sqlite3
   conn = sqlite3.connect('/app/data/news.db')
   cursor = conn.cursor()
   updates = [
       ('GLM', 'GLM'), ('GPU', 'GPU'), ('AWS', 'AWS'), ('MoE', 'MoE'),
       ('openai', 'OpenAI'), ('huggingface', 'Hugging Face'),
       ('chatgpt', 'ChatGPT'), ('aqua', 'AQuA'),
       ('ross-harness', 'ROSS Harness'), ('deepseek', 'DeepSeek'),
       ('agents.md', 'AGENTS.md'), ('openclaw', 'OpenClaw'),
   ]
   for term, display in updates:
       cursor.execute('UPDATE terms SET display = ? WHERE term = ?', (display, term))
   conn.commit()
   print('Updated', cursor.rowcount, 'rows')
   conn.close()
   "
   ```

## 常见错误

| 错误 | 原因 | 修复 |
|------|------|------|
| 修改后词仍显示旧大小写 | 缓存未刷新（words.json / terms 表） | 等下次刷新或手动触发 |
| 测试机生效但生产不生效 | 生产容器未 `--force-recreate` | bind mount 换 inode 必须 recreate |
| 新规则与旧规则冲突 | `_UPPER_ACRONYMS` 与 `_LEXICON` canonical 键不一致 | 统一用 `terms_canonical.json` 管理，同步脚本保证一致性 |
| 缩写被误判为普通词 | 缩写长度 ≤2 被 `normalize_term` 过滤 | 确保缩写长度 ≥2，或在 `extract_keywords_dict` 中特殊处理 |

## 相关文件

- `terms.py` — `_UPPER_ACRONYMS`、`_LEXICON`、`_display_of()`
- `config/terms_canonical.json` — 本 skill 管理的规则配置
- `scripts/sync_terms_canonical.py` — 配置→代码同步脚本
- `tests/test_acronym_normalize.py` — 缩写归一测试
- `docs/memory/aitrendwatch-test-host.md` — 测试机详情
- `docs/memory/aitrendwatch-deploy-key.md` — 生产机详情
