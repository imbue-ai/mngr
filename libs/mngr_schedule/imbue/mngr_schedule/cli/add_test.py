"""Unit tests for schedule add auto-fix and safety check logic."""

import shlex

from inline_snapshot import snapshot

from imbue.mngr_schedule.cli.add import auto_fix_create_args
from imbue.mngr_schedule.cli.add import check_safe_create_command

# =============================================================================
# auto_fix_create_args tests
# =============================================================================


def test_auto_fix_create_args_appends_all_flags_for_canonical_case() -> None:
    assert auto_fix_create_args("my-agent", "trigger-1") == snapshot(
        "my-agent --headless --no-connect --host-label SCHEDULE=trigger-1"
    )


def test_auto_fix_create_args_appends_headless_when_absent() -> None:
    result = auto_fix_create_args("my-agent", "trigger-1")
    parts = shlex.split(result)
    assert "--headless" in parts


def test_auto_fix_create_args_skips_headless_when_already_present() -> None:
    result = auto_fix_create_args("my-agent --headless", "trigger-1")
    parts = shlex.split(result)
    assert parts.count("--headless") == 1


def test_auto_fix_create_args_appends_no_connect_when_absent() -> None:
    result = auto_fix_create_args("my-agent", "trigger-1")
    parts = shlex.split(result)
    assert "--no-connect" in parts


def test_auto_fix_create_args_skips_no_connect_when_connect_present() -> None:
    result = auto_fix_create_args("my-agent --connect", "trigger-1")
    parts = shlex.split(result)
    assert "--no-connect" not in parts
    assert "--connect" in parts


def test_auto_fix_create_args_skips_no_connect_when_no_connect_already_present() -> None:
    result = auto_fix_create_args("my-agent --no-connect", "trigger-1")
    parts = shlex.split(result)
    assert parts.count("--no-connect") == 1


def test_auto_fix_create_args_appends_schedule_host_label() -> None:
    result = auto_fix_create_args("my-agent", "nightly-build")
    parts = shlex.split(result)
    assert "--host-label" in parts
    tag_idx = parts.index("--host-label")
    assert parts[tag_idx + 1] == "SCHEDULE=nightly-build"


def test_auto_fix_create_args_skips_schedule_host_label_when_already_present() -> None:
    result = auto_fix_create_args("my-agent --host-label SCHEDULE=custom", "nightly-build")
    parts = shlex.split(result)
    assert parts.count("--host-label") == 1
    tag_idx = parts.index("--host-label")
    assert parts[tag_idx + 1] == "SCHEDULE=custom"


def test_auto_fix_create_args_skips_schedule_host_label_when_present_in_equals_form() -> None:
    result = auto_fix_create_args("my-agent --host-label=SCHEDULE=custom", "nightly-build")
    parts = shlex.split(result)
    # Should not add a duplicate --host-label SCHEDULE=...
    assert sum(1 for p in parts if "SCHEDULE=" in p) == 1


def test_auto_fix_create_args_preserves_passthrough_args_after_separator() -> None:
    result = auto_fix_create_args("my-agent --reuse -- --model opus", "trigger-1")
    parts = shlex.split(result)
    assert "--" in parts
    separator_idx = parts.index("--")
    # Passthrough args should be after --
    assert "--model" in parts[separator_idx + 1 :]
    assert "opus" in parts[separator_idx + 1 :]
    # Auto-fixed args should be before --
    assert "--no-connect" in parts[:separator_idx]


def test_auto_fix_create_args_appends_all_flags_for_empty_args() -> None:
    result = auto_fix_create_args("", "trigger-1")
    parts = shlex.split(result)
    assert "--headless" in parts
    assert "--no-connect" in parts
    assert "--host-label" in parts


def test_auto_fix_create_args_preserves_existing_args() -> None:
    result = auto_fix_create_args(
        "my-agent --type claude --message 'fix bugs' --provider modal",
        "trigger-1",
    )
    parts = shlex.split(result)
    assert "my-agent" in parts
    assert "--type" in parts
    assert "--message" in parts
    assert "fix bugs" in parts
    assert "--provider" in parts
    assert "modal" in parts


# =============================================================================
# check_safe_create_command tests
# =============================================================================


def test_check_safe_create_command_accepts_reuse() -> None:
    result = check_safe_create_command("my-agent --reuse --provider modal")
    assert result is None


def test_check_safe_create_command_accepts_branch_with_date_placeholder() -> None:
    result = check_safe_create_command("my-agent --branch ':agent-run-{DATE}' --provider modal")
    assert result is None


def test_check_safe_create_command_accepts_branch_equals_with_date_placeholder() -> None:
    result = check_safe_create_command("my-agent --branch=:agent-run-{DATE} --provider modal")
    assert result is None


def test_check_safe_create_command_rejects_branch_equals_without_date() -> None:
    result = check_safe_create_command("my-agent --branch=:static-branch --provider modal")
    assert result is not None


def test_check_safe_create_command_rejects_missing_reuse_and_branch_date() -> None:
    result = check_safe_create_command("my-agent --provider modal")
    assert result is not None
    assert "--branch" in result
    assert "--reuse" in result


def test_check_safe_create_command_rejects_branch_without_date() -> None:
    result = check_safe_create_command("my-agent --branch ':static-branch' --provider modal")
    assert result is not None


def test_check_safe_create_command_accepts_empty_args_with_reuse() -> None:
    result = check_safe_create_command("--reuse")
    assert result is None


def test_check_safe_create_command_rejects_empty_args() -> None:
    result = check_safe_create_command("")
    assert result is not None


def test_check_safe_create_command_ignores_reuse_after_separator() -> None:
    result = check_safe_create_command("my-agent -- --reuse")
    assert result is not None


def test_check_safe_create_command_accepts_branch_date_before_separator() -> None:
    result = check_safe_create_command("my-agent --branch ':run-{DATE}' -- --model opus")
    assert result is None


def test_check_safe_create_command_accepts_branch_with_base_and_date() -> None:
    result = check_safe_create_command("my-agent --branch 'main:run-{DATE}' --provider modal")
    assert result is None


def test_check_safe_create_command_accepts_foreground() -> None:
    # Headless agents (--foreground) auto-destroy per run and reject both
    # --branch and --reuse on the create headless path, so the safety check
    # is inapplicable.
    result = check_safe_create_command("my-agent --type headless_command --foreground")
    assert result is None


def test_check_safe_create_command_ignores_foreground_after_separator() -> None:
    result = check_safe_create_command("my-agent -- --foreground")
    assert result is not None
