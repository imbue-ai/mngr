"""Tests for the config module."""

import pytest

from imbue.minds_workspace_server.config import Config
from imbue.minds_workspace_server.config import DuplicateStaticBasenameError
from imbue.minds_workspace_server.config import load_config


def test_default_config() -> None:
    config = Config()
    assert config.minds_workspace_server_host == "127.0.0.1"
    assert config.minds_workspace_server_port == 8000
    assert config.minds_workspace_server_javascript_plugins is None
    assert config.minds_workspace_server_static_paths is None


def test_load_config_returns_config() -> None:
    config = load_config()
    assert isinstance(config, Config)


def test_javascript_plugin_basenames_empty() -> None:
    config = Config()
    assert config.javascript_plugin_basenames == []


def test_javascript_plugin_basenames_extracts_names() -> None:
    config = Config(minds_workspace_server_javascript_plugins=["/path/to/plugin.js", "/other/script.js"])
    assert config.javascript_plugin_basenames == ["plugin.js", "script.js"]


def test_static_file_basename_to_path_empty() -> None:
    config = Config()
    assert config.static_file_basename_to_path == {}


def test_static_file_basename_to_path_maps() -> None:
    config = Config(minds_workspace_server_static_paths=["/path/to/file.css"])
    assert config.static_file_basename_to_path == {"file.css": "/path/to/file.css"}


def test_split_comma_separated_string() -> None:
    config = Config(minds_workspace_server_javascript_plugins="a.js, b.js")  # type: ignore[arg-type]
    assert config.minds_workspace_server_javascript_plugins == ["a.js", "b.js"]


def test_static_file_basename_to_path_raises_on_duplicate_basename() -> None:
    config = Config(minds_workspace_server_static_paths=["/path/a/file.css", "/path/b/file.css"])
    with pytest.raises(DuplicateStaticBasenameError, match="Duplicate basename 'file.css'"):
        _ = config.static_file_basename_to_path
