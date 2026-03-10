"""Unit tests for schedule add auto-fix and safety check logic."""

import shlex

from imbue.mng_schedule.cli.add import auto_fix_create_args
from imbue.mng_schedule.cli.add import check_safe_create_command

# =============================================================================
# auto_fix_create_args tests
# =============================================================================


class TestAutoFixCreateArgs:
    """Tests for auto_fix_create_args."""

    def test_adds_headless_when_missing(self) -> None:
        result = auto_fix_create_args("my-agent", "trigger-1")
        parts = shlex.split(result)
        assert "--headless" in parts

    def test_skips_headless_when_already_present(self) -> None:
        result = auto_fix_create_args("my-agent --headless", "trigger-1")
        parts = shlex.split(result)
        assert parts.count("--headless") == 1

    def test_adds_no_connect_when_missing(self) -> None:
        result = auto_fix_create_args("my-agent", "trigger-1")
        parts = shlex.split(result)
        assert "--no-connect" in parts

    def test_skips_no_connect_when_connect_present(self) -> None:
        result = auto_fix_create_args("my-agent --connect", "trigger-1")
        parts = shlex.split(result)
        assert "--no-connect" not in parts
        assert "--connect" in parts

    def test_skips_no_connect_when_no_connect_already_present(self) -> None:
        result = auto_fix_create_args("my-agent --no-connect", "trigger-1")
        parts = shlex.split(result)
        assert parts.count("--no-connect") == 1

    def test_adds_schedule_tag(self) -> None:
        result = auto_fix_create_args("my-agent", "nightly-build")
        parts = shlex.split(result)
        assert "--tag" in parts
        tag_idx = parts.index("--tag")
        assert parts[tag_idx + 1] == "SCHEDULE=nightly-build"

    def test_skips_schedule_tag_when_already_present(self) -> None:
        result = auto_fix_create_args("my-agent --tag SCHEDULE=custom", "nightly-build")
        parts = shlex.split(result)
        assert parts.count("--tag") == 1
        tag_idx = parts.index("--tag")
        assert parts[tag_idx + 1] == "SCHEDULE=custom"

    def test_skips_schedule_tag_when_present_in_equals_form(self) -> None:
        result = auto_fix_create_args("my-agent --tag=SCHEDULE=custom", "nightly-build")
        parts = shlex.split(result)
        # Should not add a duplicate --tag SCHEDULE=...
        assert sum(1 for p in parts if "SCHEDULE=" in p) == 1

    def test_preserves_passthrough_args(self) -> None:
        result = auto_fix_create_args("my-agent --reuse -- --model opus", "trigger-1")
        parts = shlex.split(result)
        assert "--" in parts
        separator_idx = parts.index("--")
        # Passthrough args should be after --
        assert "--model" in parts[separator_idx + 1 :]
        assert "opus" in parts[separator_idx + 1 :]
        # Auto-fixed args should be before --
        assert "--no-connect" in parts[:separator_idx]

    def test_handles_empty_args(self) -> None:
        result = auto_fix_create_args("", "trigger-1")
        parts = shlex.split(result)
        assert "--no-connect" in parts
        assert "--tag" in parts

    def test_preserves_existing_args(self) -> None:
        result = auto_fix_create_args(
            "my-agent --type claude --message 'fix bugs' --in modal",
            "trigger-1",
        )
        parts = shlex.split(result)
        assert "my-agent" in parts
        assert "--type" in parts
        assert "--message" in parts
        assert "fix bugs" in parts
        assert "--in" in parts
        assert "modal" in parts


# =============================================================================
# check_safe_create_command tests
# =============================================================================


class TestCheckSafeCreateCommand:
    """Tests for check_safe_create_command."""

    def test_passes_with_reuse(self) -> None:
        result = check_safe_create_command("my-agent --reuse --in modal")
        assert result is None

    def test_passes_with_branch_date_placeholder(self) -> None:
        result = check_safe_create_command("my-agent --branch ':agent-run-{DATE}' --in modal")
        assert result is None

    def test_passes_with_branch_equals_date_placeholder(self) -> None:
        result = check_safe_create_command("my-agent --branch=:agent-run-{DATE} --in modal")
        assert result is None

    def test_fails_with_branch_equals_without_date(self) -> None:
        result = check_safe_create_command("my-agent --branch=:static-branch --in modal")
        assert result is not None

    def test_fails_without_reuse_or_branch_date(self) -> None:
        result = check_safe_create_command("my-agent --in modal")
        assert result is not None
        assert "--branch" in result
        assert "--reuse" in result

    def test_fails_with_branch_without_date(self) -> None:
        result = check_safe_create_command("my-agent --branch ':static-branch' --in modal")
        assert result is not None

    def test_passes_with_empty_args_and_reuse(self) -> None:
        result = check_safe_create_command("--reuse")
        assert result is None

    def test_fails_with_empty_args(self) -> None:
        result = check_safe_create_command("")
        assert result is not None

    def test_only_checks_args_before_separator(self) -> None:
        """--reuse after -- separator should not count."""
        result = check_safe_create_command("my-agent -- --reuse")
        assert result is not None

    def test_branch_date_before_separator_passes(self) -> None:
        result = check_safe_create_command("my-agent --branch ':run-{DATE}' -- --model opus")
        assert result is None

    def test_passes_with_branch_base_and_date(self) -> None:
        result = check_safe_create_command("my-agent --branch 'main:run-{DATE}' --in modal")
        assert result is None
