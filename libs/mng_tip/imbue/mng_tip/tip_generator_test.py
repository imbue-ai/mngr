import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from imbue.mng_tip.invocation_logger import get_tip_data_dir
from imbue.mng_tip.invocation_logger import log_invocation
from imbue.mng_tip.tip_generator import generate_tip
from imbue.mng_tip.tip_generator import main


class TestGenerateTip:
    def test_returns_none_with_no_invocations(self, temp_host_dir: Path) -> None:
        assert generate_tip() is None

    @patch("imbue.mng_tip.tip_generator.query_claude")
    def test_returns_tip_on_success(self, mock_query: Any, temp_host_dir: Path) -> None:
        log_invocation("list", {})
        log_invocation("create", {})

        mock_query.return_value = "Try `mng pair` for continuous file sync"

        result = generate_tip()
        assert result == "Try `mng pair` for continuous file sync"

    @patch("imbue.mng_tip.tip_generator.query_claude")
    def test_returns_none_on_claude_failure(self, mock_query: Any, temp_host_dir: Path) -> None:
        log_invocation("list", {})

        mock_query.return_value = None

        assert generate_tip() is None

    @patch("imbue.mng_tip.tip_generator.query_claude")
    def test_passes_prompt_and_system_prompt(self, mock_query: Any, temp_host_dir: Path) -> None:
        log_invocation("list", {})

        mock_query.return_value = "a tip"

        generate_tip()

        mock_query.assert_called_once()
        call_kwargs = mock_query.call_args
        assert "prompt" in call_kwargs.kwargs
        assert "list" in call_kwargs.kwargs["prompt"]

    @patch("imbue.mng_tip.tip_generator.query_claude")
    def test_includes_previous_suggestions_in_prompt(self, mock_query: Any, temp_host_dir: Path) -> None:
        log_invocation("list", {})

        # Write a previous suggestion
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": "2026-01-01T00:00:00Z", "suggestion": "Use mng pair"}
        with open(tip_dir / "suggestions.jsonl", "w") as f:
            f.write(json.dumps(entry) + "\n")

        mock_query.return_value = "Try mng snapshot"

        generate_tip()

        # Verify the prompt includes the previous suggestion
        call_kwargs = mock_query.call_args
        assert "Use mng pair" in call_kwargs.kwargs["prompt"]


class TestMain:
    @patch("imbue.mng_tip.tip_generator.generate_tip")
    def test_skips_when_next_tip_exists(self, mock_gen: Any, temp_host_dir: Path) -> None:
        tip_dir = get_tip_data_dir()
        tip_dir.mkdir(parents=True, exist_ok=True)
        (tip_dir / "next_tip.txt").write_text("existing tip")

        main()
        mock_gen.assert_not_called()

    @patch("imbue.mng_tip.tip_generator.generate_tip")
    def test_calls_generate_when_no_next_tip(self, mock_gen: Any, temp_host_dir: Path) -> None:
        mock_gen.return_value = "A fresh tip"
        main()
        mock_gen.assert_called_once()

    @patch("imbue.mng_tip.tip_generator.generate_tip")
    def test_saves_suggestion_on_success(self, mock_gen: Any, temp_host_dir: Path) -> None:
        mock_gen.return_value = "Use mng clone to duplicate agents"
        main()

        tip_dir = get_tip_data_dir()
        assert (tip_dir / "next_tip.txt").read_text() == "Use mng clone to duplicate agents"
        assert (tip_dir / "suggestions.jsonl").exists()

        suggestions = (tip_dir / "suggestions.jsonl").read_text().strip().splitlines()
        assert len(suggestions) == 1
        record = json.loads(suggestions[0])
        assert record["suggestion"] == "Use mng clone to duplicate agents"

    @patch("imbue.mng_tip.tip_generator.generate_tip")
    def test_no_write_on_none_result(self, mock_gen: Any, temp_host_dir: Path) -> None:
        mock_gen.return_value = None
        main()

        tip_dir = get_tip_data_dir()
        assert not (tip_dir / "next_tip.txt").exists()
