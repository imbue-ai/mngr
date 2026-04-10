from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
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
