from datetime import datetime
from datetime import timezone

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.shell import ShellCommandConfig
from imbue.mngr_kanpan.data_sources.shell import ShellCommandDataSource
from imbue.mngr_kanpan.data_sources.shell import _build_shell_env
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_cg

_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def test_build_shell_env_basic() -> None:
    agent = make_agent_details(
        name="agent-1",
        initial_branch="mngr/test",
    )
    env = _build_shell_env(agent, {})
    assert env["MNGR_AGENT_NAME"] == "agent-1"
    assert env["MNGR_AGENT_BRANCH"] == "mngr/test"
    assert env["MNGR_AGENT_STATE"] == "RUNNING"


def test_build_shell_env_with_pr_field() -> None:
    agent = make_agent_details(name="agent-1")
    pr = PrField(
        number=42,
        url="https://github.com/org/repo/pull/42",
        is_draft=False,
        title="Test",
        state=PrState.OPEN,
        head_branch="b",
        created=_NOW,
    )
    cached: dict[str, FieldValue] = {"pr": pr}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_PR_NUMBER"] == "42"
    assert env["MNGR_FIELD_PR_URL"] == "https://github.com/org/repo/pull/42"
    assert env["MNGR_FIELD_PR_STATE"] == "OPEN"


def test_build_shell_env_with_ci_field() -> None:
    agent = make_agent_details(name="agent-1")
    ci = CiField(status=CiStatus.FAILING, created=_NOW)
    cached: dict[str, FieldValue] = {"ci": ci}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CI_STATUS"] == "FAILING"


def test_build_shell_env_with_string_field() -> None:
    agent = make_agent_details(name="agent-1")
    cached: dict[str, FieldValue] = {"custom_val": StringField(value="hello", created=_NOW)}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CUSTOM_VAL"] == "hello"


def test_build_shell_env_no_branch() -> None:
    agent = make_agent_details(name="agent-1", initial_branch=None)
    env = _build_shell_env(agent, {})
    assert env["MNGR_AGENT_BRANCH"] == ""


def test_build_shell_env_with_other_field() -> None:
    """Non-PrField, non-CiField, non-StringField falls back to display().text."""
    agent = make_agent_details(name="agent-1")
    field = CommitsAheadField(count=3, has_work_dir=True, created=_NOW)
    env = _build_shell_env(agent, {"commits_ahead": field})
    assert env["MNGR_FIELD_COMMITS_AHEAD"] == "[3 unpushed]"


# === compute ===


def test_compute_success(test_cg: ConcurrencyGroup) -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'output text'"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    field = fields[agent.name]["custom"]
    assert isinstance(field, StringField)
    assert field.value == "output text"


def test_compute_empty_stdout_not_included(test_cg: ConcurrencyGroup) -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name not in fields


def test_compute_nonzero_exit_produces_error(test_cg: ConcurrencyGroup) -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="exit 1"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name not in fields
    assert any("Custom" in e and "agent-1" in e for e in errors)


def test_compute_timeout_produces_error(test_cg: ConcurrencyGroup) -> None:
    """A command that exceeds the timeout produces an error."""
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="sleep 60"),
        timeout_seconds=0.1,
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert any("Custom" in e for e in errors)
