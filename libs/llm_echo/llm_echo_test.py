"""Unit tests for the llm-echo plugin."""

import json
from pathlib import Path

import pytest
from llm_echo import EchoModel
from llm_echo import _resolve_response


class TestResolveResponse:
    """Tests for the _resolve_response function."""

    def test_default_echo(self) -> None:
        assert _resolve_response("Hello world") == "Echo: Hello world"

    def test_empty_message(self) -> None:
        assert _resolve_response("") == "Echo: (empty message)"

    def test_static_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ECHO_RESPONSE", "Static reply")
        assert _resolve_response("anything") == "Static reply"

    def test_static_env_override_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ECHO_RESPONSE", "Always this")
        assert _resolve_response("") == "Always this"

    def test_static_env_takes_precedence_over_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        responses_file = tmp_path / "responses.json"
        responses_file.write_text(json.dumps({"hello": "from file"}))
        monkeypatch.setenv("LLM_ECHO_RESPONSE", "from env")
        monkeypatch.setenv("LLM_ECHO_RESPONSES_FILE", str(responses_file))
        assert _resolve_response("hello") == "from env"

    def test_responses_file_substring_match(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        responses_file = tmp_path / "responses.json"
        responses_file.write_text(json.dumps({"hello": "Hi!", "help": "I can help."}))
        monkeypatch.setenv("LLM_ECHO_RESPONSES_FILE", str(responses_file))
        assert _resolve_response("hello world") == "Hi!"

    def test_responses_file_no_match_falls_back(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        responses_file = tmp_path / "responses.json"
        responses_file.write_text(json.dumps({"hello": "Hi!"}))
        monkeypatch.setenv("LLM_ECHO_RESPONSES_FILE", str(responses_file))
        assert _resolve_response("goodbye") == "Echo: goodbye"

    def test_responses_file_missing_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_ECHO_RESPONSES_FILE", "/nonexistent/path.json")
        assert _resolve_response("hello") == "Echo: hello"

    def test_responses_file_invalid_json_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{")
        monkeypatch.setenv("LLM_ECHO_RESPONSES_FILE", str(bad_file))
        with pytest.raises(json.JSONDecodeError):
            _resolve_response("hello")


class TestEchoModel:
    """Tests for the EchoModel class."""

    def test_model_id(self) -> None:
        model = EchoModel()
        assert model.model_id == "echo"

    def test_can_stream(self) -> None:
        model = EchoModel()
        assert model.can_stream is True
