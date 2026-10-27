# AI 前沿日报

每天 08:00 抓取 AI 大模型前沿内容（arXiv、厂商博客、HN/Reddit、中文媒体），
用 DeepSeek 生成中文摘要，通过 QQ 邮箱发送 HTML 日报。

## 安装

```bash
python -m pip install --user -r requirements.txt
```

## 配置

```bash
copy config.example.toml config.toml
```

然后填写两项凭据：

1. **QQ 邮箱**：设置 → 账户 → 开启 SMTP 服务 → 生成授权码（填 `password`，不是登录密码）
2. **DeepSeek**：在 platform.deepseek.com 创建 API key（填 `api_key`）

`config.toml` 已加入 `.gitignore`，不会入库。

## 试跑

```bash
python digest.py --dry-run    # 只生成 preview.html，不发信
python digest.py              # 真实抓取并发送
```

退出码：`0` 成功 / `1` 无内容 / `2` 发信失败 / `3` 配置错误。

## 数据源与回看窗口

| 源 | 状态 | 回看窗口 |
|---|---|---|
| arXiv (cs.CL/cs.LG/cs.AI) | 可用 | 96 小时（覆盖周末） |
| DeepMind / Meta Engineering / Qwen / Mistral | 可用 | 7 天 |
| Hacker News（points ≥ 50） | 可用 | 24 小时 |
| Reddit r/LocalLLaMA、r/MachineLearning | 当前被 403 拦截，自动跳过 | 24 小时 |
| GitHub Trending（AI 关键词过滤，今日 star 前 10） | 可用 | 日榜 |
| 量子位 | 可用 | 7 天 |
| OpenAI / Anthropic 官方 RSS / 机器之心 | 本网络下不可达或空 feed，暂未启用 | - |

单源失败只写日志，不影响整封信；`seen.json` 保证同一内容不会重复推送。

## GitHub Actions（电脑关机也能跑）

把仓库推送到 GitHub 后，日报在 GitHub 的服务器上运行，不依赖本机开机。

1. 创建一个 **私有仓库**，把本项目推上去，并确认它是默认分支（`master` 或 `main`）。
2. 在仓库 **Settings → Secrets and variables → Actions** 添加 4 个 secret：
   - `MAIL_USER`：QQ 邮箱地址
   - `MAIL_PASSWORD`：QQ 邮箱 SMTP 授权码
   - `MAIL_TO`：收件邮箱（可同 `MAIL_USER`）
   - `DEEPSEEK_API_KEY`：DeepSeek API key
3. 打开 **Actions → AI Frontier Digest → Run workflow** 手动跑一次，确认收到邮件。
4. 云端验证成功后，停用本机任务，避免重复发送：

```bash
schtasks /change /tn "AI Frontier Digest" /disable
```

说明：

- 定时为 UTC `00:00`，即北京时间 08:00；GitHub 可能延迟几分钟到十几分钟。
- 私有仓库每月 2000 分钟免费额度，日报每天约 1–2 分钟，足够。
- `seen.json` 会在每次发送后由 workflow 自动提交回仓库，用于跨天去重，也让仓库保持活跃（60 天无活动会被停用定时任务）。
- 运行日志作为 artifact 保留 7 天，在 Actions 运行页面下载。
- 默认分支若开启分支保护，需要允许 GitHub Actions 推送，或改用 PAT。
- 本机 `config.toml` 不会上传，云端配置由 Secrets 在运行时生成。

## 本地定时任务（Windows）

先确认 Python 路径：

```bash
where python
```

创建每天 08:00 的任务（把 `<python.exe>` 换成实际路径）：

```bash
schtasks /create /tn "AI Frontier Digest" /tr "\"<python.exe>\" \"C:\Users\74159\ai-frontier-digest\digest.py\"" /sc daily /st 08:00
```

已启用失败重试：任务失败（非 0 退出码）后每 10 分钟重跑一次，最多 3 次，
用于覆盖开机后网络/代理尚未就绪的情况。查看、修改与删除：

```bash
schtasks /query /tn "AI Frontier Digest" /v
schtasks /delete /tn "AI Frontier Digest" /f
```

```powershell
# 修改重试次数与间隔
$t = Get-ScheduledTask -TaskName "AI Frontier Digest"
$t.Settings.RestartCount = 3
$t.Settings.RestartInterval = "PT10M"
Set-ScheduledTask -TaskName "AI Frontier Digest" -Settings $t.Settings
```

## 排障

- 日志在 `logs\YYYY-MM-DD.log`，每次运行记录各源回看窗口、抓取数与发送结果
- 某个源连续失败：在 `config.toml` 的 `[sources]` 里把它设为 `false`
- Reddit 返回 403 属预期，脚本会跳过该源继续
- 想清空去重记录：删除 `seen.json`（云端使用时需把删除一起提交）
- 有代理后想恢复 OpenAI / Anthropic：把地址加回 `digest.py` 的 `VENDOR_FEEDS`

## 测试

```bash
python -m pytest tests/test_digest.py -v
```
