---
name: aitrendwatch-deploy-key
description: aitrendwatch 生产主机（47.89.243.229）SSH/部署细节
metadata:
  node_type: memory
  type: reference
  originSessionId: 9940b4e2-c608-4fb4-9867-93b67017b944
---

- **IP**：`47.89.243.229`
- **SSH**：`ssh -i /home/wuyuming/Projects/work.pem root@47.89.243.229`
- **目录**：`/opt/aitrendwatch`
- **编排**：`docker-compose.prod.yml`（容器名 `aitrendwatch`，绑 `127.0.0.1:5050`，由宿主 Nginx 反代到域名 `aitrendwatch.top`）
- **Docker**：Compose v5，无 `docker-compose` v1 命令，用 `docker compose`
- **本机远程同构**：本地 `docker-compose.prod.yml` 与远程一致（挂载源码 `:ro`），改 `tracker.py` 后 rsync 上去 + `docker compose -f docker-compose.prod.yml up -d` 即可生效，无需 build。

迁移代码可用 `rsync -avz --exclude={.git,cache,data,__pycache__,vendor} -e "ssh -i ~/Projects/work.pem" root@47.89.243.229:/opt/aitrendwatch/ ~/Projects/aitrendwatch/`。生产要求 `<2GB` 内存必须 `workers: 2`、有 ≥1GB swap、保留 fcntl 跨进程锁。

**2026-08-29（release 1.1.0 部署）**：生产升级到 1.1.0（glm-switch 故障转移链 + memory-opt）。compose.prod 正式化 `workers 2` + `max-requests 1000/jitter 300`（此前 1.0.0 部署曾覆盖回 workers 4）。`.env` 原有 `DEEPSEEK_API_KEY`，本次按用户要求**追加了 `GLM_API_KEY`**（当时与测试机同一智谱 key，2026-08-30 起测试机已换独立 key，避免共用配额互挤）→ 链从 GLM 起步。VERSION 文件此前停在 0.3.0（1.0.0 部署漏更），本次已更新为 1.1.0。验证方式：`https://aitrendwatch.top/api/stream?view=news` 看 fetched_at（应为重启时刻）与 title_zh 是否真实翻译。

**⚠️ max-requests 生产副作用（2026-08-29 发现并已修）**：生产流量约 2000 请求/80 分钟 → 两 worker 各满 1000 同时被 gunicorn 回收（日志 `Booting worker with pid: N` 成对出现）→ 回收触发应用重启 → tracker+dims 双启动刷新，LLM 刷新从每 6h 变成每约 80min（约 4.5 倍）。**已修**：compose.prod + Dockerfile 的 `max-requests 1000 → 50000`（commit `8fd4476`，部署生产验证），回收间隔压到约 33h。**教训**：`--max-requests` 要按生产流量校准——过低会因「回收→启动刷新」反而高频调用 LLM，低流量测试机测不出，须在上生产流量后复验。

**2026-08-31（release 1.3.0 部署）**：dev 验证通过后合入 main 部署生产（VERSION 1.2.1→1.3.0：热词详情页词条解释 `_EXPLANATIONS` / 监控页趋势图改 UV / 相关报道按标题去重 / 「查看聚合页」改「查看热词」/ GSC JSON-LD 移除越界 aggregateRating / GLM-5.3-Flash 提示词优化 + `reasoning_effort=low`）。部署方式：`rsync --checksum`（**注意**：rsync 默认 size+mtime 快速检查会跳过内容已变但尺寸巧合相同的文件——本次 monitor.html 就被跳过，须用 `--checksum` 强制内容比对）+ `docker compose -f docker-compose.prod.yml up -d --force-recreate`（单文件 bind mount 换 inode 后必须 recreate 才生效）。`.env` 未动（DeepSeek + GLM 双 key，默认链 `glm-4.7-flash,glm-5.3-flash,deepseek-v4-flash`）。生产验证：重启后立即刷新，glm-4.7 遇 429 → 连续 3 次失败自动切 glm-5.3-flash → 27 批全部成功；新鲜 dims.json 中 30 条英文新闻句 100% 真实翻译、0 回显；历史库旧卡（部署前回显数据）随刷新轮换自然淘汰。

**2026-08-31（release 1.4.0 动态词典部署）**：VERSION 1.3.0→1.4.0，词池即词典 + LLM 解释资产化（`terms` 表 explain_zh/en/updated_at 列；`refresh_words` 6.5 解释批次；`dims.explain_terms`；详情页三级取词 + 模板兜底，每词必有解释块）。生产验证：刷新后前 60 个最热词获高质量解释（如 GLM-5.3-Flash →「智谱 320B/激活18B 原生多模态 MoE…」定义+时效价值），未回填词显示模板兜底解释；剩余 ~395 词按每轮 60 个随刷新回填。抽词提示词同步优化（禁泛化词）。**部署注意**：VERSION 文件同步后需重启才生效（version.py 启动时读取）。

**2026-09-01（release 1.5.0 部署，7 项需求）**：dev 全量回归（118 tests + 8 subtests 全绿）后合入 main（merge commit `a9b58c1`），VERSION 1.4.0→1.5.0 部署生产。内容：① 英文换行 `word-break: break-all → overflow-wrap: break-word`（index/term_detail/search，合约测试 test_word_break）；② 新增 HuggingFace 独立排序页 `/hf` + `/api/hf`（trending/likes/downloads 内存重排 + pipeline_tag 中文徽标「合理标签」+ tags/论文，开源动向，header 🤗 HF 入口，SSR 双语 SEO）；③ 通用热词停用词表 `terms._TERM_STOPWORDS`（ai/llm/model/tech 等 8 词，canonical 键，抽词/聚合/HF 三入口过滤，test_stopwords）；④ 热词详情页与词卡「展开更多」列表按 hot 降序（hot 缺失回退 score，同 hot 按 published 降序，test_term_news）；⑤ 「展开更多」「查看热词」统一 `.word-actions` 容器 + `link-btn official` 带框样式（test_show_more_view_page）；⑥ 大小写无关聚类 `normalize_term`（小写 + 空白/下划线→'-' + 首尾 ASCII 标点剥离，news_store 落库前 canonical 化，test_case_insensitive）；⑦ 热词页返回首页恢复滚动位置（sessionStorage `aitw_last_scroll` + `scroll_back=1` 标记 + back_forward 双路径 + head 首帧隐藏防闪现）。部署方式（Windows 本机，无 rsync）：`git -c core.autocrlf=false archive main` 导出 LF 干净树 → scp 变更文件（app.py/terms.py/news_store.py/VERSION/templates 4 个 html）→ `docker compose -f docker-compose.prod.yml up -d --force-recreate`。`.env` 未动。生产验证：容器 healthy；/api/hf 三排序降序正确、/hf 双语 + pipeline 中文标签；/api/dims 刷新后 ok（162 条）；词池刷新后停用词 `llm` 出池、无大小写重复词卡；/api/word 与 /term 详情新闻 hot 降序（anthropic 259906→…）；QWEN/QwEn/AnThRoPiC 等变体查询同词；dims 启动预热 glm-4.7 429 → 故障转移 glm-5.3-flash 批次成功。**注意**：本次部署前生产 `/opt/aitrendwatch/VERSION` 仍为 1.3.0（1.4.0 未同步成功），已一并更新为 1.5.0；容器未挂载 version.py/VERSION（compose volumes 无此项），应用运行不读版本号，版本仅仓库/主机追踪用。

**2026-09-01（release 1.6.0 部署，20260902 四需求）**：用户先把 dev 合入 main（merge `25a64d1`）→ main 同步回 dev（ff）→ dev 上 VERSION 1.5.0→1.6.0 + 索引同步（`b615153`）→ dev 全量回归（**133 tests + 8 subtests 全绿**，无 key 降级断言）+ 冒烟 → 合回 main（merge `36f79d1`）。内容：① 上线前核心回归清单记忆（`aitrendwatch-regression-checklist`）；② Openclaw 热词逻辑优化——词典收录 + `news_store.upsert_cards` keywords churn 防护（GLM 限流轮次降级子集不覆盖 LLM 抽取关键词）+ 热窗 hot 按报道新鲜度加权（≤1d ×3 / ≤3d ×1.5）+ `WORD_STREAM_LIMIT` 60→100 + **rise 环比改报道数口径**（分数含时效衰减掺入环比会误判稳态词下降）；③ 英文页词详情语言一致性——LLM 翻译失败轮次保留旧 display_en；④ 词页 Rise 不再显示 -1.00（无活跃报道占位值）。部署方式（本机有 rsync）：`rsync -avz --checksum app.py news_store.py terms.py VERSION root@…:/opt/aitrendwatch/` + `templates/{index,term_detail}.html` **必须分两次 rsync**（多源路径会拍平成目标根目录，模板曾误拷到 `/opt/aitrendwatch/` 根，已删）→ `docker compose -f docker-compose.prod.yml up -d --force-recreate`（模板换 inode 后必须 recreate 生效）。`.env` 未动（DeepSeek + GLM 双 key，默认链 glm-4.7,glm-5.3,deepseek-v4）。生产验证：容器 healthy、内存 143MB；重启后预热刷新约 13 分钟（07:53 重启 → 08:06 words.json 更新），glm-4.7 遇 429 → 连续 3 次切 glm-5.3-flash 批次成功；词池 200 词全量、停用词无 `llm/ai/model`、Openai/openai/OPENAI 变体同词；/term/Anthropic 新闻 hot 降序（259906→…）；/hf 三排序降序 + pipeline 中文标签（图文理解/文本生成…）；Rise -1.0 占位词隐藏、真实下跌 -0.50 正常显示；display_en 保留（教育→Education 等 51 词卡）。**注意**：Openclaw 生产数据下 hot 榜第 103 名（测试机 #72），差 3 名未进 100 词展示窗——机制全部生效（收录/加权/churn 防护），排名随生产数据波动，非代码缺陷。

**⚠️ 动态词典解释批次锁占用事故（2026-08-31 发现并已修）**：动态词典特性首轮部署后，解释批次对存量 ~455 个词典外词全量生成（38 块 LLM 调用），在 **dims 刷新锁内**执行；GLM 免费档限流不稳时单块最长 90s 读超时，整批可占锁 30-60 分钟，**阻塞 words.json 更新**（热词卡停留旧数据）。且 fcntl 锁被卡住的 worker 持有，后续所有刷新被挡回（`SPY2 ok:True calls:[]` 特征）。**已修**（commit `862c39b`）：① `EXPLAIN_BATCH_MAX_WORDS=60` 每轮解释批次上限（按热度降序，最热优先，存量词后续轮次回填，单轮锁占用约 5 分钟）；② `EXPLAIN_CONSECUTIVE_FAIL_LIMIT=5` 连续失败熔断 + 读超时 90s→60s。**教训**：任何在刷新锁内的 LLM 批量工作都必须有数量上限 + 失败熔断；排障手法——`/proc/locks` 查 flock 持有者、worker 线程 `wchan`（do_select/do_poll=网络等待）、`docker top` 看 CPU 时间。

**权限提示**：生产主机 SSH 每次读写常被分类器软拦截，需用户 AskUserQuestion 显式授权「生产」目标后才放行；只读日志/文件检查也可能被拦，可用公网 API（`https://aitrendwatch.top/api/*`）替代验证。

**⚠️ 上线前必读**：[`aitrendwatch-regression-checklist`](aitrendwatch-regression-checklist.md)——每次生产部署前先过全量核心回归（pytest 全绿 + 测试机逐项验证），再合入 main 部署。

相关：[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)、[`aitrendwatch-test-host`](aitrendwatch-test-host.md)、[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)、[`aitrendwatch-regression-checklist`](aitrendwatch-regression-checklist.md)、[`git-merge-doc-line-refs`](git-merge-doc-line-refs.md)
