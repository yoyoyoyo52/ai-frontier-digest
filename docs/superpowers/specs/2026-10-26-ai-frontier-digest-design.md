# AI 前沿日报 Agent 设计

- 日期：2026-10-26
- 状态：已确认，待写实现计划
- 形态：单脚本 + Windows 任务计划

## 1. 目标

每天早上 08:00，把过去 24 小时 AI 大模型领域值得看的内容，用中文标题、2-3 句摘要、一句"为什么重要"和原文链接，发送到用户指定的 QQ 邮箱。

## 2. 非目标

- 不做数据库、Web 界面、多用户、历史归档页
- 不做实时推送、退订管理、已读追踪
- 不做内容版权处理，只发送标题、摘要与链接

## 3. 前置条件（用户提供）

1. QQ 邮箱开启 SMTP 服务，取得授权码（非登录密码）
2. DeepSeek API key（platform.deepseek.com）

缺任意一项时，脚本以退出码 3 结束并提示缺少哪个配置，不发信。

## 4. 项目结构

```
C:\Users\74159\ai-frontier-digest\
  digest.py       # 主脚本：fetch → filter → summarize → send
  config.toml     # 邮箱 / API key / 源开关 / 条数
  seen.json       # 30 天已推送 ID
  logs\YYYY-MM-DD.log
  preview.html    # --dry-run 生成，不入库
  tests\test_digest.py
  README.md
  .gitignore
  docs\superpowers\specs\2026-10-26-ai-frontier-digest-design.md
```

`config.toml`、`seen.json`、`logs\`、`preview.html` 写入 `.gitignore`，不入库。

## 5. 数据源

| 源 | 端点 | 抓取策略 | 候选分 |
|---|---|---|---|
| arXiv | `export.arxiv.org/api/query` | cs.CL / cs.LG / cs.AI，`sortBy=submittedDate`，过去 24h | 基础分 60，标题命中关键词 +10 |
| 厂商博客 | OpenAI / Anthropic / Google DeepMind / Meta AI / Qwen / DeepSeek / Mistral 的 RSS | `feedparser` 解析，取 24h 内条目 | 基础分 70 |
| Hacker News | `hn.algolia.com/api/v1/search` | AI 关键词，`points >= 50`，24h 内 | `points` 直接作为分数 |
| Reddit | `r/LocalLLaMA`、`r/MachineLearning` 的 `top.json?t=day` | 自定义 User-Agent；403 或非 JSON 时整源跳过 | `score` 直接作为分数 |
| 中文媒体 | 机器之心、量子位 RSS | `feedparser`，24h 内 | 基础分 65 |

要求：

- 所有 HTTP 请求设置 8 秒超时
- 每个源独立 try/except，单源异常只记日志
- 源的 URL 与开关集中在文件顶部常量与 `[sources]` 配置中
- 实现阶段逐个实测端点；确认失效的源默认关闭并在日志中记录一行

关键词白名单（英文源过滤用，命中任一即候选）：`llm, gpt, claude, gemini, llama, qwen, deepseek, mistral, transformer, diffusion, agent, rag, fine-tune, benchmark, moe, multimodal, reasoning`。中文源不做关键词过滤。

## 6. 数据模型

```python
@dataclass
class Item:
    id: str          # arXiv id / 规范化 URL 的 sha1 前 12 位
    title: str
    url: str
    source: str      # arxiv / vendor / hn / reddit / chinese
    published: str   # ISO 8601
    raw: str         # 原始摘要，截断 400 字
    score: float
```

## 7. 流程

1. **fetch**：并行拉取全部启用的源，合并为候选列表
2. **filter**：
   - 剔除 `seen.json` 中出现的 id
   - 标题去重：`difflib.SequenceMatcher` 相似度 > 0.9 视为重复，保留分数高的
   - 按源配额选取（见下），不足时额度顺延给其他源
3. **summarize**：把选中的 25 条一次性交给 DeepSeek，返回 JSON 数组
4. **send**：发送 HTML 邮件，成功则更新 `seen.json`
5. **prune**：清理 `seen.json` 中 30 天前的记录

源配额（默认，可配置）：arXiv 6、厂商 6、HN+Reddit 7、中文 6。

## 8. 摘要契约

- 端点：`https://api.deepseek.com/chat/completions`，模型 `deepseek-chat`
- 输入：每条 `{id, title, source, raw}`，`raw` 已截断 400 字
- 输出要求：严格 JSON 数组，元素为 `{"id": "...", "cn_title": "...", "summary": "...", "why": "..."}`
- 校验：返回必须能 `json.loads`，且 `id` 集合与输入一致；不满足则视为失败
- 失败降级：不阻断发送，邮件改用原文标题 + 原文摘要摘录，并在邮件顶部标注"本次摘要服务不可用"
- 成本上限：单次调用，输入约 15k token

## 9. 邮件

- SMTP：`smtp.qq.com:465`，SSL，账号密码来自 `config.toml`
- 主题：`AI 前沿日报 · 2026-10-26 · 25 条`
- 正文：HTML，按 arXiv / 厂商动态 / 社区热议 / 中文媒体 四个小节；每条包含中文标题（超链接）、摘要、为什么重要、来源与时间
- 同时附纯文本版本（`EmailMessage` 的 text/plain part）
- 发送失败重试 2 次，间隔 5 秒；仍失败则退出码 2

## 10. 日志与退出码

日志写入 `logs\YYYY-MM-DD.log`，每次运行记录：每源抓取条数、过滤后条数、选中条数、摘要是否降级、发送结果。

| 退出码 | 含义 |
|---|---|
| 0 | 成功发送 |
| 1 | 所有源都未返回可用内容，未发信 |
| 2 | 邮件发送失败 |
| 3 | 配置缺失或无效 |

## 11. 配置

```toml
[mail]
smtp_host = "smtp.qq.com"
smtp_port = 465
user = "example@qq.com"
password = "SMTP授权码"
to = "example@qq.com"

[llm]
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key = "sk-..."

[digest]
send_at = "08:00"
max_items = 25
lookback_hours = 24
quota = { arxiv = 6, vendor = 6, community = 7, chinese = 6 }

[sources]
arxiv = true
vendor_blogs = true
hn = true
reddit = true
chinese_media = true
```

`send_at` 仅作记录，脚本本身不读它；实际触发时间由 Windows 任务计划决定，改时间需同步修改计划任务。`quota` 四项之和应等于 `max_items`，脚本启动时校验，不等则退出码 3。

## 12. 调度

使用 Windows 任务计划程序，每天 08:00 触发，工作目录设为项目目录。

```
schtasks /create /tn "AI Frontier Digest" /tr "\"<python.exe 路径>\" \"C:\Users\74159\ai-frontier-digest\digest.py\"" /sc daily /st 08:00
```

Python 路径以实现时 `where python` 的结果为准；若系统有 `pythonw.exe`，优先使用以避免弹出控制台窗口。

## 13. 测试

`tests\test_digest.py`（pytest），全部使用本地 fixture，不打网络：

1. `normalize_url` 与 `item_id`：同一 URL 不同参数归一为同一 id
2. 标题相似度去重：两条近似标题只保留分数高的
3. 配额选取：某源不足时额度顺延
4. 摘要响应校验：缺字段、id 不匹配、非 JSON 三种情况都走降级
5. HTML 渲染：特殊字符（`<`, `&`）被正确转义

端到端验证：`python digest.py --dry-run` 生成 `preview.html`，人工确认内容与排版后，再用真实配置跑一次真实发送。

## 14. 验收标准

1. 连续 3 天能在 08:00 后 10 分钟内收到邮件
2. 每封 15-25 条，无重复条目
3. 每条都有可点击原文链接，且链接可打开
4. 任一源挂掉时邮件照常到达，日志里有失败记录
5. DeepSeek 不可用时邮件照常到达，并标注降级
6. `--dry-run` 不发送任何邮件

## 15. 风险

- **RSS 端点失效**：实现时逐个实测，失效源默认关闭
- **Reddit 403**：已知会拦截默认请求，使用自定义 UA，失败即跳过该源
- **QQ SMTP 限频**：单日仅一封，不构成风险
- **首次运行无 `seen.json`**：视为空集合，正常执行
