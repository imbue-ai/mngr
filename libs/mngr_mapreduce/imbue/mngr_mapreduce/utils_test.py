"""Unit tests for framework utility functions."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.utils import dedup_name
from imbue.mngr_mapreduce.utils import get_base_commit
from imbue.mngr_mapreduce.utils import make_run_name
from imbue.mngr_mapreduce.utils import resolve_templates
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name


def test_make_run_name_format() -> None:
    name = make_run_name()
    assert len(name) == 14
    assert name.isdigit()


def test_dedup_name_first_use_returns_base() -> None:
    used: set[str] = set()
    assert dedup_name("foo", used) == "foo"
    assert used == {"foo"}


def test_dedup_name_collision_appends_counter() -> None:
    used: set[str] = {"foo"}
    assert dedup_name("foo", used) == "foo-2"
    assert dedup_name("foo", used) == "foo-3"
    assert used == {"foo", "foo-2", "foo-3"}


def test_dedup_name_skips_existing_counters() -> None:
    used: set[str] = {"foo", "foo-2"}
    assert dedup_name("foo", used) == "foo-3"


def test_sanitize_simple_name() -> None:
    assert sanitize_for_agent_name("test_bar") == "test-bar"


def test_sanitize_truncates_long_names() -> None:
    result = sanitize_for_agent_name("a" * 100)
    assert len(result) <= 40


def test_sanitize_special_characters() -> None:
    result = sanitize_for_agent_name("test_with spaces_and___underscores")
    assert " " not in result
    assert "--" not in result


def test_sanitize_strips_leading_and_trailing_hyphens() -> None:
    assert sanitize_for_agent_name("__foo__") == "foo"


def test_sanitize_strips_trailing_hyphen_after_truncation() -> None:
    # The pre-truncation strip drops the leading/trailing hyphens, but truncation
    # at exactly the 40th character can land on a hyphen and reintroduce one.
    # Confirmed by a TMR failure with AgentName 'tmr-...-test-create-modal-idle-mode-ssh-timeout-'.
    result = sanitize_for_agent_name("test_create_modal_idle_mode_ssh_timeout_300")
    assert result == "test-create-modal-idle-mode-ssh-timeout"
    assert not result.endswith("-")


def test_sanitize_empty_input() -> None:
    """Empty input yields an empty slug (callers must dedup if they need uniqueness)."""
    assert sanitize_for_agent_name("") == ""


def test_resolve_templates_empty_returns_empty() -> None:
    config = MngrConfig()
    assert resolve_templates((), config) == {}


def test_resolve_templates_picks_up_options() -> None:
    config = MngrConfig(
        create_templates={
            CreateTemplateName("foo"): CreateTemplate(options={"build_args": ("--x",), "agent_type": "claude"}),
        },
    )
    merged = resolve_templates(("foo",), config)
    assert merged == {"build_args": ("--x",), "agent_type": "claude"}


def test_resolve_templates_later_overrides_earlier() -> None:
    config = MngrConfig(
        create_templates={
            CreateTemplateName("a"): CreateTemplate(options={"agent_type": "x"}),
            CreateTemplateName("b"): CreateTemplate(options={"agent_type": "y"}),
        },
    )
    merged = resolve_templates(("a", "b"), config)
    assert merged["agent_type"] == "y"


def test_resolve_templates_missing_template_raises() -> None:
    config = MngrConfig()
    with pytest.raises(MngrError):
        resolve_templates(("nope",), config)


def test_get_base_commit_returns_head_sha(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """Exercises get_base_commit on a one-commit scratch repo."""
    cg.run_process_to_completion(["git", "init", "-q"], cwd=tmp_path)
    cg.run_process_to_completion(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    cg.run_process_to_completion(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "f.txt").write_text("hi")
    cg.run_process_to_completion(["git", "add", "f.txt"], cwd=tmp_path)
    cg.run_process_to_completion(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)

    sha = get_base_commit(tmp_path, cg)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
