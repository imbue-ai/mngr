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
        created=datetime(2030, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    cached: dict[str, FieldValue] = {"pr": pr}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_PR_NUMBER"] == "42"
    assert env["MNGR_FIELD_PR_URL"] == "https://github.com/org/repo/pull/42"
    assert env["MNGR_FIELD_PR_STATE"] == "OPEN"


def test_build_shell_env_with_ci_field() -> None:
    agent = make_agent_details(name="agent-1")
    ci = CiField(status=CiStatus.FAILING, created=datetime(2030, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    cached: dict[str, FieldValue] = {"ci": ci}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CI_STATUS"] == "FAILING"


def test_build_shell_env_with_string_field() -> None:
    agent = make_agent_details(name="agent-1")
    cached: dict[str, FieldValue] = {
        "custom_val": StringField(value="hello", created=datetime(2030, 1, 1, 0, 0, 3, tzinfo=timezone.utc))
    }
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CUSTOM_VAL"] == "hello"


def test_build_shell_env_no_branch() -> None:
    agent = make_agent_details(name="agent-1", initial_branch=None)
    env = _build_shell_env(agent, {})
    assert env["MNGR_AGENT_BRANCH"] == ""


def test_build_shell_env_with_other_field() -> None:
    """Non-PrField, non-CiField, non-StringField falls back to display().text."""
    agent = make_agent_details(name="agent-1")
    field = CommitsAheadField(count=3, has_work_dir=True, created=datetime(2030, 1, 1, 0, 0, 4, tzinfo=timezone.utc))
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


def test_compute_propagates_oldest_declared_input(test_cg: ConcurrencyGroup) -> None:
    """When the operator declares `inputs`, `created` is the min over those declared inputs."""
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(
            name="Custom",
            header="CUSTOM",
            command="echo 'hi'",
            inputs=("older_input", "newer_input"),
        ),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    older = datetime(2030, 1, 1, 0, 0, 5, tzinfo=timezone.utc) - timedelta(hours=2)
    newer = datetime(2030, 1, 1, 0, 0, 6, tzinfo=timezone.utc) - timedelta(minutes=5)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            "older_input": StringField(value="x", created=older),
            "newer_input": StringField(value="y", created=newer),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    assert field.created == older


def test_compute_uses_now_when_inputs_unset(test_cg: ConcurrencyGroup) -> None:
    """When `inputs` is empty, no cached field taints staleness; `created` is now."""
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


def test_compute_uses_now_when_no_inputs_declared_even_with_cached_fields(
    test_cg: ConcurrencyGroup,
) -> None:
    """With `inputs=()` (default), undeclared cached fields don't propagate staleness.

    The shell still receives MNGR_FIELD_<KEY> for cached fields, but if the operator
    didn't declare them as inputs, they're treated as unused and don't feed into
    staleness calculation.
    """
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'hi'"),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    very_old = datetime(2030, 1, 1, 0, 0, 7, tzinfo=timezone.utc) - timedelta(days=7)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            "some_other_field": StringField(value="x", created=very_old),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    assert field.created != very_old
    delta = datetime.now(timezone.utc) - field.created
    assert delta.total_seconds() < 60


def test_compute_ignores_undeclared_cached_keys(test_cg: ConcurrencyGroup) -> None:
    """Cached fields that aren't declared in `inputs` don't affect `created` -- even
    when the same agent has both declared and undeclared cached fields, only the
    declared ones taint the result.
    """
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(
            name="Custom",
            header="CUSTOM",
            command="echo 'hi'",
            inputs=("declared_input",),
        ),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    declared_age = datetime(2030, 1, 1, 0, 0, 8, tzinfo=timezone.utc) - timedelta(minutes=5)
    undeclared_age = datetime(2030, 1, 1, 0, 0, 9, tzinfo=timezone.utc) - timedelta(days=7)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            "declared_input": StringField(value="x", created=declared_age),
            "noisy_other": StringField(value="y", created=undeclared_age),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    assert field.created == declared_age


def test_compute_does_not_self_taint_even_when_self_is_declared(
    test_cg: ConcurrencyGroup,
) -> None:
    """Even if an operator pathologically declared the field's own key as an input,
    the resulting `created` is still the cached self-value's age. The self-taint
    risk is mitigated structurally by the operator declaring exactly which other
    fields they read; declaring `self.field_key` is a configuration mistake we
    don't try to catch -- but it is no longer the default behaviour.
    """
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(
            name="Custom",
            header="CUSTOM",
            # Pathological self-declaration: the operator names the field's
            # own key as an input.
            command="echo 'hi'",
            inputs=("custom",),
        ),
    )
    agent = make_agent_details(name="agent-1")
    ctx = make_mngr_ctx_with_cg(test_cg)
    very_old = datetime(2030, 1, 1, 0, 0, 10, tzinfo=timezone.utc) - timedelta(days=7)
    cached: dict[AgentName, dict[str, FieldValue]] = {
        AgentName("agent-1"): {
            "custom": StringField(value="prev", created=very_old),
        },
    }
    fields, _errors = ds.compute(agents=(agent,), cached_fields=cached, mngr_ctx=ctx)
    field = fields[AgentName("agent-1")]["custom"]
    # With self in inputs, the new field inherits the very_old `created`. This
    # documents the contract: the operator chose this and the system honors it.
    assert field.created == very_old
