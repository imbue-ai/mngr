"""Tests for the default model webchat plugin."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute

from imbue.mngr_llm.resources.webchat_plugins.webchat_default_model import DefaultModelPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_default_model import _FALLBACK_MODEL
from imbue.mngr_llm.resources.webchat_plugins.webchat_default_model import read_default_chat_model


def testread_default_chat_model_returns_fallback_when_no_work_dir() -> None:
    assert read_default_chat_model("") == _FALLBACK_MODEL


def testread_default_chat_model_returns_fallback_when_no_toml(tmp_path: Path) -> None:
    assert read_default_chat_model(str(tmp_path)) == _FALLBACK_MODEL


def testread_default_chat_model_reads_from_minds_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "minds.toml"
    toml_path.write_text('[chat]\nmodel = "gpt-4o"\n')
    assert read_default_chat_model(str(tmp_path)) == "gpt-4o"


def testread_default_chat_model_returns_fallback_when_no_chat_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "minds.toml"
    toml_path.write_text("[other]\nkey = 1\n")
    assert read_default_chat_model(str(tmp_path)) == _FALLBACK_MODEL


def testread_default_chat_model_returns_fallback_when_model_key_missing(tmp_path: Path) -> None:
    toml_path = tmp_path / "minds.toml"
    toml_path.write_text("[chat]\nother_key = 1\n")
    assert read_default_chat_model(str(tmp_path)) == _FALLBACK_MODEL


def testread_default_chat_model_returns_fallback_on_invalid_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "minds.toml"
    toml_path.write_text("this is not valid toml {{{{")
    assert read_default_chat_model(str(tmp_path)) == _FALLBACK_MODEL


def test_default_model_plugin_registers_endpoint() -> None:
    """Verify that the plugin registers the correct route on the FastAPI app."""
    app = FastAPI()
    plugin = DefaultModelPlugin()
    plugin.endpoint(app=app)

    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    route_paths = [route.path for route in api_routes]
    assert "/api/default-model" in route_paths
