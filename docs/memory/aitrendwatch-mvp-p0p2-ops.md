---
name: aitrendwatch-mvp-p0p2-ops
description: MVP P0~P2 修复收尾：宿主侧上线步骤（nginx 安全头 / 生产 CONTACT_EMAIL / UptimeRobot / SQLite 备份 cron）与验证方法
metadata:
  node_type: memory
  type: reference
  originSessionId: mvp-p0p2-2026-09-07
---

# MVP P0~P2 修复收尾（2026-09-07）

来源：[`docs/mvp-assessment-20260906.md`](../../mvp-assessment-20260906.md) P0~P2 清单。
**代码侧改动已全部合入 dev**（详见 `docs/INDEX.md` 各模块注释与 git log），本文件只记录
「必须在生产/测试主机上执行」的收尾动作 + 每项上线后的验证方法。执行前先读
[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)（部署方式/权限提示：生产 SSH 动作需
用户显式授权）与 [`aitrendwatch-regression-checklist`](aitrendwatch-regression-checklist.md)。

## 变更文件（本次代码侧）

- P0：`dims.py` 新增 news 视图内容池缓存——后台刷新后写 `cache/news.json`（neutral 池，
  url/标题级去重已做），`get_news_cards` 读池文件（mtime 感知）零 DB 读，池缺失回退旧现装配
  路径。请求路径不再每次扫 news.db（线上 `/api/stream?view=news` 7-21s 根因）。测试
  `tests/test_news_pool_cache.py`（3 用例）。
- P1：`app.py` + `templates/privacy.html` + 页脚（index/hf/terms）隐私链接 + `/privacy`
  双语页（`/privacy-policy` 301 别名）+ sitemap 收录 /privacy。`templates/` 之外还新增
  `SITE_PRIVACY_UPDATED` 常量。测试 `tests/test_privacy_legal.py`（5 用例）。
- P1：`ratelimit.py`（新，进程内固定窗口按 IP 限流，内存有界 fail-open）+ `config.py`
  限流阈值（`EVENT_RATE_LIMIT=300/分/IP`、`LOGIN_RATE_LIMIT=5/15分/IP`）+ 接线
  `/api/event` 与 `/admin/login`（429 + Retry-After）。测试 `tests/test_rate_limit.py`（7 用例）。
- P2：`app.py` 补 500 errorhandler（API→JSON，页面→noindex HTML，带完整日志）。
  测试 `tests/test_error_handling.py`（3 用例）。
- P2：`scripts/backup_db.py`（SQLite 在线一致备份 + gzip + 轮转，宿主 python3 直接跑，
  无需 sqlite3 CLI / 无需进容器）。
- 配套：`deploy/nginx-security-headers.conf`（安全响应头 snippet）、`.env.example` 补
  `CONTACT_EMAIL`、`docs/` 索引同步。

## 上线步骤（生产 47.89.243.229，/opt/aitrendwatch）

### ① 代码部署（P0+P1+P2 代码）

按 deploy-key 常规流程：dev 全量回归绿（本次基线 290 → 新增 18 用例 ≈ 308+，无 key）→
合 main → archive/scp 全部运行时文件（app/config/dims/tracker/terms/store/news_store/
text_utils/stream_utils/version.py + VERSION + **ratelimit.py**（新模块，勿漏）+ templates
9 个 html（含新 privacy.html）+ 可选 deploy/nginx-security-headers.conf、scripts/backup_db.py）
→ `docker compose -f docker-compose.prod.yml up -d --force-recreate`（镜像 runtime 不变）。

**验证 P0**：ssh 内 `curl -s 127.0.0.1:5050/api/stream?view=news | head -c 200` 秒回；
`ls -la /opt/aitrendwatch/cache/news.json` 出现且 fetched_at 为最近刷新时刻（重启后首轮
dims 预热完成前走回退路径，预热后自动出现）；计时对比公网 `curl -w '%{time_total}'`。
多 worker 每请求内存 +一份池解析驻留（约 1-3MB/worker，110MiB 预算内），勿反复重启制造
预热窗口。

### ② 生产 .env 补 CONTACT_EMAIL（P1）

编辑 `/opt/aitrendwatch/.env` 增加 `CONTACT_EMAIL=`（真实联系邮箱），recreate 容器生效。
验证：公网 `/privacy`、`/terms` 页联系位出现邮箱（不再占位文案）。

### ③ nginx 基础安全头（P1，宿主 nginx）

1. 拷 `deploy/nginx-security-headers.conf` 到宿主 `/etc/nginx/snippets/security-headers.conf`；
2. aitrendwatch.top 的 `server{}` 内加一行 `include /etc/nginx/snippets/security-headers.conf;`
   （若该 server 某 location 已有 add_header，头会只在该 location 生效/或丢失父级头——见文件注释）；
3. `nginx -t && systemctl reload nginx`。
验证：`curl -sI https://aitrendwatch.top/ | grep -iE 'strict-transport|x-content-type|x-frame|referrer-policy'`
四头都在。**建议**：nginx 1.18（2020）随系统升级，不阻塞本项。

### ④ 限流阈值（P1，可选调参）

默认即可用（300/分/IP 事件、5/15分/IP 登录）。若前端误伤可调大 `EVENT_RATE_LIMIT`
（写入 .env 后 recreate）；验证：`for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code} ' \
-X POST -H 'X-Forwarded-For: 1.2.3.4' 127.0.0.1:5050/api/event -d '{}'; done` →
连续 5 个 200 后出现 429。注意 XFF 伪造面：建议宿主 nginx 对上游只透传可信头
（`proxy_set_header X-Forwarded-For $remote_addr;`）再谈精确限流。

### ⑤ SQLite 定期备份 cron（P2）

宿主 crontab（Asia/Shanghai，每天 04:30，备份目录放 data/ 同盘之外更佳）：
```
30 4 * * * cd /opt/aitrendwatch && /usr/bin/python3 scripts/backup_db.py \
    --data-dir data --backup-dir /opt/aitrendwatch-backups --keep 14 \
    >> /var/log/aitw-backup.log 2>&1
```
验证：首跑后 `/opt/aitrendwatch-backups/` 有 `sponsors.db-*.db.gz` / `news.db-*.db.gz`；
恢复演练：`gunzip -c 最新备份 > /tmp/restore.db` 后 sqlite 能打开且 count>0。
**建议**：另把 backups 目录同步到对象存储/异机（rclone/rsync），否则同盘仍单点。
（`data/`、`cache/` 是 bind mount 运行产物，不入 git。）

### ⑥ 免费 uptime 监控（P2）

UptimeRobot 免费档（或同类）：
1. 注册后添加监控：HTTP(S) `https://aitrendwatch.top/health`（间隔 5 分钟，监控关键字
   200 或 "ok"，失败/恢复告警发邮件）；
2. 再加一条 `https://aitrendwatch.top/` 首页监控；
3. 告警联系人邮箱建议与 CONTACT_EMAIL 一致。
验证：故意停容器 60s（或等一次真实故障）确认收到告警；恢复邮件也应到达。

### ⑦ 500 错误页（P2）

代码已部署即生效；回归由 pytest（`tests/test_error_handling.py`）覆盖，生产不主动制造 500。
抽查：`gunicorn error log` 无新增堆栈噪音即可。

## 上线顺序与回归

按 regression-checklist 全量过一遍再合 main；测试机先行（同文件清单 + `docker compose -f
docker-compose.test.yml up -d --force-recreate`），逐项验证后生产。**本批次已定 release
1.11.0**（VERSION 已 bump），合 main 后按 deploy-key 常规流程部署，部署完用本文件①~⑦逐项验证。

## 相关记忆

[`aitrendwatch-deploy-key`](aitrendwatch-deploy-key.md)、[`aitrendwatch-test-host`](aitrendwatch-test-host.md)、
[`aitrendwatch-regression-checklist`](aitrendwatch-regression-checklist.md)、
[`aitrendwatch-server-stability`](aitrendwatch-server-stability.md)。
