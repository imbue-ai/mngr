from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import AgentName
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
from imbue.mngr_kanpan.testing import TEST_NOW
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_cg


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
        created=TEST_NOW,
    )
    cached: dict[str, FieldValue] = {"pr": pr}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_PR_NUMBER"] == "42"
    assert env["MNGR_FIELD_PR_URL"] == "https://github.com/org/repo/pull/42"
    assert env["MNGR_FIELD_PR_STATE"] == "OPEN"


def test_build_shell_env_with_ci_field() -> None:
    agent = make_agent_details(name="agent-1")
    ci = CiField(status=CiStatus.FAILING, created=TEST_NOW)
    cached: dict[str, FieldValue] = {"ci": ci}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CI_STATUS"] == "FAILING"


def test_build_shell_env_with_string_field() -> None:
    agent = make_agent_details(name="agent-1")
    cached: dict[str, FieldValue] = {"custom_val": StringField(value="hello", created=TEST_NOW)}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CUSTOM_VAL"] == "hello"


def test_build_shell_env_no_branch() -> None:
    agent = make_agent_details(name="agent-1", initial_branch=None)
    env = _build_shell_env(agent, {})
    assert env["MNGR_AGENT_BRANCH"] == ""


def test_build_shell_env_with_other_field() -> None:
    """Non-PrField, non-CiField, non-StringField falls back to display().text."""
    agent = make_agent_details(name="agent-1")
    field = CommitsAheadField(count=3, has_work_dir=True, created=TEST_NOW)
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


def test_compute_propagates_oldest_cached_created(test_cg: ConcurrencyGroup) -> None:
    """Shell output's `created` is the min over the cached fields offered as env vars."""
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'hi'"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    older = TEST_NOW - timedelta(hours=2)
    newer = TEST_NOW - timedelta(minutes=5)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            "older_input": StringField(value="x", created=older),
            "newer_input": StringField(value="y", created=newer),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    assert field.created == older


def test_compute_uses_now_when_no_cached_inputs(test_cg: ConcurrencyGroup) -> None:
    """With no cached inputs to taint from, `created` is the wall-clock now."""
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'hi'"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    fields, _errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    delta = datetime.now(timezone.utc) - field.created
    assert delta.total_seconds() < 60


def test_compute_excludes_self_from_staleness_inputs(test_cg: ConcurrencyGroup) -> None:
    """The shell field's own previous value is not an input to its new `created`.

    Otherwise the field's `created` would feed back into itself each cycle and
    stay pinned to its first-ever value forever, eventually appearing stale
    even though it gets recomputed every refresh.
    """
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'hi'"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    very_old = TEST_NOW - timedelta(days=7)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            # Only a previous version of the shell field itself in cache.
            "custom": StringField(value="prev", created=very_old),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    # With self excluded and no other inputs, `created` falls back to now.
    assert field.created != very_old
    delta = datetime.now(timezone.utc) - field.created
    assert delta.total_seconds() < 60
