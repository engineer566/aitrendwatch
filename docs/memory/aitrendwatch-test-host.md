---
name: aitrendwatch-test-host
description: aitrendwatch 测试主机（47.98.124.167）SSH/部署细节
metadata:
  node_type: memory
  type: reference
  originSessionId: 9940b4e2-c608-4fb4-9867-93b67017b944
---

- **IP**：`47.98.124.167`
- **SSH**：`ssh -i ~/Projects/test_host.pem root@47.98.124.167`（key 需 `chmod 600`，否则 ssh 拒绝）
- **目录**：`/opt/aitrendwatch`
- **编排**：`docker-compose.test.yml`（测试专用，直接暴露 `0.0.0.0:8080:5000`，2 worker，不依赖 Nginx）
- **访问**：`http://47.98.124.167:8080`
- **容器名**：`aitrendwatch`，基础镜像 `python:3.13-slim`
- **.env**：已生成随机 `SECRET_KEY` 和 `ADMIN_TOKEN`（ADMIN_TOKEN 见部署时回显，或服务器 `/opt/aitrendwatch/.env`）
- **主机环境**：Ubuntu 22.04 / x86_64 / 1.6G 内存 / Docker 29.7.2 + Compose v5.5.0

部署：`ssh -i ~/Projects/test_host.pem root@47.98.124.167`（本机实际 key 路径为 `/data/test_host.pem`，不是 `~/Projects/`）→ `cd /opt/aitrendwatch` → `docker compose -f docker-compose.test.yml up -d` → `docker compose -f docker-compose.test.yml logs -f`。测试主机与生产主机是**两台独立机器**，不要混淆 SSH key。

**重要（2026-08-28 验证）**：`/opt/aitrendwatch` **不是 git clone**，是文件拷贝部署；compose 用 bind mount 逐个挂载 `.py` 文件。**每次合入新增模块（如 `terms.py`）时，必须在 `docker-compose.test.yml` 里补挂载**（`- ./terms.py:/app/terms.py:ro`），否则容器内 `import` 失败、功能静默降级。本地仓库没有 `docker-compose.test.yml`（只在主机上手工维护）。

**公网 8080 已放行（2026-08-28 阿里云安全组开通后验证）**：SSH(22) 与 `http://47.98.124.167:8080` 均可公网访问。此前 8080 公网不可达的根因是**阿里云安全组未放行 8080**（主机 iptables/UFW 无拦截、docker-proxy 监听 `0.0.0.0:8080`）——主机本机 curl 自己公网 IP 也超时的现象即安全组拦截。若再遇到公网访问超时，先查阿里云控制台安全组，而非主机防火墙。

**2026-08-29（dev 合入 glm-switch + memory-opt 后部署）**：`/opt/aitrendwatch/.env` 已配置 `GLM_API_KEY`（智谱 BigModel，值不记录于此）。应用默认走 `LLM_CHAIN`（`glm-4.7-flash` 首档），**未设 DeepSeek key**。

**2026-08-30（修复中英文混杂）**：测试机更换为**独立 GLM key**（不再与生产共用，避免两机同时刷新互挤免费档配额），`.env` 另设 `DIMS_REFRESH_HOURS=2,8,14,20`（与生产 1,7,13,19 错开）。仍**未设 DeepSeek key**——测试机混杂率上限受 GLM 免费档质量约束（实测独立 key 后 zh 语境混杂 64%→32%，剩余为 GLM 偶发 429/超时且无备用 provider）。

**智谱 GLM 免费档限流现象（实测）**：`glm-4.7-flash` 高峰返 `1305 访问量过大`、账户级返 `1302 速率限制`（同 key 所有 GLM 档都受限）；1-token 直测可成功但批量分类请求被限。属**临时性**——限流时应用按设计走故障转移链 → 优雅降级（`title_zh=原标题`、`dimension=default_dim`），服务不中断、缓存照写。下次测试 GLM 若再遇 1302/1305，等限流窗口重置或改非高峰时段即可，不是 key/代码问题。

相关：[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)、[`hot-aggregator-aitrendwatch`](hot-aggregator-aitrendwatch.md)、[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)
