"""AI 前沿日报：抓取、筛选、摘要、投递。"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"
SEEN_PATH = ROOT / "seen.json"
LOG_DIR = ROOT / "logs"
PREVIEW_PATH = ROOT / "preview.html"

USER_AGENT = "Mozilla/5.0 (compatible; ai-frontier-digest/1.0)"
REQUEST_TIMEOUT = 8

log = logging.getLogger("digest")


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


SOURCE_GROUP = {
    "arxiv": "arxiv",
    "vendor": "vendor",
    "hn": "community",
    "reddit": "community",
    "chinese": "chinese",
    "github": "github",
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

# 实测于 2026-10-26：openai.com 与 anthropic.com 在本网络下 SSL 超时，
# 机器之心 RSS 返回空喂，暂不启用。有代理后可把地址加回。
VENDOR_FEEDS = {
    "DeepMind": "https://deepmind.google/blog/rss.xml",
    "Meta Engineering": "https://engineering.fb.com/feed/",
    "Qwen": "https://qwenlm.github.io/blog/index.xml",
    "Mistral": "https://mistral.ai/rss.xml",
}

CHINESE_FEEDS = {
    "量子位": "https://www.qbitai.com/feed",
}

ARXIV_API = "http://export.arxiv.org/api/query"
HN_API = "https://hn.algolia.com/api/v1/search_by_date"
REDDIT_FEEDS = {
    "LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=25",
    "MachineLearning": "https://www.reddit.com/r/MachineLearning/top.json?t=day&limit=25",
}

GITHUB_TRENDING_URL = "https://github.com/trending?since=daily"
GITHUB_TOP_N = 10
GITHUB_AI_PATTERN = re.compile(
    r"(?<![a-z])(ai|llm|gpt|claude|codex|agents?|agentic|chatbot|copilot|mcp|rag|"
    r"transformer|diffusion|inference|embedding|multimodal|reasoning|machine learning|"
    r"deep learning|neural|openai|anthropic|gemini|llama|qwen|deepseek|mistral|model)(?![a-z])",
    re.I,
)


def strip_html(text: str) -> str:
    """去掉 HTML 标签并压缩空白。"""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    unescaped = html.unescape(without_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


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
    "github": fetch_github,
}


# arXiv 周末不更新、厂商博客发布稀疏，用比基准更宽的回看窗口，
# 避免周一或安静日整组为空；seen.json 保证同一内容不会重复推送。
SOURCE_LOOKBACK_HOURS = {
    "arxiv": 96,
    "vendor_blogs": 168,
    "chinese_media": 168,
    "github": 24,
}


def fetch_all(cfg: dict, now: datetime, since: datetime) -> list[Item]:
    """按配置抓取全部启用的源，单源失败只记日志。"""
    items: list[Item] = []
    base_hours = int(cfg["digest"]["lookback_hours"])
    for key, fetcher in FETCHERS.items():
        if not cfg["sources"].get(key, True):
            continue
        hours = max(base_hours, SOURCE_LOOKBACK_HOURS.get(key, base_hours))
        source_since = now - timedelta(hours=hours)
        try:
            found = fetcher(source_since)
            log.info("[%s] 回看 %dh，获取 %d 条", key, hours, len(found))
            items.extend(found)
        except Exception as exc:
            log.warning("[%s] 失败：%s", key, exc)
    return items


SUMMARY_SYSTEM_PROMPT = (
    "你是 AI 行业分析师。用户会给你一批最近的技术内容条目。"
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
    log.addHandler(file_handler)
    # pythonw.exe 下 sys.stderr 为 None，跳过终端输出避免日志报错
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(stream_handler)


def run(cfg: dict, dry_run: bool) -> int:
    """执行一次完整流程，返回退出码。"""
    now = now_utc()
    since = now - timedelta(hours=int(cfg["digest"]["lookback_hours"]))
    seen = load_seen()

    candidates = fetch_all(cfg, now, since)
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


class ConfigError(Exception):
    """配置缺失或取值非法。"""


class MailError(Exception):
    """邮件发送失败。"""


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


if __name__ == "__main__":
    sys.exit(main())
