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


# arXiv 周末不更新、厂商博客发布稀疏，用比基准更宽的回看窗口，
# 避免周一或安静日整组为空；seen.json 保证同一内容不会重复推送。
SOURCE_LOOKBACK_HOURS = {
    "arxiv": 96,
    "vendor_blogs": 168,
    "chinese_media": 168,
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
