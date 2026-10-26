# AI 前沿日报 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天定时抓取 AI 大模型前沿内容，用 DeepSeek 生成中文摘要，通过 QQ 邮箱 SMTP 发送 HTML 日报。

**Architecture:** 单个 `digest.py`，按 `fetch → filter → summarize → send` 四段组织；配置用 `config.toml`（tomllib 读取），去重状态用 `seen.json`；由 Windows 任务计划程序每日触发。

**Tech Stack:** Python 3.13、requests、feedparser、pytest；DeepSeek Chat Completions API；smtplib（QQ 邮箱 SSL 465）。

**Spec:** `docs/superpowers/specs/2026-10-26-ai-frontier-digest-design.md`

---

## File Structure

| 文件 | 职责 |
|---|---|
| `digest.py` | 全部逻辑：配置、抓取、去重、配额、摘要、渲染、发信、CLI |
| `tests/test_digest.py` | 纯函数单测，不打网络 |
| `config.example.toml` | 配置模板（入库） |
| `config.toml` | 用户真实配置（不入库） |
| `README.md` | 安装、配置、调度、排障 |
| `docs/superpowers/specs/...` | 已提交的设计文档 |

`digest.py` 保持在 500 行以内；超过时再拆 `sources.py`，不在本计划范围内。

---

### Task 1: 项目骨架与配置加载

**Files:**
- Create: `digest.py`
- Create: `tests/test_digest.py`
- Create: `requirements.txt`

- [ ] **Step 1: 安装依赖**

Run:
```bash
cd /c/Users/74159/ai-frontier-digest
python -m pip install --user requests feedparser pytest
python -c "import requests, feedparser, pytest; print('ok')"
```
Expected: `ok`

- [ ] **Step 2: 写依赖清单**

Create `requirements.txt`:
```
requests>=2.32
feedparser>=6.0
pytest>=8.0
```

- [ ] **Step 3: 写失败的配置测试**

Create `tests/test_digest.py`:
```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import ConfigError, load_config

CONFIG_SAMPLE = """
[mail]
smtp_host = "smtp.qq.com"
smtp_port = 465
user = "a@qq.com"
password = "code"
to = "a@qq.com"

[llm]
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key = "sk-test"

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
"""


def write_config(tmp_path: Path, text: str) -> Path:
    """把配置文本写入临时文件并返回路径。"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_ok(tmp_path):
    """合法配置应返回 dict 且保留 quota。"""
    cfg = load_config(write_config(tmp_path, CONFIG_SAMPLE))
    assert cfg["digest"]["max_items"] == 25
    assert cfg["digest"]["quota"]["community"] == 7


def test_config_rejects_quota_mismatch(tmp_path):
    """quota 之和与 max_items 不等时抛 ConfigError。"""
    bad = CONFIG_SAMPLE.replace("max_items = 25", "max_items = 24")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad))


def test_config_rejects_missing_section(tmp_path):
    """缺 [llm] 段时抛 ConfigError。"""
    bad = CONFIG_SAMPLE.split("[llm]")[0] + CONFIG_SAMPLE.split("[digest]")[1]
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad))
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'digest'`

- [ ] **Step 5: 写最小实现**

Create `digest.py`:
```python
"""AI 前沿日报：抓取、筛选、摘要、投递。"""

from __future__ import annotations

import argparse
import json
import logging
import smtplib
import sys
import time
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
SEEN_PATH = ROOT / "seen.json"
LOG_DIR = ROOT / "logs"
PREVIEW_PATH = ROOT / "preview.html"

USER_AGENT = "Mozilla/5.0 (compatible; ai-frontier-digest/1.0)"
REQUEST_TIMEOUT = 8

log = logging.getLogger("digest")


class ConfigError(Exception):
    """配置缺失或取值非法。"""


def load_config(path: Path = CONFIG_PATH) -> dict:
    """读取并校验 config.toml，返回配置字典。"""
    import tomllib

    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")

    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml 解析失败：{exc}") from exc

    required = {
        "mail": ("smtp_host", "smtp_port", "user", "password", "to"),
        "llm": ("base_url", "model", "api_key"),
        "digest": ("max_items", "lookback_hours", "quota"),
        "sources": (),
    }
    for section, keys in required.items():
        if section not in cfg:
            raise ConfigError(f"配置缺少 [{section}] 段")
        for key in keys:
            if not cfg[section].get(key):
                raise ConfigError(f"配置缺少 {section}.{key}")

    quota = cfg["digest"]["quota"]
    if sum(quota.values()) != int(cfg["digest"]["max_items"]):
        raise ConfigError("quota 四项之和必须等于 max_items")

    return cfg
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 3 passed

- [ ] **Step 7: 提交**

```bash
git add digest.py tests/test_digest.py requirements.txt
git commit -m "feat: add config loader with validation"
```

---

### Task 2: 条目模型与 URL 规范化

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from datetime import datetime, timezone

from digest import Item, item_id, normalize_url, title_key


def test_normalize_url_strips_tracking_and_fragment():
    """utm_* 参数与 fragment 被移除，www 被去掉。"""
    url = "https://www.example.com/post/?utm_source=x&id=7#section"
    assert normalize_url(url) == "https://example.com/post?id=7"


def test_item_id_is_stable_across_url_variants():
    """同一文章的不同 URL 变体得到同一 id。"""
    a = "https://example.com/a?utm_medium=rss"
    b = "https://www.example.com/a/"
    assert item_id(a) == item_id(b)
    assert len(item_id(a)) == 12


def test_item_id_differs_for_different_pages():
    """不同 URL 的 id 必须不同。"""
    assert item_id("https://example.com/a") != item_id("https://example.com/b")


def test_title_key_ignores_case_and_punctuation():
    """标题归一化后忽略大小写与标点。"""
    assert title_key("LLM: The Next Wave!") == title_key("llm the next wave")


def test_item_holds_datetime():
    """Item.published 保持 datetime 类型。"""
    item = Item(
        id="abc",
        title="t",
        url="https://example.com",
        source="arxiv",
        published=datetime(2026, 10, 26, tzinfo=timezone.utc),
        raw="r",
        score=60.0,
    )
    assert item.published.year == 2026
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'Item'`

- [ ] **Step 3: 写实现**

In `digest.py`, add imports (replace the existing import block's stdlib section):
```python
import argparse
import hashlib
import html
import json
import logging
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
```

Add after `log = logging.getLogger("digest")`:
```python
@dataclass
class Item:
    """一条待推送内容。"""

    id: str
    title: str
    url: str
    source: str
    published: datetime
    raw: str
    score: float


def normalize_url(url: str) -> str:
    """去掉跟踪参数、fragment 与 www 前缀，输出可比较的 URL。"""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    drop = {"ref", "source", "fbclid", "gclid"}
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in drop
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def item_id(url: str) -> str:
    """由规范化 URL 生成稳定的 12 位 id。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


def title_key(title: str) -> str:
    """标题归一化键：小写、去标点、压空白。"""
    lowered = title.lower()
    cleaned = re.sub(r"[^\w一-鿿]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: add item model and url normalization"
```

---

### Task 3: 标题去重

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import dedupe_items


def make_item(title: str, score: float, url: str = "https://example.com/x") -> Item:
    """构造测试用 Item。"""
    return Item(
        id=item_id(url),
        title=title,
        url=url,
        source="hn",
        published=datetime(2026, 10, 26, tzinfo=timezone.utc),
        raw="raw",
        score=score,
    )


def test_dedupe_keeps_higher_score():
    """近似标题只保留分数高的一条。"""
    low = make_item("OpenAI releases GPT-5.5 model", 50, "https://example.com/a")
    high = make_item("OpenAI releases GPT-5.5 model!", 90, "https://example.com/b")
    other = make_item("Meta open-sources new vision model", 70, "https://example.com/c")

    kept = dedupe_items([low, high, other])

    assert [item.score for item in kept] == [90, 70]


def test_dedupe_keeps_distinct_titles():
    """不相似的标题全部保留。"""
    a = make_item("Claude gets a new context window", 80, "https://example.com/a")
    b = make_item("Llama 5 benchmark results", 60, "https://example.com/b")
    assert len(dedupe_items([a, b])) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'dedupe_items'`

- [ ] **Step 3: 写实现**

Add to `digest.py`:
```python
TITLE_SIMILARITY_THRESHOLD = 0.9


def titles_are_similar(left: str, right: str) -> bool:
    """两个标题归一化后相似度是否超过阈值。"""
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() > TITLE_SIMILARITY_THRESHOLD


def dedupe_items(items: list[Item]) -> list[Item]:
    """按分数从高到低保留标题不重复的条目。"""
    kept: list[Item] = []
    for item in sorted(items, key=lambda i: (i.score, i.published), reverse=True):
        if any(titles_are_similar(item.title, other.title) for other in kept):
            continue
        kept.append(item)
    return kept
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: dedupe items by title similarity"
```

---

### Task 4: 源配额选取与顺延

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import select_items

QUOTA = {"arxiv": 2, "vendor": 1, "community": 1, "chinese": 1}


def test_select_respects_quota():
    """每个分组不超过配额。"""
    items = [make_item(f"arxiv {n}", 60, f"https://example.com/a{n}") for n in range(5)]
    for item in items:
        item.source = "arxiv"

    selected = select_items(items, QUOTA, max_items=5)

    assert len(selected) == 2
    assert all(item.source == "arxiv" for item in selected)


def test_select_redistributes_unfilled_quota():
    """某分组不足时，空出的名额顺延给有剩余的组。"""
    arxiv = [make_item(f"arxiv {n}", 60 + n, f"https://example.com/a{n}") for n in range(1)]
    community = [make_item(f"community {n}", 80 + n, f"https://example.com/c{n}") for n in range(5)]
    for item in arxiv:
        item.source = "arxiv"
    for item in community:
        item.source = "hn"

    selected = select_items(arxiv + community, QUOTA, max_items=5)

    assert len(selected) == 5
    assert sum(1 for item in selected if item.source == "arxiv") == 1


def test_select_never_exceeds_max_items():
    """可用条目多于 max_items 时只取上限数量。"""
    items = [make_item(f"item {n}", n, f"https://example.com/i{n}") for n in range(20)]
    for item in items:
        item.source = "vendor"
    selected = select_items(items, QUOTA, max_items=4)
    assert len(selected) == 4
    assert min(item.score for item in selected) >= 16
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'select_items'`

- [ ] **Step 3: 写实现**

Add to `digest.py`:
```python
SOURCE_GROUP = {
    "arxiv": "arxiv",
    "vendor": "vendor",
    "hn": "community",
    "reddit": "community",
    "chinese": "chinese",
}


def select_items(items: list[Item], quota: dict, max_items: int) -> list[Item]:
    """按分组配额选取条目，不足的配额顺延给有剩余的组。"""
    buckets: dict[str, list[Item]] = {group: [] for group in quota}
    for item in items:
        group = SOURCE_GROUP.get(item.source)
        if group in buckets:
            buckets[group].append(item)

    for bucket in buckets.values():
        bucket.sort(key=lambda i: (i.score, i.published), reverse=True)

    selected: list[Item] = []
    used = {group: 0 for group in buckets}
    for group, limit in quota.items():
        take = buckets[group][: int(limit)]
        selected.extend(take)
        used[group] = len(take)

    remaining = int(max_items) - len(selected)
    while remaining > 0:
        leftovers = [
            (group, len(buckets[group]) - used[group])
            for group in buckets
            if len(buckets[group]) > used[group]
        ]
        if not leftovers:
            break
        group, _ = max(leftovers, key=lambda pair: pair[1])
        selected.append(buckets[group][used[group]])
        used[group] += 1
        remaining -= 1

    return selected[: int(max_items)]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 13 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: select items by source quota with redistribution"
```

---

### Task 5: 已推送记录（seen.json）

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import load_seen, prune_seen, save_seen


def test_seen_roundtrip(tmp_path):
    """写入后能原样读回。"""
    path = tmp_path / "seen.json"
    save_seen({"abc": "2026-10-26"}, path)
    assert load_seen(path) == {"abc": "2026-10-26"}


def test_load_seen_missing_file_returns_empty(tmp_path):
    """文件不存在时返回空字典，不抛异常。"""
    assert load_seen(tmp_path / "nope.json") == {}


def test_prune_seen_drops_old_entries():
    """超过 30 天的记录被清理。"""
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    seen = {"old": "2026-09-01", "new": "2026-10-25"}
    pruned = prune_seen(seen, now, days=30)
    assert pruned == {"new": "2026-10-25"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'load_seen'`

- [ ] **Step 3: 写实现**

Add to `digest.py`:
```python
SEEN_RETENTION_DAYS = 30


def load_seen(path: Path = SEEN_PATH) -> dict[str, str]:
    """读取已推送记录；文件缺失或损坏时返回空字典。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("seen.json 无法读取，按空记录处理")
        return {}
    return data if isinstance(data, dict) else {}


def save_seen(seen: dict[str, str], path: Path = SEEN_PATH) -> None:
    """原子写入已推送记录。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def prune_seen(
    seen: dict[str, str], now: datetime, days: int = SEEN_RETENTION_DAYS
) -> dict[str, str]:
    """删除超过保留期的记录。"""
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    return {key: value for key, value in seen.items() if value >= cutoff}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 16 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: persist and prune seen ids"
```

---

### Task 6: 抓取四类源

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
import feedparser

from digest import parse_entry_date, strip_html

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Test entry</title>
    <link href="https://example.com/atom-entry"/>
    <updated>2026-10-25T10:30:00Z</updated>
    <summary>Some &lt;b&gt;rich&lt;/b&gt; text</summary>
  </entry>
</feed>"""


def test_parse_entry_date_reads_iso_updated():
    """Atom 的 ISO 时间能被解析为 UTC datetime。"""
    feed = feedparser.parse(ATOM_SAMPLE)
    parsed = parse_entry_date(feed.entries[0])
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.hour == 10
    assert parsed.tzinfo is not None


def test_strip_html_removes_tags_and_unescapes():
    """摘要里的 HTML 标签被剥离并反转义。"""
    assert strip_html("Some <b>rich</b> &amp; text") == "Some rich & text"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'parse_entry_date'`

- [ ] **Step 3: 写解析与抓取实现**

Add to `digest.py`:
```python
KEYWORDS = (
    "llm", "gpt", "claude", "gemini", "llama", "qwen", "deepseek", "mistral",
    "transformer", "diffusion", "agent", "rag", "fine-tune", "benchmark",
    "moe", "multimodal", "reasoning",
)

BASE_SCORE = {
    "arxiv": 60.0,
    "vendor": 70.0,
    "hn": 0.0,
    "reddit": 0.0,
    "chinese": 65.0,
}

VENDOR_FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Anthropic": "https://www.anthropic.com/rss.xml",
    "DeepMind": "https://deepmind.google/blog/rss.xml",
    "Meta AI": "https://ai.meta.com/blog/rss/",
    "Qwen": "https://qwenlm.github.io/blog/index.xml",
    "Mistral": "https://mistral.ai/news/feed.xml",
}

CHINESE_FEEDS = {
    "机器之心": "https://www.jiqizhixin.com/rss",
    "量子位": "https://www.qbitai.com/feed",
}

ARXIV_API = "http://export.arxiv.org/api/query"
HN_API = "https://hn.algolia.com/api/v1/search_by_date"
REDDIT_FEEDS = {
    "LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=25",
    "MachineLearning": "https://www.reddit.com/r/MachineLearning/top.json?t=day&limit=25",
}


def strip_html(text: str) -> str:
    """去掉 HTML 标签并压缩空白。"""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    unescaped = html.unescape(without_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def parse_entry_date(entry) -> datetime | None:
    """从 feed 条目解析发布时间，返回 UTC datetime 或 None。"""
    for attr in ("published", "updated"):
        raw = entry.get(attr)
        if not raw:
            continue
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

    for attr in ("published_parsed", "updated_parsed"):
        struct = entry.get(attr)
        if struct:
            return datetime(*struct[:6], tzinfo=timezone.utc)
    return None


def load_feed(url: str):
    """带超时与 UA 地抓取并解析一个 feed。"""
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return feedparser.parse(response.content)


def has_keyword(text: str) -> bool:
    """标题是否命中 AI 关键词表。"""
    lowered = text.lower()
    return any(word in lowered for word in KEYWORDS)


def make_scored_item(
    *, url: str, title: str, source: str, published: datetime, raw: str, score: float
) -> Item:
    """构造 Item，统一生成 id。"""
    return Item(
        id=item_id(url),
        title=title.strip(),
        url=url,
        source=source,
        published=published,
        raw=strip_html(raw)[:400],
        score=float(score),
    )


def fetch_feed_source(url: str, source: str, since: datetime) -> list[Item]:
    """从单个 RSS/Atom 源抓取窗口内条目。"""
    feed = load_feed(url)
    items: list[Item] = []
    for entry in feed.entries:
        published = parse_entry_date(entry)
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not published or published < since or not title or not link:
            continue
        raw = entry.get("summary") or entry.get("description") or ""
        score = BASE_SCORE[source]
        if has_keyword(title):
            score += 10
        items.append(
            make_scored_item(
                url=link, title=title, source=source, published=published, raw=raw, score=score
            )
        )
    return items


def fetch_arxiv(since: datetime) -> list[Item]:
    """抓取 arXiv cs.CL/cs.LG/cs.AI 最新提交。"""
    params = {
        "search_query": "cat:cs.CL OR cat:cs.LG OR cat:cs.AI",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": "80",
    }
    response = requests.get(
        ARXIV_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    items: list[Item] = []
    for entry in feed.entries:
        published = parse_entry_date(entry)
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not published or published < since or not title or not link:
            continue
        score = BASE_SCORE["arxiv"] + (10 if has_keyword(title) else 0)
        items.append(
            make_scored_item(
                url=link,
                title=title,
                source="arxiv",
                published=published,
                raw=entry.get("summary") or "",
                score=score,
            )
        )
    return items


def fetch_vendor(since: datetime) -> list[Item]:
    """抓取厂商官方博客，单源失败不影响其他源。"""
    items: list[Item] = []
    for name, url in VENDOR_FEEDS.items():
        try:
            items.extend(fetch_feed_source(url, "vendor", since))
        except Exception as exc:  # 单源失败只记录
            log.warning("[vendor/%s] 失败：%s", name, exc)
    return items


def fetch_chinese(since: datetime) -> list[Item]:
    """抓取中文科技媒体，不做关键词过滤。"""
    items: list[Item] = []
    for name, url in CHINESE_FEEDS.items():
        try:
            items.extend(fetch_feed_source(url, "chinese", since))
        except Exception as exc:
            log.warning("[chinese/%s] 失败：%s", name, exc)
    return items


def fetch_hn(since: datetime) -> list[Item]:
    """抓取 HN 高分 AI 讨论。"""
    threshold = int(since.timestamp())
    items: list[Item] = []
    seen_ids: set[str] = set()
    for query in ("LLM", "AI model"):
        response = requests.get(
            HN_API,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{threshold},points>50",
                "hitsPerPage": "40",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        for hit in response.json().get("hits", []):
            object_id = str(hit.get("objectID") or "")
            title = (hit.get("title") or "").strip()
            if not object_id or not title:
                continue
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
            identifier = item_id(url)
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            published = datetime.fromtimestamp(int(hit.get("created_at_i", 0)), tz=timezone.utc)
            items.append(
                make_scored_item(
                    url=url,
                    title=title,
                    source="hn",
                    published=published,
                    raw=hit.get("story_text") or hit.get("title") or "",
                    score=float(hit.get("points") or 0),
                )
            )
    return items


def fetch_reddit(since: datetime) -> list[Item]:
    """抓取 Reddit 当日热帖；被拦截时抛异常由上层跳过。"""
    items: list[Item] = []
    for name, url in REDDIT_FEEDS.items():
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise RuntimeError(f"{name} 返回 {response.status_code}")
        try:
            children = response.json()["data"]["children"]
        except (ValueError, KeyError) as exc:
            raise RuntimeError(f"{name} 返回非 JSON") from exc
        for child in children:
            data = child.get("data", {})
            title = (data.get("title") or "").strip()
            permalink = data.get("permalink") or ""
            if not title or not permalink:
                continue
            published = datetime.fromtimestamp(int(data.get("created_utc", 0)), tz=timezone.utc)
            if published < since:
                continue
            items.append(
                make_scored_item(
                    url=f"https://www.reddit.com{permalink}",
                    title=title,
                    source="reddit",
                    published=published,
                    raw=data.get("selftext") or "",
                    score=float(data.get("score") or 0),
                )
            )
    return items


FETCHERS = {
    "arxiv": fetch_arxiv,
    "vendor_blogs": fetch_vendor,
    "hn": fetch_hn,
    "reddit": fetch_reddit,
    "chinese_media": fetch_chinese,
}


def fetch_all(cfg: dict, since: datetime) -> list[Item]:
    """按配置抓取全部启用的源，单源失败只记日志。"""
    items: list[Item] = []
    for key, fetcher in FETCHERS.items():
        if not cfg["sources"].get(key, True):
            continue
        try:
            found = fetcher(since)
            log.info("[%s] 获取 %d 条", key, len(found))
            items.extend(found)
        except Exception as exc:
            log.warning("[%s] 失败：%s", key, exc)
    return items
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 18 passed

- [ ] **Step 5: 实测所有 feed 端点（网络）**

Run:
```bash
cd /c/Users/74159/ai-frontier-digest
python - <<'PY'
import requests, feedparser
from digest import VENDOR_FEEDS, CHINESE_FEEDS
for name, url in {**VENDOR_FEEDS, **CHINESE_FEEDS}.items():
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        feed = feedparser.parse(r.content)
        print(f"{name:12} status={r.status_code} entries={len(feed.entries)}")
    except Exception as exc:
        print(f"{name:12} FAIL {exc}")
PY
```
Expected: 每个源打印 status 与 entries。**对 entries=0 或 status 非 200 的源，从 `VENDOR_FEEDS` / `CHINESE_FEEDS` 中删除该行并重跑一次**，确保剩下的都是可用源。

- [ ] **Step 6: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: fetch arxiv, vendor rss, hn, reddit and chinese media"
```

---

### Task 7: DeepSeek 摘要与降级

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import validate_summary


def test_validate_summary_accepts_complete_payload():
    """id 集合完全匹配时校验通过。"""
    payload = {
        "items": [
            {"id": "a", "cn_title": "甲", "summary": "乙", "why": "丙"},
            {"id": "b", "cn_title": "丁", "summary": "戊", "why": "己"},
        ]
    }
    assert validate_summary(payload, {"a", "b"}) is True


def test_validate_summary_rejects_missing_id():
    """缺少任意一条即判失败。"""
    payload = {"items": [{"id": "a", "cn_title": "甲", "summary": "乙", "why": "丙"}]}
    assert validate_summary(payload, {"a", "b"}) is False


def test_validate_summary_rejects_unknown_id():
    """出现输入之外的 id 判失败。"""
    payload = {"items": [{"id": "z", "cn_title": "甲", "summary": "乙", "why": "丙"}]}
    assert validate_summary(payload, {"a"}) is False


def test_validate_summary_rejects_empty_field():
    """字段为空字符串判失败。"""
    payload = {"items": [{"id": "a", "cn_title": "", "summary": "乙", "why": "丙"}]}
    assert validate_summary(payload, {"a"}) is False


def test_validate_summary_rejects_non_dict():
    """顶层不是对象判失败。"""
    assert validate_summary([], {"a"}) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'validate_summary'`

- [ ] **Step 3: 写实现**

Add to `digest.py`:
```python
SUMMARY_SYSTEM_PROMPT = (
    "你是 AI 行业分析师。用户会给你一批最近 24 小时的技术内容条目。"
    "请为每一条写出中文标题、2-3 句中文摘要、以及一句话说明为什么值得关注。"
    "只输出 JSON，格式为 {\"items\": [{\"id\": \"...\", \"cn_title\": \"...\", "
    "\"summary\": \"...\", \"why\": \"...\"}]}。id 必须与输入一致，不得增删条目。"
)


def validate_summary(payload, expected_ids: set[str]) -> bool:
    """校验模型返回：必须是对象、条目齐全、字段非空、id 不重复。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return False
    seen: set[str] = set()
    for row in payload["items"]:
        if not isinstance(row, dict):
            return False
        for key in ("id", "cn_title", "summary", "why"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                return False
        if row["id"] not in expected_ids or row["id"] in seen:
            return False
        seen.add(row["id"])
    return seen == expected_ids


def summarize_items(items: list[Item], cfg: dict) -> tuple[dict[str, dict], bool]:
    """调用 DeepSeek 生成中文摘要，失败时返回空映射并标记降级。"""
    if not items:
        return {}, False

    payload = [
        {"id": item.id, "title": item.title, "source": item.source, "raw": item.raw}
        for item in items
    ]
    body = {
        "model": cfg["llm"]["model"],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
    }
    url = cfg["llm"]["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['llm']['api_key']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, json=body, headers=headers, timeout=60)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        log.warning("摘要生成失败，降级为原文摘录：%s", exc)
        return {}, True

    expected = {item.id for item in items}
    if not validate_summary(parsed, expected):
        log.warning("摘要返回不合契约，降级为原文摘录")
        return {}, True

    return {row["id"]: row for row in parsed["items"]}, False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 23 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: summarize with deepseek and degrade gracefully"
```

---

### Task 8: HTML 与纯文本渲染

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
from digest import render_html, render_text


def test_render_html_escapes_content():
    """标题和摘要中的 HTML 被转义。"""
    item = make_item("<script>alert(1)</script>", 80, "https://example.com/x")
    html = render_html([item], {}, "2026-10-26", degraded=False)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_uses_summary_when_available():
    """有摘要时渲染中文标题与 why。"""
    item = make_item("Original title", 80, "https://example.com/x")
    summaries = {item.id: {"cn_title": "中文标题", "summary": "摘要正文", "why": "值得关注"}}
    html = render_html([item], summaries, "2026-10-26", degraded=False)
    assert "中文标题" in html
    assert "摘要正文" in html
    assert "值得关注" in html


def test_render_html_marks_degraded():
    """降级时邮件顶部有提示。"""
    item = make_item("t", 80, "https://example.com/x")
    html = render_html([item], {}, "2026-10-26", degraded=True)
    assert "摘要服务不可用" in html


def test_render_text_contains_links():
    """纯文本版本包含标题与 URL。"""
    item = make_item("t", 80, "https://example.com/x")
    text = render_text([item], {}, "2026-10-26", degraded=False)
    assert "https://example.com/x" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`ImportError: cannot import name 'render_html'`

- [ ] **Step 3: 写实现**

Add to `digest.py`:
```python
SECTION_ORDER = (
    ("arxiv", "arXiv 论文"),
    ("vendor", "厂商动态"),
    ("community", "社区热议"),
    ("chinese", "中文媒体"),
)


def group_items(items: list[Item]) -> dict[str, list[Item]]:
    """按展示分组归类，保持组内原顺序。"""
    groups: dict[str, list[Item]] = {group: [] for group, _ in SECTION_ORDER}
    for item in items:
        group = SOURCE_GROUP.get(item.source)
        if group in groups:
            groups[group].append(item)
    return groups


def item_fields(item: Item, summaries: dict[str, dict]) -> tuple[str, str, str]:
    """返回 (标题, 摘要, 为什么重要)，无摘要时回退原文。"""
    row = summaries.get(item.id)
    if row:
        return row["cn_title"], row["summary"], row["why"]
    fallback = item.raw or "（暂无摘要，请点开原文）"
    return item.title, fallback, ""


def render_html(
    items: list[Item], summaries: dict[str, dict], date_str: str, degraded: bool
) -> str:
    """渲染 HTML 邮件正文。"""
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<style>",
        "body{font-family:-apple-system,'Segoe UI',sans-serif;max-width:720px;margin:0 auto;",
        "padding:24px;color:#1a1a1a;line-height:1.65}",
        "h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 12px;",
        "border-bottom:1px solid #e5e5e5;padding-bottom:6px}",
        ".item{margin:0 0 20px}.item h3{font-size:15px;margin:0 0 6px}",
        ".item p{margin:0 0 6px;font-size:14px}",
        ".why{color:#0b6b3a;font-size:13px}.meta{color:#888;font-size:12px}",
        ".banner{background:#fff4e5;border:1px solid #ffd8a8;padding:10px 12px;",
        "font-size:13px;margin:16px 0}",
        "a{color:#0b5fff;text-decoration:none}",
        "</style></head><body>",
        f"<h1>AI 前沿日报</h1><p class='meta'>{escape(date_str)} · 共 {len(items)} 条</p>",
    ]
    if degraded:
        parts.append("<div class='banner'>本次摘要服务不可用，以下为原文标题与摘录。</div>")

    groups = group_items(items)
    for group, label in SECTION_ORDER:
        bucket = groups[group]
        if not bucket:
            continue
        parts.append(f"<h2>{escape(label)}</h2>")
        for item in bucket:
            title, summary, why = item_fields(item, summaries)
            published = item.published.astimezone().strftime("%m-%d %H:%M")
            parts.append("<div class='item'>")
            parts.append(
                f"<h3><a href='{escape(item.url)}'>{escape(title)}</a></h3>"
            )
            parts.append(f"<p>{escape(summary)}</p>")
            if why:
                parts.append(f"<p class='why'>为什么重要：{escape(why)}</p>")
            parts.append(
                f"<p class='meta'>{escape(item.source)} · {escape(published)}</p>"
            )
            parts.append("</div>")

    parts.append("</body></html>")
    return "".join(parts)


def render_text(
    items: list[Item], summaries: dict[str, dict], date_str: str, degraded: bool
) -> str:
    """渲染纯文本备份正文。"""
    lines = [f"AI 前沿日报 {date_str} · 共 {len(items)} 条", ""]
    if degraded:
        lines.extend(["本次摘要服务不可用，以下为原文标题与摘录。", ""])

    groups = group_items(items)
    for group, label in SECTION_ORDER:
        bucket = groups[group]
        if not bucket:
            continue
        lines.append(f"== {label} ==")
        for item in bucket:
            title, summary, why = item_fields(item, summaries)
            lines.append(f"- {title}")
            lines.append(f"  {summary}")
            if why:
                lines.append(f"  为什么重要：{why}")
            lines.append(f"  {item.url}")
            lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 27 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: render html and text digests"
```

---

### Task 9: 邮件发送与重试

**Files:**
- Modify: `digest.py`
- Test: `tests/test_digest.py`

- [ ] **Step 1: 写失败的测试**

Append to `tests/test_digest.py`:
```python
import digest


class FakeSMTP:
    """记录调用并在前 N 次抛异常的假 SMTP 客户端。"""

    attempts = 0
    fail_times = 0

    def __init__(self, host, port, timeout=20):
        self.host = host
        self.port = port

    def __enter__(self):
        FakeSMTP.attempts += 1
        if FakeSMTP.attempts <= FakeSMTP.fail_times:
            raise OSError("boom")
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, user, password):
        return None

    def send_message(self, message):
        return None


def test_send_mail_retries_then_raises(monkeypatch):
    """连续失败达到上限后抛 MailError，并记录尝试次数。"""
    FakeSMTP.attempts = 0
    FakeSMTP.fail_times = 99
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    cfg = {
        "mail": {
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "user": "a@qq.com",
            "password": "code",
            "to": "a@qq.com",
        }
    }
    with pytest.raises(digest.MailError):
        digest.send_mail(cfg, "subject", "<p>x</p>", "x", attempts=2, delay=0)
    assert FakeSMTP.attempts == 2


def test_send_mail_succeeds_after_retry(monkeypatch):
    """第一次失败、第二次成功时不抛异常。"""
    FakeSMTP.attempts = 0
    FakeSMTP.fail_times = 1
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    cfg = {
        "mail": {
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "user": "a@qq.com",
            "password": "code",
            "to": "a@qq.com",
        }
    }
    digest.send_mail(cfg, "subject", "<p>x</p>", "x", attempts=2, delay=0)
    assert FakeSMTP.attempts == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_digest.py -v`
Expected: FAIL，`AttributeError: module 'digest' has no attribute 'MailError'`

- [ ] **Step 3: 写实现**

Add to `digest.py` (after `ConfigError`):
```python
class MailError(Exception):
    """邮件发送失败。"""
```

Add at the end of the file:
```python
def send_mail(
    cfg: dict, subject: str, html_body: str, text_body: str, attempts: int = 3, delay: int = 5
) -> None:
    """通过 QQ 邮箱 SSL 发送 HTML 邮件，失败重试。"""
    mail_cfg = cfg["mail"]
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_cfg["user"]
    message["To"] = mail_cfg["to"]
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with smtplib.SMTP_SSL(
                mail_cfg["smtp_host"], int(mail_cfg["smtp_port"]), timeout=20
            ) as client:
                client.login(mail_cfg["user"], mail_cfg["password"])
                client.send_message(message)
            log.info("邮件已发送：%s", subject)
            return
        except Exception as exc:
            last_error = exc
            log.warning("第 %d 次发送失败：%s", attempt, exc)
            if attempt < attempts:
                time.sleep(delay)

    raise MailError(f"邮件发送失败：{last_error}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 29 passed

- [ ] **Step 5: 提交**

```bash
git add digest.py tests/test_digest.py
git commit -m "feat: send html mail with retry"
```

---

### Task 10: 主流程、日志与 CLI

**Files:**
- Modify: `digest.py`

- [ ] **Step 1: 写主流程实现**

Add to the end of `digest.py`:
```python
def now_utc() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def setup_logging() -> None:
    """同时写文件与终端。"""
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(
        LOG_DIR / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(file_handler)
    log.addHandler(stream_handler)


def run(cfg: dict, dry_run: bool) -> int:
    """执行一次完整流程，返回退出码。"""
    now = now_utc()
    since = now - timedelta(hours=int(cfg["digest"]["lookback_hours"]))
    seen = load_seen()

    candidates = fetch_all(cfg, since)
    fresh = [item for item in candidates if item.id not in seen]
    unique = dedupe_items(fresh)
    selected = select_items(
        unique, cfg["digest"]["quota"], int(cfg["digest"]["max_items"])
    )
    log.info("候选 %d，去重后 %d，选中 %d", len(candidates), len(unique), len(selected))

    if not selected:
        log.error("没有可发送的内容")
        return 1

    summaries, degraded = summarize_items(selected, cfg)
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    html_body = render_html(selected, summaries, today, degraded)
    text_body = render_text(selected, summaries, today, degraded)

    if dry_run:
        PREVIEW_PATH.write_text(html_body, encoding="utf-8")
        print(f"已生成预览：{PREVIEW_PATH}")
        return 0

    subject = f"AI 前沿日报 · {today} · {len(selected)} 条"
    try:
        send_mail(cfg, subject, html_body, text_body)
    except MailError as exc:
        log.error("%s", exc)
        return 2

    updated = dict(seen)
    for item in selected:
        updated[item.id] = today
    save_seen(prune_seen(updated, now))
    log.info("发送成功，%d 条", len(selected))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="AI 前沿日报")
    parser.add_argument("--dry-run", action="store_true", help="只生成 preview.html，不发信")
    args = parser.parse_args(argv)

    setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        print(f"配置错误：{exc}", file=sys.stderr)
        return 3

    return run(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 运行全部测试**

Run: `python -m pytest tests/test_digest.py -v`
Expected: 29 passed

- [ ] **Step 3: 检查模块可导入且 CLI 有帮助**

Run: `python digest.py --help`
Expected: 打印用法，包含 `--dry-run`

- [ ] **Step 4: 提交**

```bash
git add digest.py
git commit -m "feat: wire up main flow, logging and cli"
```

---

### Task 11: 配置模板与 README

**Files:**
- Create: `config.example.toml`
- Create: `README.md`

- [ ] **Step 1: 写配置模板**

Create `config.example.toml`:
```toml
[mail]
smtp_host = "smtp.qq.com"
smtp_port = 465
user = "你的QQ邮箱@qq.com"
password = "SMTP授权码"
to = "你的QQ邮箱@qq.com"

[llm]
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
api_key = "sk-替换成你的DeepSeekKey"

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

- [ ] **Step 2: 写 README**

Create `README.md`:
```markdown
# AI 前沿日报

每天 08:00 抓取 AI 大模型前沿内容（arXiv、厂商博客、HN/Reddit、中文媒体），
用 DeepSeek 生成中文摘要，通过 QQ 邮箱发送 HTML 日报。

## 安装

    python -m pip install --user -r requirements.txt

## 配置

    copy config.example.toml config.toml

然后填写两项凭据：

1. QQ 邮箱：设置 → 账户 → 开启 SMTP 服务 → 生成授权码（填 `password`，不是登录密码）
2. DeepSeek：platform.deepseek.com 创建 API key（填 `api_key`）

`config.toml` 已加入 `.gitignore`，不会入库。

## 试跑

    python digest.py --dry-run    # 只生成 preview.html，不发信
    python digest.py              # 真实抓取并发送

退出码：0 成功 / 1 无内容 / 2 发信失败 / 3 配置错误。

## 定时任务

先确认 Python 路径：

    where python

创建每天 08:00 的任务（把 `<python.exe>` 换成实际路径）：

    schtasks /create /tn "AI Frontier Digest" /tr "\"<python.exe>\" \"C:\Users\74159\ai-frontier-digest\digest.py\"" /sc daily /st 08:00

检查与删除：

    schtasks /query /tn "AI Frontier Digest"
    schtasks /delete /tn "AI Frontier Digest" /f

## 排障

- 日志在 `logs\YYYY-MM-DD.log`，每次运行记录各源抓取数与发送结果
- 某个源连续失败：在 `config.toml` 的 `[sources]` 里把它设为 `false`
- Reddit 返回 403：属预期，脚本会跳过该源继续
- 想清空去重记录：删除 `seen.json`
```

- [ ] **Step 3: 提交**

```bash
git add config.example.toml README.md
git commit -m "docs: add config template and readme"
```

---

### Task 12: 端到端验证与调度安装

**Files:**
- Create: `config.toml`（用户凭据，不入库）

- [ ] **Step 1: 让用户填写凭据**

把 `config.example.toml` 复制为 `config.toml`，填入 QQ 邮箱授权码与 DeepSeek key。

- [ ] **Step 2: 真实抓取但不发信**

Run: `python digest.py --dry-run`
Expected: 输出 `已生成预览：...preview.html`，退出码 0。打开 preview.html 检查：四个小节有内容、链接可点、无乱码。

- [ ] **Step 3: 检查日志**

Run: `type logs\%DATE:~0,4%-%DATE:~5,2%-%DATE:~8,2%.log` 或在文件管理器打开 `logs\`。
Expected: 每个启用源一行 `[源名] 获取 N 条`，失败源有 `失败：` 行。

- [ ] **Step 4: 真实发送一次**

Run: `python digest.py`
Expected: 退出码 0，收件箱收到主题为 `AI 前沿日报 · YYYY-MM-DD · N 条` 的邮件。

- [ ] **Step 5: 再跑一次验证去重**

Run: `python digest.py --dry-run`
Expected: 日志显示 `去重后` 条数明显减少（因为 seen.json 已记录上一批）。

- [ ] **Step 6: 安装计划任务**

Run:
```bash
where python
schtasks /create /tn "AI Frontier Digest" /tr "\"<上一步输出的python.exe路径>\" \"C:\Users\74159\ai-frontier-digest\digest.py\"" /sc daily /st 08:00
schtasks /query /tn "AI Frontier Digest"
```
Expected: 查询结果显示任务存在、下次运行时间为次日 08:00。

- [ ] **Step 7: 提交（如实现期改过代码）**

```bash
git add -A
git commit -m "chore: finalize config template and scheduling"
```

---

## Self-Review

**Spec coverage**

| Spec 要求 | 对应任务 |
|---|---|
| 四类源抓取、8 秒超时、单源失败继续 | Task 6 |
| seen.json 去重、标题相似去重 | Task 3、5 |
| 配额与顺延 | Task 4 |
| DeepSeek 摘要、JSON 契约、降级 | Task 7 |
| HTML 邮件、纯文本备份、重试 | Task 8、9 |
| 日志与退出码 0/1/2/3 | Task 10 |
| 配置校验（含 quota 求和） | Task 1 |
| `--dry-run` | Task 10、12 |
| 任务计划 08:00 | Task 12 |
| 测试 5 项 | Task 2-9 |
| 验收标准 6 条 | Task 12 |

**Placeholder scan:** 无 TBD/TODO；`<python.exe>` 是执行时由 `where python` 决定的真实值，已在任务步骤中给出获取命令。

**Type consistency:** `Item` 字段在 Task 2 定义，Task 3、4、6、7、8 使用的字段名一致；`summarize_items` 返回 `(dict, bool)` 在 Task 7 定义、Task 10 按该顺序解包；`send_mail(cfg, subject, html_body, text_body)` 在 Task 9 定义、Task 10 调用一致。
