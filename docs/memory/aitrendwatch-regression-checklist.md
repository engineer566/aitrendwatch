---
name: aitrendwatch-regression-checklist
description: 每次上线生产环境前必须过全量的核心回归测试清单（自动化 + 手工验证）
metadata:
  node_type: memory
  type: reference
  originSessionId: 20260902
---

# aitrendwatch 上线前核心回归测试清单

> **触发时机**：每次准备上线生产环境（dev → main）之前，以及 dev 上做了跨模块改动后。
> 目的：任何一次上线都过同一份全量回归，避免「某功能某次上线悄悄坏掉」。
> 分两层：① 自动化（pytest，无 LLM key 降级路径，零 token）；② 手工/线上验证
> （测试机 `http://47.98.124.167:8080` 与生产 `https://aitrendwatch.top`）。

## 一、自动化回归（必跑）

在干净的 dev 分支上执行，**严禁设置 `DEEPSEEK_API_KEY` / `GLM_API_KEY`**
（降级断言即预期行为，零 token）：

```powershell
python -m pytest -q        # 全量，应全绿（当前 ~124 tests + 8 subtests）
```

覆盖矩阵（测试文件 → 回归点）：

| 测试文件 | 回归点 |
|---|---|
| `test_language.py` | 中英文路由：首页/词详情/搜索链接带 lang、`?lang=` 优先、localStorage 覆盖、SSR 卡片语言投影 |
| `test_stream_consistency.py` | `/api/stream` words/news 统一流：排序-截断稳定性、维度计数、去重身份 |
| `test_count_and_failover.py` | 2026-08-30 修复：计数/故障转移链状态机 |
| `test_dynamic_lexicon.py` | 词池即词典：解释批次生成/优化/熔断、静态词典不覆盖、无 key 降级 |
| `test_term_explanation.py` | 详情页解释三级取词（静态 → terms 表 → 模板兜底恒非空） |
| `test_term_news.py` | 词 → 关联报道：canonical/别名/标题边界匹配、按 hot 降序、同标题去重 |
| `test_dup_related_news.py` | 同标题转载去重（top news / 详情页同口径） |
| `test_case_insensitive.py` | `normalize_term` 大小写无关聚类、混合大小写 keywords 不分裂 |
| `test_stopwords.py` | 通用热词停用词（抽词/聚合/HF 三入口过滤） |
| `test_openclaw_hotword.py` | 词典外热词收录 + keywords churn 防护 + 热窗新鲜度加权（2026-09-02 新增） |
| `test_display_en_preserve.py` | 英文展示名不因 LLM 限流轮次清空（2026-09-02 新增） |
| `test_rise_display.py` | 词页 Rise 不显示 -1.00 占位（2026-09-02 新增） |
| `test_hf_page.py` | `/hf` + `/api/hf`：三排序、pipeline 中文标签、双语 SSR |
| `test_glm53_prompt.py` | GLM-5.3-Flash 提示词/思考强度（reasoning_effort）防回显/非空规则 |
| `test_html_entities.py` / `test_jsonld.py` | HTML/URL 实体双层解码；结构化数据（DefinedTerm/ItemList/SoftwareApplication 无越界评分） |
| `test_word_break.py` / `test_show_more_view_page.py` / `test_view_term_text.py` | 模板合约：英文换行不拆词、展开按钮条件、文案措辞 |
| `test_monitor_chart.py` | 监控页 30 天趋势图（UV 口径） |

## 二、手工/线上验证清单（按模块）

### 1. 数据源与缓存
- [ ] 首页 / `/api/stream?view=news` 的 `fetched_at` 是最近刷新时间（重启后应立即有预热）
- [ ] `/api/hot/<baidu|bilibili|zhihu|douyin|hackernews|github>` 各单源 ok（微博常失败属预期）
- [ ] `cache/terms.json` / `dims.json` / `words.json` 均为本次刷新产物（mtime 对应刷新时刻）
- [ ] 历史库 `news_cards` 有近 30 天数据；`/api/dims` 各维度非空

### 2. LLM 链与降级
- [ ] 无 key 环境（测试机 .env 只有 GLM key）：`title_zh == 原标题`、`dimension == default_dim`（降级断言）
- [ ] 有 key 环境：日志出现 `批次成功(glm-4.7-flash|glm-5.3-flash)`；429/1302/1305 是临时限流，顺链/降级不中断服务
- [ ] 故障转移链：首档连续失败 3 次切下一档；无 key 的 provider 档直接跳过
- [ ] 热词解释批次 ≤60 词/轮，不长时间占刷新锁（见部署记忆锁占用事故）

### 3. 热词池与三榜（词维度核心）
- [ ] `/api/stream?view=words` 三排序各自正确：rise（上升最快）/ hot（最热）/ new（新奇度）
- [ ] 词池无大小写重复卡（GPT-5 / gpt-5 单条）；停用词（llm/ai/model…）不进池
- [ ] 今日热词可显示：近 1-3 天高分报道的词在榜（新鲜度加权生效，2026-09-02 优化）
- [ ] 词典外 LLM 抽词词连续刷新不掉池（keywords churn 防护生效）
- [ ] 词卡 `news_cnt` 与详情页报道数一致；词卡内嵌 top-3 与「展开更多」同序

### 4. 词详情页与语言
- [ ] `/term/<词>` 任意词有页（词池命中 / HF 长尾直达 / 否则 404 + noindex）
- [ ] 详情页有解释块（静态词典 → terms 表 LLM 解释 → 模板兜底，恒非空）
- [ ] 中英切换：`/term/<词>?lang=en` 英文页显示 `display_en`（中文热词不回退中文）；
     从英文首页点词进详情仍保持英文（链接带 `?lang=`）
- [ ] Rise 显示：本周期无活跃报道的词不显示 `-1.00`（2026-09-02 修复）；真实下跌（如 -0.50）正常显示
- [ ] HF 词详情：官方/社区/arXiv 区块（live 数据）；`/hf` 页三排序降序

### 5. 搜索
- [ ] `/search?q=<词>&lang=zh|en`：结果按相关性打分，热词命中卡在顶部
- [ ] 高亮 `<mark>` 正确、HTML 转义安全；`/api/search/suggest` 补全
- [ ] 历史归档标记「📅 含 N 条历史归档」；空结果有建议词

### 6. SEO 与爬虫
- [ ] `/robots.txt`、`/sitemap.xml`（词表非空，按热度降序）、favicon 三件套
- [ ] 详情页 ld+json 四块合法（GSC 无越界 aggregateRating 报错）；canonical/OG 正确
- [ ] 404 页 `noindex`；`?lang=` 中文页 canonical 指向 `/term/<词>?lang=zh`

### 7. 管理后台 / 监控 / 统计
- [ ] `/admin`（ADMIN_TOKEN 未设则 404 隐身）；赞助位增删改/上下架/点击跳转
- [ ] `/monitor` 30 天趋势图（UV 口径）；`/monitor/api?days=30`
- [ ] `/health` ok；`/api/click/<slot>` 302 + 计数

### 8. 容器 / 进程 / 内存 / 锁
- [ ] `docker compose ps` healthy；workers=2（生产小内存约束）；`max-requests` 按流量校准（生产 50000）
- [ ] 跨进程锁正常：任意时刻只有一个 worker 刷新（fcntl）；刷新锁不被长任务卡死
- [ ] 内存：容器 RSS 稳态 < 1.6G（词聚合流式扫描、无整表 fetchall）
- [ ] 重启后预热：tracker/dims 启动线程各自完成一轮抓取（logs 无 `BlockingIOError` 风暴）

### 9. 部署产物与版本
- [ ] VERSION 文件已更新并随部署同步（应用运行不读，仅追踪用）
- [ ] 新增 `.py` 模块必须补 `docker-compose.test.yml` / `docker-compose.prod.yml` 挂载
- [ ] 文件拷贝部署用 `rsync --checksum`（Windows 本机用 `git -c core.autocrlf=false archive` 导出 LF 干净树），单文件改动后 `up -d --force-recreate`（bind mount 换 inode 需 recreate）

## 三、环境注意事项（经验）

- **GLM 免费档限流**：高峰返 1302/1305 是临时现象，等窗口重置即可，不是代码问题；应用按设计降级不中断。
- **测试机 vs 生产**：两台独立机器、独立 GLM key、`DIMS_REFRESH_HOURS` 错开（测试 1,7,13,19；生产 1,7,13,19 注意避免同 key 互挤——已独立 key）。
- **LLM 纪律**：worktree/单特性测试严禁设 key；只有 dev 完整回归需要时才在 `.env` 配真实 key。
- **锁占用教训**：刷新锁内的 LLM 批量工作必须有数量上限 + 失败熔断；卡锁特征 = words.json 停留旧数据 + 所有刷新被挡回。

## 四、上线步骤（简版）

1. dev 全量 pytest 绿（无 key）→ 冒烟 `python app.py` 首页/API。
2. 按本清单二逐项在测试机验证（`http://47.98.124.167:8080`），记录「需求# / 验证方式 / 结果 / 证据」。
3. 测试机验证通过 → 用户授权后合入 main → 按 `aitrendwatch-deploy-key` 部署生产。
4. 生产公网复验关键项（首页词卡、/api/stream fetched_at、详情页语言、搜索、health）。

相关：[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)、[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)、[`aitrendwatch-test-host`](aitrendwatch-test-host.md)、[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)、[`git-merge-doc-line-refs`](git-merge-doc-line-refs.md)
