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


from digest import select_items

QUOTA = {"arxiv": 2, "vendor": 1, "community": 1, "chinese": 1}


def test_select_respects_quota():
    """各组都有足够候选时，按配额分配。"""
    items = []
    for source_name, count in (("arxiv", 4), ("vendor", 3), ("hn", 3), ("chinese", 3)):
        for n in range(count):
            item = make_item(
                f"{source_name} title {n}", 50 + n, f"https://example.com/{source_name}{n}"
            )
            item.source = source_name
            items.append(item)

    selected = select_items(items, QUOTA, max_items=5)

    counts: dict[str, int] = {}
    for item in selected:
        counts[item.source] = counts.get(item.source, 0) + 1
    assert counts == {"arxiv": 2, "vendor": 1, "hn": 1, "chinese": 1}


def test_select_redistributes_unfilled_quota():
    """某分组不足时，空出的名额顺延给有剩余的组。"""
    arxiv = [make_item(f"arxiv {n}", 60 + n, f"https://example.com/a{n}") for n in range(1)]
    community = [
        make_item(f"community {n}", 80 + n, f"https://example.com/c{n}") for n in range(5)
    ]
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


from digest import render_html, render_text


def test_render_html_escapes_content():
    """标题和摘要中的 HTML 被转义。"""
    item = make_item("<script>alert(1)</script>", 80, "https://example.com/x")
    html_out = render_html([item], {}, "2026-10-26", degraded=False)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_html_uses_summary_when_available():
    """有摘要时渲染中文标题与 why。"""
    item = make_item("Original title", 80, "https://example.com/x")
    summaries = {item.id: {"cn_title": "中文标题", "summary": "摘要正文", "why": "值得关注"}}
    html_out = render_html([item], summaries, "2026-10-26", degraded=False)
    assert "中文标题" in html_out
    assert "摘要正文" in html_out
    assert "值得关注" in html_out


def test_render_html_marks_degraded():
    """降级时邮件顶部有提示。"""
    item = make_item("t", 80, "https://example.com/x")
    html_out = render_html([item], {}, "2026-10-26", degraded=True)
    assert "摘要服务不可用" in html_out


def test_render_text_contains_links():
    """纯文本版本包含标题与 URL。"""
    item = make_item("t", 80, "https://example.com/x")
    text = render_text([item], {}, "2026-10-26", degraded=False)
    assert "https://example.com/x" in text


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


def test_main_guard_is_last_statement():
    """入口守卫必须在文件末尾，否则脚本模式会早于定义执行 main。"""
    source = Path(digest.__file__).read_text(encoding="utf-8").rstrip()
    assert source.endswith("sys.exit(main())")


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
