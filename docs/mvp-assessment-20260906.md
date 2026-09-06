# aitrendwatch MVP 全面评估（2026-09-06）

> 评估方式：通读 `docs/` 多级索引与关键配置；本地全量 pytest（无 LLM key 降级路径）；
> 对生产站 `https://aitrendwatch.top` 实测 20+ 端点的状态码 / 延迟 / 内容 / 响应头。
> 视角：MVP——只看「必做项」，不展开复杂功能设想。

## 总体结论

**功能层面已远超 MVP 及格线，没有「功能必做项」缺口。**

核心闭环完整：36 个 RSS 源 + HuggingFace 模型榜 + arXiv → LLM 维度打标/双语翻译/抽词
→ 热词卡 / 逐条新闻 / 开源（HF）三视图 → 全站搜索 → 词条长尾 SEO 页
（sitemap / hreflang / JSON-LD / indexable 门槛）→ 埋点 + 流量监控后台 + 赞助位变现。
自动化测试 290 个全绿（实测约 6 秒跑完）；降级链、三级缓存、跨进程锁设计完善。
用户系统、评论、订阅推送等 MVP 不需要的东西也确实没有，方向正确。

## 线上实测结果（2026-09-06）

| 端点 | 状态 | 延迟 | 评价 |
|---|---|---|---|
| `/` 首页 | 200 | 冷 3.9s / 热 1.6–2.4s | 可接受，gzip 已开 |
| `/health` `/robots.txt` `/sitemap.xml` | 200 | <1.1s | 正常，sitemap 达标词过滤生效 |
| `/hf`、`/term/Anthropic` | 200 | ~1.1s | 正常 |
| `/api/stream?view=words` | 200 | 1.8–2.2s | 正常 |
| `/search?q=agent` | 200 | **4.4s** | 偏慢 |
| `/api/stream?view=news` | 200 | **7–21s（5 次复测稳定复现）** | ⚠️ 严重问题 |

其他实测发现：

- `/api/dims` 曾瞬时返回 `ok:false` 空数据（几分钟后复测正常），疑似 worker 回收 /
  缓存重建窗口期的鲁棒性小瑕疵。
- 生产响应头只有 `Server: nginx/1.18.0 (Ubuntu)`，无 HSTS / X-Content-Type-Options /
  X-Frame-Options / Referrer-Policy / CSP；nginx 1.18 为 2020 年版本，偏老旧。
- `/privacy`、`/privacy-policy`、`/about`、`/contact` 全部 404；首页 footer 只有
  Terms 一个链接；生产 `CONTACT_EMAIL` 未配置（条款页联系邮箱位为空）。同时全站挂了
  Google Analytics（gtag），自建埋点收集 IP / GeoIP / session_id。
- 仅注册了 404 errorhandler，无 500 错误处理。
- `/api/event` 为无鉴权公开 POST（仅有 event_type 白名单），无频率限制；
  `/admin/login` 有 hmac 防时序比较，但无失败次数限制。

## MVP 视角的必做项（按优先级）

### P0 — 一个真正的功能缺陷

1. **修复 `/api/stream?view=news` 的 7–21 秒延迟。**
   「逐条新闻」是首页三大视图之一，442 张卡 / 554KB 响应每次请求现算，与架构文档
   「请求路径零上游 IO、读缓存秒回」的设计目标不符，用户实际不可用。
   修法建议：按 `(lang, sort)` 缓存装配结果（后台刷新后失效）、或截断数量 + 前端分页。
   **这是唯一影响主功能体验的必做修复。**

### P1 — 正式运营/变现前的合规与安全（各约半天工作量）

2. **补隐私政策页 + 配置联系邮箱。**
   全站收集 IP/GeoIP/session_id 行为数据且挂了 GA，但没有任何隐私政策页面。
   没有隐私政策页 AdSense 审核过不了，GDPR 层面也是裸奔。
   做法：`/privacy` 双语页 + footer 加链接 + 生产 `.env` 填 `CONTACT_EMAIL`。
3. **nginx 加基础安全响应头。**
   反代层加 HSTS / X-Content-Type-Options / X-Frame-Options / Referrer-Policy 四行即可。
   另外 nginx 1.18（2020 年版本）建议随系统一起规划升级。
4. **两个公开端点加限流。**
   `/api/event`（埋点上报）可被刷量把 SQLite 撑爆；`/admin/login` 无失败次数限制。
   按 IP 做简单内存计数限流即够 MVP 用。

### P2 — 运维兜底（各约 1–2 小时）

5. **接免费 uptime 监控。** `/health` 已就绪，挂 UptimeRobot 免费档盯它 + 首页，
   宕机邮件告警；目前站点挂了只能靠用户反馈发现。
6. **SQLite 定期备份。** `data/` 是 bind mount 单点（赞助位/统计/事件历史库），
   一个 cron + `sqlite3 .backup` 或定时拷到对象存储即可。
7. **补 500 错误处理。** 目前只有 404 handler，500 会裸露 Flask/nginx 默认错误页。

### P3 — 可选增强（非必做）

- 搜索 4.4s 提速（加权打分可缓存热门查询）。
- 静态资源 Cache-Control + 前置 CDN，压低首页 1.6s+ 的 TTFB。
- GitHub 榜（`history/20260905.txt` 阶段 2）——设计已完整，但属内容增强而非 MVP
  必做，等 P0–P2 清完再动。

## 明确不建议做（守住 MVP 边界）

用户注册/账号体系、评论互动、邮件订阅推送、移动端 App、个性化推荐——对
「AI 热点聚合站」的验证目标（SEO 长尾获客 → 广告/赞助变现）都不是必需品，
当前单容器小内存架构也不适合承载。

## 一句话总结

功能上做多了、做好了；真正欠的是「news 视图性能修复」这一项功能债，
加上隐私政策、安全头、限流、监控、备份这几件每个上线网站都该有的运营地基。
