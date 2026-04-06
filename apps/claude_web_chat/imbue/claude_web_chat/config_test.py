"""Tests for the config module."""

from imbue.claude_web_chat.config import Config
from imbue.claude_web_chat.config import load_config


def test_default_config() -> None:
    config = Config()
    assert config.claude_web_chat_host == "127.0.0.1"
    assert config.claude_web_chat_port == 8000
    assert config.claude_web_chat_javascript_plugins is None
    assert config.claude_web_chat_static_paths is None


def test_load_config_returns_config() -> None:
    config = load_config()
    assert isinstance(config, Config)


def test_javascript_plugin_basenames_empty() -> None:
    config = Config()
    assert config.javascript_plugin_basenames == []


def test_javascript_plugin_basenames_extracts_names() -> None:
    config = Config(claude_web_chat_javascript_plugins=["/path/to/plugin.js", "/other/script.js"])
    assert config.javascript_plugin_basenames == ["plugin.js", "script.js"]


def test_static_file_basename_to_path_empty() -> None:
    config = Config()
    assert config.static_file_basename_to_path == {}


def test_static_file_basename_to_path_maps() -> None:
    config = Config(claude_web_chat_static_paths=["/path/to/file.css"])
    assert config.static_file_basename_to_path == {"file.css": "/path/to/file.css"}


def test_split_comma_separated_string() -> None:
    config = Config(claude_web_chat_javascript_plugins="a.js, b.js")  # type: ignore[arg-type]
    assert config.claude_web_chat_javascript_plugins == ["a.js", "b.js"]
