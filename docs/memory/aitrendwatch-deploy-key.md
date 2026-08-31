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

**权限提示**：生产主机 SSH 每次读写常被分类器软拦截，需用户 AskUserQuestion 显式授权「生产」目标后才放行；只读日志/文件检查也可能被拦，可用公网 API（`https://aitrendwatch.top/api/*`）替代验证。

相关：[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)、[`aitrendwatch-test-host`](aitrendwatch-test-host.md)、[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)、[`git-merge-doc-line-refs`](git-merge-doc-line-refs.md)
