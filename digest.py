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
