# 热点聚合 · 实时热搜一览

通过多个免费 API 聚合当前最新热点，单页展示。Python Flask 后端聚合（规避 CORS），前端单页自动刷新。

## 数据源

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

### 方式一：Docker（推荐，生产级部署）

```bash
cd ~/Projects/web2
docker-compose up -d --build      # 构建并后台启动
docker-compose logs -f             # 查看日志
docker-compose down                # 停止
```

容器以 gunicorn（4 worker）运行，带健康检查与自动重启策略。

### 方式二：本地直接运行

```bash
pip install flask requests
python app.py
```

## 访问

服务监听 `0.0.0.0:5000`：

| 场景 | 地址 |
|------|------|
| 本机 | http://127.0.0.1:5000 |
| 局域网 | http://10.10.103.6:5000 |
| Tailscale 内网 | http://100.97.161.96:5000 |

> 已映射到宿主机 `0.0.0.0:5000`，同 Tailscale 网络的设备可直接访问。暂未做公网暴露。

## API

- `GET /api/sources` — 所有源元信息
- `GET /api/hot/<source>` — 单源热点（`source` 取上表任一 key）
- `GET /api/all` — 并发聚合所有源
- `GET /` — 前端页面

## 特性

- 8 大热点源聚合，并发抓取
- 单源结果 5 分钟内存缓存，减轻上游压力
- 容错：单源失败不影响其他源展示
- 响应式暗色主题 UI，支持 Tab 筛选 / 全部展示
- 每 5 分钟自动刷新 + 手动刷新
- 点击条目新标签打开原链接

> 仅供学习交流，数据版权归原作者所有。
