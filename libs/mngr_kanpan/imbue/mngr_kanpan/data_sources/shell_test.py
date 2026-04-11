from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import CommitsAheadField
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_source import StringField
from imbue.mngr_kanpan.data_sources.shell import ShellCommandConfig
from imbue.mngr_kanpan.data_sources.shell import ShellCommandDataSource
from imbue.mngr_kanpan.data_sources.shell import _build_shell_env
from imbue.mngr_kanpan.testing import make_agent_details


def test_shell_data_source_name() -> None:
    ds = ShellCommandDataSource(
        field_key="slack",
        config=ShellCommandConfig(name="Slack", header="SLACK", command="echo test"),
    )
    assert ds.name == "shell_slack"


def test_shell_data_source_columns() -> None:
    ds = ShellCommandDataSource(
        field_key="slack",
        config=ShellCommandConfig(name="Slack", header="SLACK", command="echo test"),
    )
    assert ds.columns == {"slack": "SLACK"}


def test_shell_data_source_field_types() -> None:
    ds = ShellCommandDataSource(
        field_key="slack",
        config=ShellCommandConfig(name="Slack", header="SLACK", command="echo test"),
    )
    assert ds.field_types == {"slack": StringField}


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
    )
    cached: dict[str, FieldValue] = {"pr": pr}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_PR_NUMBER"] == "42"
    assert env["MNGR_FIELD_PR_URL"] == "https://github.com/org/repo/pull/42"
    assert env["MNGR_FIELD_PR_STATE"] == "OPEN"


def test_build_shell_env_with_ci_field() -> None:
    agent = make_agent_details(name="agent-1")
    ci = CiField(status=CiStatus.FAILING)
    cached: dict[str, FieldValue] = {"ci": ci}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CI_STATUS"] == "FAILING"


def test_build_shell_env_with_string_field() -> None:
    agent = make_agent_details(name="agent-1")
    cached: dict[str, FieldValue] = {"custom_val": StringField(value="hello")}
    env = _build_shell_env(agent, cached)
    assert env["MNGR_FIELD_CUSTOM_VAL"] == "hello"


def test_build_shell_env_no_branch() -> None:
    agent = make_agent_details(name="agent-1", initial_branch=None)
    env = _build_shell_env(agent, {})
    assert env["MNGR_AGENT_BRANCH"] == ""


def test_build_shell_env_with_other_field() -> None:
    """Non-PrField, non-CiField, non-StringField falls back to display().text."""
    agent = make_agent_details(name="agent-1")
    field = CommitsAheadField(count=3, has_work_dir=True)
    env = _build_shell_env(agent, {"commits_ahead": field})
    assert env["MNGR_FIELD_COMMITS_AHEAD"] == "[3 unpushed]"


# === compute ===


def _make_real_mngr_ctx(cg: ConcurrencyGroup) -> MngrContext:
    """Build a minimal mngr_ctx with a real active ConcurrencyGroup."""
    return cast(MngrContext, SimpleNamespace(concurrency_group=cg))


def test_compute_success() -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo 'output text'"),
    )
    agent = make_agent_details(name="agent-1")
    with ConcurrencyGroup(name="test") as cg:
        ctx = _make_real_mngr_ctx(cg)
        fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name in fields
    field = fields[agent.name]["custom"]
    assert isinstance(field, StringField)
    assert field.value == "output text"


def test_compute_empty_stdout_not_included() -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="echo"),
    )
    agent = make_agent_details(name="agent-1")
    with ConcurrencyGroup(name="test") as cg:
        ctx = _make_real_mngr_ctx(cg)
        fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert errors == []
    assert agent.name not in fields


def test_compute_nonzero_exit_produces_error() -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="exit 1"),
    )
    agent = make_agent_details(name="agent-1")
    with ConcurrencyGroup(name="test") as cg:
        ctx = _make_real_mngr_ctx(cg)
        fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name not in fields
    assert any("Custom" in e and "agent-1" in e for e in errors)


def test_compute_process_returncode_none_skipped() -> None:
    """Process with returncode None (still running) should be skipped.

    This tests an edge case in the result-processing loop. A real process always
    finishes with a non-None returncode, so this test uses a mock proc to reach
    the code path that handles a still-running process.
    """
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="sleep 1"),
    )
    agent = make_agent_details(name="agent-1")

    proc = MagicMock()
    proc.returncode = None

    child_cg = MagicMock()
    child_cg.__enter__ = MagicMock(return_value=child_cg)
    child_cg.__exit__ = MagicMock(return_value=False)
    child_cg.run_process_in_background.return_value = proc

    cg = MagicMock()
    cg.make_concurrency_group.return_value = child_cg

    ctx = cast(MngrContext, SimpleNamespace(concurrency_group=cg))
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert agent.name not in fields
    assert errors == []


def test_compute_concurrency_exception_group_produces_error() -> None:
    ds = ShellCommandDataSource(
        field_key="custom",
        config=ShellCommandConfig(name="Custom", header="CUSTOM", command="exit 1"),
    )
    agent = make_agent_details(name="agent-1")

    proc = MagicMock()
    proc.returncode = 0
    proc.read_stdout.return_value = ""

    child_cg = MagicMock()
    child_cg.__enter__ = MagicMock(return_value=child_cg)
    child_cg.__exit__ = MagicMock(side_effect=ConcurrencyExceptionGroup("timeout", [RuntimeError("timed out")]))
    child_cg.run_process_in_background.return_value = proc

    cg = MagicMock()
    cg.make_concurrency_group.return_value = child_cg

    ctx = cast(MngrContext, SimpleNamespace(concurrency_group=cg))
    fields, errors = ds.compute(agents=(agent,), cached_fields={}, mngr_ctx=ctx)
    assert any("Custom" in e for e in errors)
