"""Unit tests for the llm-matched-responses plugin."""

import json
from pathlib import Path

import pytest
from llm_matched_responses import resolve_response


def test_default_echo() -> None:
    assert resolve_response("Hello world") == "Echo: Hello world"


def test_empty_message() -> None:
    assert resolve_response("") == "Echo: (empty message)"


def test_static_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MATCHED_RESPONSE", "Static reply")
    assert resolve_response("anything") == "Static reply"


def test_static_env_override_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MATCHED_RESPONSE", "Always this")
    assert resolve_response("") == "Always this"


def test_static_env_takes_precedence_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    responses_file = tmp_path / "responses.json"
    responses_file.write_text(json.dumps({"hello": "from file"}))
    monkeypatch.setenv("LLM_MATCHED_RESPONSE", "from env")
    monkeypatch.setenv("LLM_MATCHED_RESPONSES_FILE", str(responses_file))
    assert resolve_response("hello") == "from env"


def test_responses_file_substring_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    responses_file = tmp_path / "responses.json"
    responses_file.write_text(json.dumps({"hello": "Hi!", "help": "I can help."}))
    monkeypatch.setenv("LLM_MATCHED_RESPONSES_FILE", str(responses_file))
    assert resolve_response("hello world") == "Hi!"


def test_responses_file_no_match_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    responses_file = tmp_path / "responses.json"
    responses_file.write_text(json.dumps({"hello": "Hi!"}))
    monkeypatch.setenv("LLM_MATCHED_RESPONSES_FILE", str(responses_file))
    assert resolve_response("goodbye") == "Echo: goodbye"


def test_responses_file_missing_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MATCHED_RESPONSES_FILE", "/nonexistent/path.json")
    assert resolve_response("hello") == "Echo: hello"


def test_responses_file_invalid_json_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{")
    monkeypatch.setenv("LLM_MATCHED_RESPONSES_FILE", str(bad_file))
    with pytest.raises(json.JSONDecodeError):
        resolve_response("hello")
