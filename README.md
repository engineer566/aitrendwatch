# ModelRadar · AI 热点聚合信息流

追踪 HuggingFace 模型热榜 + arXiv 相关论文，聚合国内/国际一手热点，单页信息流展示。Python Flask 后端聚合（规避 CORS），暗色主题前端单页应用。

> 原名 web2 热点聚合站点，已重构为以 AI 热点为主的信息流。

## 功能概览

- **AI 热词追踪（主功能）**：拉取 HuggingFace 模型榜（trendingScore / likes 两种排序），按底模去重，每个热词卡片含官方链接、社区讨论入口（知乎 / B站 / GitHub 搜索）、相关 arXiv 论文。
- **国内/国际热点聚合**：百度、B站、今日头条、知乎、抖音、Hacker News、GitHub Trending 多源并发抓取，单源失败不影响整体。
- **三系统变现位**：自建赞助位（管理后台 CRUD + 曝光/点击统计）+ Google AdSense + 百度联盟，三者并存，自建优先。
- **维度榜双版本**：国内中文一手源 + LLM 翻译产出的中英文双版本。

## 架构

```
app.py       Flask 路由层：热点聚合 + 热词 API + 赞助位 + 管理后台
tracker.py   热词追踪层：HF 模型抓取 + 底模去重 + arXiv 论文 enrich + 后台预热
store.py     赞助位数据访问层：SQLite (WAL) + 优雅降级，统计收口于此
config.py    集中配置层：所有参数从环境变量读，带安全默认值
templates/   index.html（信息流）+ admin.html / admin_login.html（后台）
```

### 热词缓存设计（关键）

arXiv 全文检索慢（8 词 × 3s ≈ 24s）且限速，**不在请求路径做**：

- 后台 daemon 线程每 **6 小时**预热一次，完整抓取（HF + arXiv）写文件缓存。
- 预热失败 **5 分钟**后快速重试，不干等下个周期。
- 文件缓存兜底有效期 **24 小时**（即便预热连续失败，旧缓存最多服务 24 小时）。
- 多 gunicorn worker 通过文件锁串行化，实际只有一个 worker 打 arXiv（省配额）。
- 请求路径只读文件缓存秒回；缓存完全缺失时走快速兜底（只抓 HF，~1s）保证首屏不卡。

## 数据源

### 热词追踪

| 来源 | 方式 | 备注 |
|------|------|------|
| HuggingFace 模型榜 | `hf-mirror.com` 镜像 API | ✅ 官方 `huggingface.co` 本网络不可达，走镜像 |
| arXiv 相关论文 | `export.arxiv.org` 全文检索 | ✅ 串行 + 3s 间隔避开 429 限速 |

### 热点聚合

| 来源 | 方式 | 备注 |
|------|------|------|
| 百度热搜 | `top.baidu.com` 官方接口 | ✅ |
| B站热门 | `api.bilibili.com` 官方接口 | ✅ |
| 今日头条热榜 | `toutiao.com` 官方接口 | ✅ |
| 知乎热榜 | `api.zhihu.com` 官方接口 | ✅ |
| 抖音热搜 | `aweme.snssdk.com` 官方接口 | ✅ |
| Hacker News | 官方 Firebase API | ✅ |
| GitHub Trending | 抓取 trending 页面 HTML | ✅ |
| 微博热搜 | —— | ⚠️ 接口需登录态，暂不可用 |

## 运行

### 前置：环境变量

复制 `.env.example` 为 `.env` 并填入（生产必设 `SECRET_KEY` 与 `ADMIN_TOKEN`，未设则管理后台隐身 404）：

```bash
cp .env.example .env
```

可选变量：`SITE_NAME`、`BASE_URL`、`ANALYTICS_ENABLED`，以及广告联盟 `ADSENSE_ENABLED` / `ADSENSE_CLIENT`、`BAIDU_ADS_ENABLED` / `BAIDU_ADS_CPRO_ID`（详见 `.env.example` 注释）。

### 方式一：Docker（推荐，生产级部署）

本地开发（直接暴露 5000 端口）：

```bash
docker-compose up -d --build      # 构建并后台启动
docker-compose logs -f             # 查看日志
docker-compose down                # 停止
```

生产部署（仅绑 `127.0.0.1:5050`，由宿主 Nginx 反代，不直接暴露公网）：

```bash
docker-compose -f docker-compose.prod.yml up -d --build
```

容器以 gunicorn（4 worker，60s 超时）运行，带健康检查与 `unless-stopped` 自动重启。

### 方式二：本地直接运行

```bash
pip install -r requirements.txt
python app.py
```

## API

### 热词追踪（主功能）

| 路由 | 说明 |
|------|------|
| `GET /api/trending` | 7 日上升最快热词（HF trendingScore 降序） |
| `GET /api/top` | 热度最高热词（HF likes 降序） |
| `GET /api/term/<name>` | 单个热词详情：官方链接 + 社区讨论 + 相关论文 |

### 热点聚合

| 路由 | 说明 |
|------|------|
| `GET /api/sources` | 所有源元信息 |
| `GET /api/hot/<source>` | 单源热点（`source` 取下表任一 key） |
| `GET /api/all` | 并发聚合所有源（每源带硬性超时） |

热点源 key：`baidu` / `bilibili` / `toutiao` / `zhihu` / `weibo` / `douyin` / `hackernews` / `github`

### 其他

| 路由 | 说明 |
|------|------|
| `GET /` | 前端信息流页面 |
| `GET /health` | 健康检查 |
| `GET /api/click/<slot_id>` | 赞助位点击记录 + 302 跳转 |
| `/admin` | 管理后台（需 `ADMIN_TOKEN`，未设则 404 隐身） |

## 特性

- 热词榜 + 8 大热点源聚合，并发抓取
- 单源结果 5 分钟内存缓存，热点源慢速失败不拖垮整体
- 后台预热文件缓存（6h / 失败 5min 重试 / 24h 兜底），请求路径秒回
- 容错：单源失败不影响其他源展示
- 响应式暗色 / 亮色主题 UI，支持 Tab 筛选 / 全部展示
- 赞助位 HTML 净化（白名单标签 + 属性，剔除 script/on*）
- 管理：赞助位 CRUD、启停、投放期、30 天曝光/点击统计、PV 计数

## 访问

服务监听 `0.0.0.0:5000`（本地）/ `127.0.0.1:5050`（生产，由 Nginx 反代）：

| 场景 | 地址 |
|------|------|
| 本机（本地运行） | http://127.0.0.1:5000 |
| 本机（生产 compose） | http://127.0.0.1:5050 |

> 暂未做公网直接暴露；生产由宿主 Nginx 反代 `127.0.0.1:5050`。

## 技术栈

- Python 3.13 + Flask + gunicorn（4 worker）
- requests（HTTP 抓取）、sqlite3（stdlib，赞助位存储）
- 零 LLM 依赖的热词提取；arXiv 全文检索 + 词边界过滤保证论文相关性
- Docker / docker-compose 部署

> 仅供学习交流，数据版权归原作者所有。
