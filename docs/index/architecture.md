# 架构索引

> 模块依赖、请求生命周期、后台预热、缓存层级。配合 [INDEX.md](../INDEX.md) 使用。

## 模块依赖图

```
                    ┌──────────┐
                    │ config.py│  (环境变量/开关/路径，无内部依赖)
                    └────┬─────┘
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
     ┌─────────┐   ┌─────────┐   ┌──────────┐
     │dims.py  │   │tracker  │   │ store.py │  ┐
     │(事件层) │   │.py(热词)│   │(赞助/统计)│  │ 各自
     └────┬────┘   └────┬────┘   └────┬─────┘  │ 独立
          │             │             │        │ SQLite
          ▼             │             │        ┘
   ┌─────────────┐      │      ┌─────────────┐
   │news_store.py│      │      │ (sponsors.db)│
   │(事件历史库) │      │      └─────────────┘
   └──────┬──────┘      │
          │             │
          ▼             ▼
   ┌─────────────────────────┐
   │ terms.py (词粒度聚合层)  │  ──┐
   │ 词池归并/三榜打分/快照    │    │ news.db
   │ 词典抽词/历史回填        │    │ (terms/
   └─────────────────────────┘    │  term_snapshots)
          ▲                       │
          │ (dims 刷新锁内调)      ┘
          │
   ┌──────────────────────────────────────┐
   │ app.py  (Flask 入口 + 路由)           │
   │ import tracker, dims, terms, config, │
   │        store                          │
   └──────────────────────────────────────┘
```

依赖要点：
- `app.py` 是唯一聚合点，import 全部业务模块。
- `dims.py` 依赖 `config` + `news_store` + `terms`（抽词降级 + 刷新时调 refresh_words）。
- `terms.py` 依赖 `config` + `news_store`（读历史库聚合）；`tracker.py` 只依赖 `config`。
- `text_utils.py` 为 RSS 文本/URL 实体解码叶子模块；`stream_utils.py` 统一卡片身份去重与维度计数规则。
- `store.py` 与 `news_store.py`/`terms.py` 互不依赖，各管 SQLite（`sponsors.db` / `news.db`）。
- `config.py` 是叶子，被所有模块 import，自身只 import `os`。
- `vendor/` 在 `sys.path` 前置，提供 flask/requests/gunicorn/jinja2 等，非项目代码。

## 请求生命周期（三条主链路）

### 1. 首页 `/`  （`app.py:628`）
```
浏览器 GET /
  → detect_region()              # Accept-Language → zh/global
  → store.list_slots(region)     # 取该地域活跃赞助位
  → store.record_pageview()      # PV+1 (best-effort)
  → store.record_visit(ip, country)  # 写 visits 表（监控页数据源）
  → store.record_impression(slot)    # 每个赞助位曝光+1
  → _initial_terms_for_ssr(sort, lang)  # SEO 开启时按同一排序取首批词卡塞 SSR
  → render_template("index.html", ...)  # 前端 JS 再 fetch /api/stream 补全
```
首页本身**不抓上游**——数据靠前端 JS 异步拉 `/api/stream`，后端只读缓存。

### 2. 统一卡片流 `/api/stream`  （`app.py:769`）  ← 前端主数据源
```
GET /api/stream?lang=zh&sort=rise&view=words
  → detect_region() 决定默认 lang
  → view=words → terms.get_word_cards(sort, lang, limit=60)  # 先完整排序再截断
  → view=news → tracker.get_model_cards + dims.get_news_cards  # 旧逻辑零回归
  → 按 sort(rise/hot/new) 排序并按稳定身份去重（words 视图 new=新奇度）
  → {ok, view, fetched_at, count, dimension_list, dimension_counts, terms}
```
**核心特征：请求路径零上游 IO**。词卡/新闻卡都来自后台预热线程写好的文件缓存。

### 3. 单词聚合 `/api/word/<term>`  （`app.py:840`）  ← 词卡展开「更多」+ 详情页共用
```
GET /api/word/<term>
  → _word_detail(term)  # terms 词池命中 → 报道 LIKE 聚合 + HF 元数据
  → 未命中词池 → 回退 HF live → 再无 → 404
  → 进程内 TTL 缓存
```

### 4. 热词详情 `/term/<name>`  （`app.py:694`）  ← SEO 长尾 + 慢路径
```
GET /term/<name>
  → _detail_cached(key)            # 进程内 TTL 缓存（默认 1800s）
  → miss → _word_detail(term, lang)  # 词池命中（任何词有页）→ 报道聚合；
  │                                  # HF 词额外 live tracker.get_term_detail（~1-4s）
  → data.ok=False → abort(404)
  → render_template("term_detail.html", word=..., ...)
```
因 HF live 区块慢（arXiv 串行检索），靠进程内缓存 + SEO 长尾页价值支撑。

## 后台预热线程（两层）

### tracker 层  （`tracker.py:540` `start_background_refresher`）
- daemon 线程，每个 gunicorn worker 各起一个。
- 循环：`_refresh_once(sort)` 抓 HF 模型榜（trending + top 两种 sort）→ 写 `cache/terms.json`。
- `_cross_proc_lock` (`tracker.py:476`) 用 `fcntl.flock` 跨进程锁，整个容器只有一个 worker 在抓。
- 失败兜底：读旧缓存文件；旧缓存也无 → 内存兜底。

### dims 层  （`dims.py:1530` `start_background_dims_refresher`）
- 独立 daemon 线程，独立跨进程锁 `cache/.dims.refresh.lock`。
- `_dims_refresh_once` (`dims.py:1433`)：拉 17 个 RSS 源 → HN/Reddit 复合热度 → LLM 批量打标（故障转移链）+ 抽关键词（无 key 走降级：词典匹配抽词）→ 写 `cache/dims.json` + `news_store.upsert_cards` 入历史库 → **拿到锁的 worker 再调 `terms.refresh_words` 归并热词池 + 三榜打分 + 周期快照，写 `cache/words.json`**。
- **定点刷新**（Asia/Shanghai）：`DIMS_REFRESH_HOURS = (1,7,13,19)`（`config.py:118`），一天 4 次，6 小时一档。选点避开 DeepSeek 高峰段 + 命中硬盘缓存 TTL。`_seconds_until_next_refresh_hour` (`dims.py:1483`) 算下次刷新倒计时。
- `_persist_to_history` (`dims.py:1414`)：每轮把 cards 持久化到 `news.db`，供 `list_history_cards` 扩大内容池 + `terms` 词聚合扫描。

### 启动时机
`app.py:47-49` 模块加载时即 `start_background_refresher()` + `start_background_dims_refresher()`。每个 worker 进程各起线程，靠 fcntl 锁去重。

## 缓存层级（三级）

| 层级 | 介质 | 作用域 | TTL | 典型键 | 代码位置 |
|------|------|--------|-----|--------|----------|
| L1 内存 | 进程内 `dict` | 单 worker 进程 | 300s（单源）/ 1800s（详情） | `{source: (ts,data)}` | `app.py:61` `_cache` |
| L2 文件 | `cache/*.json` | 跨 worker 共享 | 后台线程刷新频率决定 | `terms.json`, `dims.json`, `words.json` | `tracker.py:62` / `dims.py:77` / `terms.py:91` |
| L3 SQLite | `data/*.db` | 跨 worker 共享，持久 | 永久（历史库）/ 按周期聚合 | sponsors.db, news.db | `store.py` / `news_store.py` / `terms.py` |

跨进程锁文件：`cache/.tracker.refresh.lock`、`cache/.dims.refresh.lock`（`fcntl.flock`）。

降级链：L1 命中 → 秒回；L1 miss → L2 文件；L2 也无 → L3 历史库（dims/terms）/ 内存兜底（tracker）；DB 不可用 → `_DB_OK=False` 全程静默降级，返空不报错。

## 进程模型

- 生产：gunicorn 多 worker（`docker-compose.prod.yml`），每 worker 一个 Python 进程，各起后台线程。
- 锁策略：`threading.Lock` 只进程内有效，故跨 worker 用 `fcntl.flock` 文件锁（历史教训：曾因多 worker × threading.Lock 导致内存耗尽，见 memory `aitrendwatch-server-stability`）。
- 本地：`python app.py` 单进程 debug 模式，`app.py:1400` `app.run(port=5000, debug=True)`。
