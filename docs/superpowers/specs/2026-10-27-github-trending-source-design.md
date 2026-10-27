# GitHub Trending 来源设计

- 日期：2026-10-27
- 状态：已确认
- 范围：在现有 AI 前沿日报中新增 GitHub 热门项目来源

## 1. 目标

每天随日报附上 GitHub Trending 日榜中 AI 相关的前 10 个项目，按“今日新增 star”降序，
包含仓库名、中文摘要、为什么值得关注与链接。

## 2. 非目标

- 不做 GitHub 登录、star、fork 等写操作
- 不覆盖“新项目 star 总榜”或“近期活跃项目榜”
- 不追求全站所有热门项目，只取 Trending 日榜页面

## 3. 数据源

- 端点：`https://github.com/trending?since=daily`
- 方式：`requests` 获取 HTML，正则解析 `<article class="Box-row">` 块
- 每个仓库提取：
  - 仓库全名：`owner/repo`（来自 `<h2>` 内链接）
  - 描述：`p.col-9` 文本，去标签、反转义
  - 今日新增 star：页面 `N stars today`
- 链接：`https://github.com/{owner/repo}`
- 当前页面只返回 10 条，筛完 AI 相关可能不足 10 条，不足部分由其他源顺延补足。

## 4. AI 相关筛选

对 `owner/repo + 描述` 做正则匹配，命中任一即保留：

`ai, llm, gpt, claude, codex, agent/agents/agentic, chatbot, copilot, mcp, rag,
transformer, diffusion, inference, embedding, multimodal, reasoning, machine learning,
deep learning, neural, openai, anthropic, gemini, llama, qwen, deepseek, mistral, model`

`ai` 等短词使用词边界，避免命中 `chair`、`maintain` 等普通单词。
已知会漏掉描述不含关键词的项目（如 DeepTutor），属可接受损失。

## 5. 接入点

- `SOURCE_GROUP` 增加 `"github": "github"`
- `FETCHERS` 增加 `"github": fetch_github`
- `SECTION_ORDER` 增加 `("github", "GitHub 热榜")`
- `Item.source = "github"`，`Item.score = 今日新增 star`，`Item.published = 抓取时刻`
- `Item.raw = 描述 + （今日新增 N star）`，供摘要模型参考
- `seen.json` 按规范化仓库 URL 去重，同一仓库 30 天内不重复出现

## 6. 配置

```toml
[digest]
max_items = 35
quota = { arxiv = 6, vendor = 6, community = 7, chinese = 6, github = 10 }

[sources]
github = true
```

`config.example.toml`、本地 `config.toml` 与 README 同步更新。

## 7. 渲染

- 新增小节标题：`GitHub 热榜`
- GitHub 条目署名显示 `github · 今日 +N star`，不显示抓取时间
- 其余渲染逻辑复用现有 `render_html` / `render_text`

## 8. 错误处理

- HTTP 失败、非 200、解析出 0 条：记录 warning，返回空列表，其他源不受影响
- 单条仓库缺仓库名或链接时跳过
- 描述为空时仍保留项目，`raw` 至少包含 star 数

## 9. 测试

全部离线，使用固定 HTML 片段：

1. 解析 2 个仓库的 name/url/description/stars
2. AI 关键词命中与误命中（`chair` 不应命中 `ai`）
3. 按今日 star 降序排序并截断到 10
4. 页面无 `Box-row` 时返回空列表

## 10. 验收

1. `python -m pytest tests/test_digest.py` 全部通过
2. `python digest.py --dry-run` 生成含 “GitHub 热榜” 小节的 preview.html
3. 次日 08:00 邮件包含最多 10 个 GitHub 项目，链接可打开
4. GitHub 页面解析失败时，邮件其余部分照常发送
