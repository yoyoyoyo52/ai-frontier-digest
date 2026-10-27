# GitHub Trending 来源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI 前沿日报中加入 GitHub Trending 日榜的 AI 相关项目（按今日新增 star 取前 10）。

**Architecture:** 在现有 `digest.py` 中新增 `fetch_github`，抓取 trending 页面 HTML 并用正则解析；复用现有 `Item`、去重、配额、摘要与渲染管线。配置的 `max_items` 从 25 提到 35，新增 `github = 10` 配额。

**Tech Stack:** Python 3.13、requests、re、pytest；无新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-27-github-trending-source-design.md`

---

## File Structure

| 文件 | 改动 |
|---|---|
| `digest.py` | 新增 GitHub 解析/抓取、分组映射与小节渲染 |
| `tests/test_digest.py` | 新增解析、过滤、排序、渲染测试；更新配置样例 |
| `config.example.toml` | `max_items`、`quota`、`[sources]` 增加 github |
| `config.toml` | 本地凭据文件同步配置（不入库） |
| `README.md` | 数据源表增加 GitHub |

---

### Task 1: 解析 Trending 页面与 AI 过滤

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [x] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import parse_github_trending

GITHUB_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/Nutlope/hallmark">Nutlope / hallmark</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Anti-AI-slop design skill for Claude Code.</p>
  <span>2,859 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/acme/agent">acme / agent</a></h2>
  <p class="col-9">An LLM agent toolkit.</p>
  <span>1,200 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/acme/chair">acme / chair</a></h2>
  <p class="col-9">A chair for your desk.</p>
  <span>900 stars today</span>
</article>
"""


def test_parse_github_trending_keeps_ai_items_sorted_by_stars():
    """只保留 AI 相关项目，并按今日新增 star 降序。"""
    items = parse_github_trending(GITHUB_HTML, datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [item.title for item in items] == ["Nutlope/hallmark", "acme/agent"]
    assert [item.score for item in items] == [2859.0, 1200.0]
    assert items[0].url == "https://github.com/Nutlope/hallmark"
    assert items[0].source == "github"


def test_parse_github_trending_ignores_ai_inside_words():
    """chair 里的 ai 不应命中关键词。"""
    items = parse_github_trending(GITHUB_HTML, datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert all("chair" not in item.title for item in items)


def test_parse_github_trending_returns_empty_without_articles():
    """页面结构变化时返回空列表，不抛异常。"""
    assert parse_github_trending("<html></html>", datetime(2026, 10, 27, tzinfo=timezone.utc)) == []


def test_parse_github_trending_caps_at_ten():
    """超过 10 条时只保留今日 star 最高的 10 条。"""
    blocks = [
        f'<article class="Box-row"><h2><a href="/acme/r{n}">r{n}</a></h2>'
        f'<p class="col-9">LLM project {n}</p><span>{n} stars today</span></article>'
        for n in range(12)
    ]
    items = parse_github_trending("".join(blocks), datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert len(items) == 10
    assert min(item.score for item in items) == 2.0
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -q -k github`
Expected: FAIL，`ImportError: cannot import name 'parse_github_trending'`

- [x] **Step 3: 写实现**

In `digest.py`, add constants after `REDDIT_FEEDS`:
```python
GITHUB_TRENDING_URL = "https://github.com/trending?since=daily"
GITHUB_TOP_N = 10
GITHUB_AI_PATTERN = re.compile(
    r"(?<![a-z])(ai|llm|gpt|claude|codex|agents?|agentic|chatbot|copilot|mcp|rag|"
    r"transformer|diffusion|inference|embedding|multimodal|reasoning|machine learning|"
    r"deep learning|neural|openai|anthropic|gemini|llama|qwen|deepseek|mistral|model)(?![a-z])",
    re.I,
)
```

Add after `strip_html`:
```python
def parse_github_trending(page_html: str, now: datetime) -> list[Item]:
    """解析 GitHub Trending 页面，保留 AI 相关项目，按今日新增 star 降序。"""
    items: list[Item] = []
    blocks = re.findall(r'<article class="Box-row"[^>]*>(.*?)</article>', page_html, re.S)
    for block in blocks:
        name_match = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', block)
        if not name_match:
            continue
        repo = name_match.group(1).strip()
        stars_match = re.search(r"([\d,]+)\s+stars?\s+today", block)
        stars = int(stars_match.group(1).replace(",", "")) if stars_match else 0
        desc_match = re.search(
            r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', block, re.S
        )
        description = strip_html(desc_match.group(1)) if desc_match else ""
        if not GITHUB_AI_PATTERN.search(f"{repo} {description}"):
            continue
        url = f"https://github.com/{repo}"
        items.append(
            Item(
                id=item_id(url),
                title=repo,
                url=url,
                source="github",
                published=now,
                raw=f"{description}（今日新增 {stars} star）"[:400],
                score=float(stars),
            )
        )

    items.sort(key=lambda item: item.score, reverse=True)
    return items[:GITHUB_TOP_N]
```

- [x] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -q -k github`
Expected: 4 passed

- [x] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: parse github trending ai projects"
```

---

### Task 2: 接入抓取管线与配置

**Files:**
- Modify: `digest.py`
- Modify: `tests/test_digest.py`
- Modify: `config.example.toml`
- Modify: `config.toml`

- [x] **Step 1: 更新配置测试样例**

In `tests/test_digest.py`, change `CONFIG_SAMPLE`:
```toml
[digest]
send_at = "08:00"
max_items = 35
lookback_hours = 24
quota = { arxiv = 6, vendor = 6, community = 7, chinese = 6, github = 10 }

[sources]
arxiv = true
vendor_blogs = true
hn = true
reddit = true
chinese_media = true
github = true
```

Update assertions:
```python
def test_load_config_ok(tmp_path):
    """合法配置应返回 dict 且保留 quota。"""
    cfg = load_config(write_config(tmp_path, CONFIG_SAMPLE))
    assert cfg["digest"]["max_items"] == 35
    assert cfg["digest"]["quota"]["community"] == 7
    assert cfg["digest"]["quota"]["github"] == 10
```

Update the mismatch test:
```python
def test_config_rejects_quota_mismatch(tmp_path):
    """quota 之和与 max_items 不等时抛 ConfigError。"""
    bad = CONFIG_SAMPLE.replace("max_items = 35", "max_items = 34")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad))
```

- [x] **Step 2: 运行配置测试确认通过**

Run: `python -m pytest tests/test_digest.py -q -k config`
Expected: 3 passed（`load_config` 尚未依赖新产物）

- [x] **Step 3: 实现抓取与接入**

In `digest.py`, add after `fetch_chinese`:
```python
def fetch_github(since: datetime) -> list[Item]:
    """抓取 GitHub Trending 日榜中的 AI 项目。"""
    response = requests.get(
        GITHUB_TRENDING_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    items = parse_github_trending(response.text, now_utc())
    if not items:
        log.warning("[github] 页面未解析出 AI 项目")
    return items
```

Add `"github": "github"` to `SOURCE_GROUP`, `"github": fetch_github` to `FETCHERS`,
and `"github": 24` to `SOURCE_LOOKBACK_HOURS`.

- [x] **Step 4: 更新示例与本地配置**

`config.example.toml`:
```toml
[digest]
send_at = "08:00"
max_items = 35
lookback_hours = 24
quota = { arxiv = 6, vendor = 6, community = 7, chinese = 6, github = 10 }

[sources]
arxiv = true
vendor_blogs = true
hn = true
reddit = true
chinese_media = true
github = true
```

`config.toml` 只改 `[digest]` 与 `[sources]` 两段，保留真实凭据。

- [x] **Step 5: 运行全部测试**

Run: `python -m pytest tests/test_digest.py -q`
Expected: 34 passed

- [x] **Step 6: 实测抓取**

Run:
```bash
python -c "from digest import fetch_github, now_utc; items = fetch_github(now_utc()); print(len(items)); [print(i.score, i.title) for i in items]"
```
Expected: 打印 0-10 条，score 为今日新增 star；网络异常时抛错由上层捕获。

- [x] **Step 7: 提交**

```bash
git add digest.py tests/test_digest.py config.example.toml
git commit -m "feat: add github trending source to pipeline"
```

---

### Task 3: 渲染、文档与端到端验证

**Files:**
- Modify: `digest.py`
- Modify: `tests/test_digest.py`
- Modify: `README.md`

- [x] **Step 1: 写失败的渲染测试**

Append to `tests/test_digest.py`:
```python
def test_render_html_shows_github_stars():
    """GitHub 条目署名显示今日新增 star，而不是抓取时间。"""
    item = make_item("owner/repo", 123, "https://github.com/owner/repo")
    item.source = "github"
    html = render_html([item], {}, "2026-10-27", degraded=False)
    assert "GitHub 热榜" in html
    assert "今日 +123 star" in html
```

- [x] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -q -k github_stars`
Expected: FAIL，`assert "GitHub 热榜" in html`

- [x] **Step 3: 实现小节与署名**

In `digest.py`, add to `SECTION_ORDER` at the end:
```python
    ("github", "GitHub 热榜"),
```

In `render_html`, replace the meta line:
```python
            if item.source == "github":
                meta = f"github · 今日 +{int(item.score)} star"
            else:
                meta = f"{item.source} · {published}"
            parts.append(f"<p class='meta'>{escape(meta)}</p>")
```

- [x] **Step 4: 运行全部测试**

Run: `python -m pytest tests/test_digest.py -q`
Expected: 35 passed

- [x] **Step 5: 更新 README 数据源表**

Add a row to the table:
```markdown
| GitHub Trending（AI 关键词过滤，今日 star 前 10） | 可用 | 日榜 |
```

- [x] **Step 6: 端到端 dry-run**

Run: `python digest.py --dry-run`
Expected: 退出码 0；`preview.html` 含 “GitHub 热榜” 小节与 `今日 +N star` 署名。

- [x] **Step 7: 提交**

```bash
git add digest.py tests/test_digest.py README.md
git commit -m "feat: render github trending section"
```

---

## Self-Review

**Spec coverage**

| Spec 要求 | 对应任务 |
|---|---|
| 抓取 trending 页面、解析 name/描述/今日 star | Task 1 |
| AI 关键词过滤与词边界 | Task 1 |
| 按今日 star 降序、截断 10 条 | Task 1 |
| 接入 FETCHERS/SOURCE_GROUP/回看窗口 | Task 2 |
| 配置 35 条与 github=10 配额 | Task 2 |
| “GitHub 热榜” 小节与 star 署名 | Task 3 |
| 解析失败返回空列表、不影响其他源 | Task 1、Task 2 |
| 离线测试与 dry-run 验收 | Task 1-3 |

**Placeholder scan:** 无 TBD/TODO；所有测试与实现代码均完整。

**Type consistency:** `parse_github_trending(page_html, now) -> list[Item]` 在 Task 1 定义、Task 2 调用；`fetch_github(since)` 签名与 `FETCHERS` 其他成员一致；`Item.score` 存今日 star，Task 3 渲染读取同一字段。
