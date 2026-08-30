---
name: aitrendwatch-server-stability
description: 1.6G 内存远程服务器 OOM 卡死根因 + fcntl 跨进程锁修复
metadata:
  node_type: memory
  type: feedback
  originSessionId: 9940b4e2-c608-4fb4-9867-93b67017b944
---

远程部署服务器规格极小：**1.6GB RAM**，6 容器共存（aitrendwatch、ai_search uvicorn/postgres/searxng/redis、docparse）。

**卡死根因（2026-08-26 排查）**：现象「CPU 占满卡死」，journalctl 显示 37 次 gunicorn SIGKILL（OOM killer），按小时聚集，正对 dims 1h 刷新周期，被杀时 worker RSS 287-289MB。**真因是内存耗尽，不是 CPU**：gunicorn `--workers 4` 是 4 个独立 fork 进程，而 `tracker.py`/`dims.py` 后台预热线程用 `threading.Lock` 串行化——threading.Lock **只在单进程内生效，不跨进程**。4 worker 各自起后台线程，每轮 4×并发跑 `_fetch_dims_raw()`（18 RSS + 46 HN + DeepSeek LLM）和 `_fetch_terms_raw()`，内存峰值约 4×，撑爆 1.6GB（当时 0 swap）→ OOM killer → fork-kill 循环（表面像 CPU 问题）。一次 `_fetch_dims_raw()` 约 280-576s。

**修复（三层，均已上线）**：

1. 止血：`--workers 4→2`（`docker-compose.prod.yml`，备份 `.bak.202608260748`）+ 加 1GB swap（`/swapfile`）。
2. 根因：`tracker.py` + `dims.py` 各加 `fcntl` 跨进程文件锁（`fcntl.flock(fd, LOCK_EX | LOCK_NB)` 非阻塞 trylock，锁文件 `CACHE_DIR/.tracker.refresh.lock`/`.dims.refresh.lock`）。两层锁：`threading.Lock`（串行同 worker 内）+ fcntl（跨 worker）。拿不到锁抛 `BlockingIOError`，视为「别的 worker 在做，跳过」。已双子进程验证：p1 LOCKED、p2 立即 SKIPPED。
3. 部署完整站点重设计（统一卡片流 + 6 维分类 + 新闻复合热度 + 分类条滚动条）。

**内存再优化（2026-08-29，history item 2，worktree-feat-memory-opt）**：把「workers 4→2」正式写进 `docker-compose.prod.yml` + `Dockerfile`（之前只是服务器手工改，repo 一直还是 4）；加 `--max-requests 1000 --max-requests-jitter 300` 定期回收 worker 防长驻线程内存爬升；`terms._refresh_words_inner` 把 `news_cards` 全表 `fetchall()`（随历史只增不减）改成单趟流式游标扫描（top news 与聚合一趟完成），`final_rows` 只读回 kept 词 + 精简列，`old` 用完即释放；`dims._dims_refresh_once` 把 `all_cards` 从 dims.json 摘出（只刷新管道用，服务路径/前端不读）→ 每个 worker 常驻缓存缩小。实测单进程 baseline 约 41MB，刷新峰值是主要矛盾。

**Why**：threading.Lock 不跨 fork 进程，多 worker gunicorn + 内存敏感任务必须用 fcntl 跨进程锁，否则 N worker × N 并发 = 内存 N×峰值。

**How to apply**：任何「后台预热线程 + 多 worker gunicorn」架构，跨 worker 串行化一律用 `fcntl.flock` 文件锁而非 `threading.Lock`；小内存机器（<2GB）默认加 swap + gunicorn max-requests 回收；历史库「只增不减」的大表扫描一律流式游标别 fetchall；跨 worker 常驻的大 JSON 缓存只存服务路径需要的字段（全量中间数据摘到刷新管道内用完即弃）。

相关：[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)、[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)
