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
| 量子位 | 可用 | 7 天 |
| OpenAI / Anthropic 官方 RSS / 机器之心 | 本网络下不可达或空 feed，暂未启用 | - |

单源失败只写日志，不影响整封信；`seen.json` 保证同一内容不会重复推送。

## 定时任务

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
- 想清空去重记录：删除 `seen.json`
- 有代理后想恢复 OpenAI / Anthropic：把地址加回 `digest.py` 的 `VENDOR_FEEDS`

## 测试

```bash
python -m pytest tests/test_digest.py -v
```
