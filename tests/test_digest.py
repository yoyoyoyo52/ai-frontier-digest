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
