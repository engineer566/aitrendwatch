# 20260901 需求 8~11 点验证清单

## 任务 8：Gpu/Ui/Glm 大小写统一

### 验证方式
1. **单元测试**（本地 pytest，无 DEEPSEEK_API_KEY）：
   - `normalize_term("gpu") == "GPU"`
   - `normalize_term("Gpu") == "GPU"`
   - `normalize_term("ui") == "UI"`
   - `normalize_term("glM") == "GLM"`
   - 其他常见缩写：API, ML, NLP, LLM, HF, CNN, RNN, GAN 等

2. **测试机验证**（http://47.98.124.167:8080）：
   - 访问首页，检查热词榜中是否有统一大写的缩写词
   - 访问 `/term/gpu`、`/term/ui`、`/term/glm` 等详情页
   - 检查词名显示是否为大写形式

3. **数据验证**：
   - 检查 `cache/words.json` 中的词是否为统一大写
   - 检查 SQLite `terms` 表中的 `term` 列（canonical 键）

---

## 任务 9：埋点监控用户行为轨迹

### 验证方式
1. **前端埋点测试**：
   - 打开浏览器开发者工具 Network 面板
   - 执行以下操作并观察 `/api/event` 请求：
     - 点击热词卡「展开更多」
     - 点击「查看热词」跳转到详情页
     - 切换 views（热词/逐条新闻）
     - 切换语言（zh/en）
     - 切换排序（hot/rise/new）
     - 点击维度 pill
     - 执行搜索

2. **后端 API 测试**：
   - `curl http://47.98.124.167:8080/api/event -X POST -H "Content-Type: application/json" -d '{"event_type":"test","event_data":{"action":"click"}}'`
   - 检查返回状态码

3. **数据库验证**：
   - SSH 到测试机：`ssh -i /data/test_host.pem root@47.98.124.167`
   - `cd /opt/aitrendwatch && docker exec aitrendwatch sqlite3 /app/data/sponsors.db "SELECT * FROM user_events ORDER BY id DESC LIMIT 10;"`

4. **监控页验证**：
   - 登录 admin（需要 ADMIN_TOKEN）
   - 访问 `/monitor`，检查是否显示新的事件统计

---

## 任务 10：合并 monitor 和 admin

### 验证方式
1. **路由测试**：
   - 访问 `/admin`，应该重定向到 `/monitor` 或显示合并后的页面
   - 访问 `/monitor`，应该能看到流量监控和赞助位管理入口

2. **功能测试**：
   - 登录后访问 `/monitor`
   - 检查是否能切换到赞助位管理视图
   - 测试赞助位的 CRUD 操作（新建、编辑、上下架、删除）
   - 测试流量监控数据展示

3. **API 兼容性**：
   - `/admin/sponsors` POST 创建赞助位
   - `/admin/sponsors/<id>/toggle` POST 上下架
   - `/admin/sponsors/<id>/delete` POST 删除
   - `/admin/stats` GET 获取统计
   - `/monitor/api?days=30` GET 获取监控数据

4. **UI 测试**：
   - 主题切换是否正常
   - 响应式布局是否正常
   - 数据加载是否正常

---

## 任务 11：SEO 优化

### 验证方式
1. **Meta 标签检查**（使用浏览器开发者工具或 curl）：
   - 首页 `/`：
     ```bash
     curl http://47.98.124.167:8080/ | grep -E "<(title|meta|link)" | head -20
     ```
   - 热词详情页 `/term/openclaw`：
     ```bash
     curl http://47.98.124.167:8080/term/openclaw | grep -E "<(title|meta|link)" | head -20
     ```
   - HF 页 `/hf`：
     ```bash
     curl http://47.98.124.167:8080/hf | grep -E "<(title|meta|link)" | head -20
     ```

2. **结构化数据验证**：
   - 使用 Google Rich Results Test：https://search.google.com/test/rich-results
   - 输入测试机 URL，检查 ld+json 是否正确解析

3. **Open Graph 标签**：
   - 复制页面链接到微信/QQ/Twitter，检查预览卡片是否显示正确的标题、描述和图片

4. **Sitemap 验证**：
   - 访问 `http://47.98.124.167:8080/sitemap.xml`
   - 检查是否包含所有重要页面（首页、热词详情页、HF 页等）
   - 检查 URL 数量是否在 `SITEMAP_MAX_URLS` 限制内

5. **Robots.txt 验证**：
   - 访问 `http://47.98.124.167:8080/robots.txt`
   - 检查配置是否正确（生产环境应允许索引，测试环境可能需要禁止）

6. **语义化 HTML**：
   - 使用 W3C Validator：https://validator.w3.org/
   - 检查主要页面是否有 HTML 错误

7. **性能测试**：
   - 使用 Lighthouse 或 PageSpeed Insights 测试页面性能
   - 检查关键 CSS 是否内联
   - 检查是否有阻塞渲染的资源

---

## 部署步骤

### 1. 合并 worktree 分支到 dev
```powershell
# 在主仓库根目录执行
.\skills\aitrendwatch-task-workflow\scripts\merge-and-cleanup.ps1 -Branches codex/task8-case-normalize,codex/task9-analytics,codex/task10-merge-admin,codex/task11-seo
```

### 2. 重核索引行号
```bash
# 检查 docs/INDEX.md 和 docs/index/*.md 中的 file:行号 是否正确
wc -l app.py config.py dims.py tracker.py terms.py store.py news_store.py stream_utils.py text_utils.py version.py
grep -n '^def ' app.py config.py dims.py tracker.py terms.py store.py news_store.py stream_utils.py text_utils.py version.py
```

### 3. 本地回归测试
```bash
# 确保没有设置 DEEPSEEK_API_KEY
unset DEEPSEEK_API_KEY
pytest -v
```

### 4. 部署到测试机
```bash
# Windows 本机导出 LF 干净树
git -c core.autocrlf=false archive dev | tar -xf - -C /tmp/aitrendwatch-export

# SCP 变更文件到测试机
scp -i /data/test_host.pem /tmp/aitrendwatch-export/*.py root@47.98.124.167:/opt/aitrendwatch/
scp -i /data/test_host.pem /tmp/aitrendwatch-export/templates/*.html root@47.98.124.167:/opt/aitrendwatch/templates/

# SSH 重启容器
ssh -i /data/test_host.pem root@47.98.124.167 "cd /opt/aitrendwatch && docker compose -f docker-compose.test.yml up -d --force-recreate"
```

### 5. 逐项验证（见上方验证清单）

### 6. 修复问题（如有）
- 在 dev 分支直接修复
- 重新部署变更文件
- 复验

### 7. 推送到远程 dev
```bash
git push origin dev
```

---

## 注意事项

1. **LLM 纪律**：测试机 `.env` 只有 GLM key，无 DeepSeek key；验证降级路径
2. **新增模块挂载**：如果任务 9 新增了 Python 模块，必须在测试机的 `docker-compose.test.yml` 中补挂载
3. **GLM 限流**：如遇 1302/1305 错误，等待窗口重置，不是代码问题
4. **容器预热时间**：重启后约 15-25 分钟完成预热刷新，以 `cache/words.json` 更新为标志
