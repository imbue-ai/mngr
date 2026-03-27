"""Unit tests for test-mapreduce API functions."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr_tmr.api import CollectTestsError
from imbue.mngr_tmr.api import _build_agent_options
from imbue.mngr_tmr.api import _sanitize_test_name_for_agent
from imbue.mngr_tmr.api import _short_random_id
from imbue.mngr_tmr.api import _transfer_mode_for_provider
from imbue.mngr_tmr.api import build_current_results
from imbue.mngr_tmr.api import collect_tests
from imbue.mngr_tmr.api import read_agent_result
from imbue.mngr_tmr.api import read_integrator_result
from imbue.mngr_tmr.api import should_pull_changes
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import ReportSection
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.prompts import PLUGIN_NAME
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.report import report_section_of
from imbue.mngr_tmr.testing import BLOCKED_FIX
from imbue.mngr_tmr.testing import FAILED_FIX
from imbue.mngr_tmr.testing import SUCCEEDED_FIX
from imbue.mngr_tmr.testing import make_test_result


def test_short_random_id_length() -> None:
    rid = _short_random_id()
    assert len(rid) == 6


def test_short_random_id_is_hex() -> None:
    rid = _short_random_id()
    int(rid, 16)


def test_short_random_id_is_unique() -> None:
    ids = {_short_random_id() for _ in range(100)}
    assert len(ids) == 100


def test_sanitize_simple_test_name() -> None:
    assert _sanitize_test_name_for_agent("tests/test_foo.py::test_bar") == "test-bar"


def test_sanitize_nested_test_name() -> None:
    assert _sanitize_test_name_for_agent("tests/test_foo.py::TestClass::test_method") == "test-method"


def test_sanitize_parametrized_test_name() -> None:
    result = _sanitize_test_name_for_agent("tests/test_foo.py::test_bar[param1-param2]")
    assert result == "test-bar-param1-param2-"[:40].rstrip("-")


def test_sanitize_truncates_long_names() -> None:
    long_name = "tests/test_foo.py::test_" + "a" * 100
    result = _sanitize_test_name_for_agent(long_name)
    assert len(result) <= 40


def test_sanitize_special_characters() -> None:
    result = _sanitize_test_name_for_agent("tests/test_foo.py::test_with spaces_and___underscores")
    assert " " not in result
    assert "--" not in result


def test_sanitize_single_part() -> None:
    result = _sanitize_test_name_for_agent("simple_test")
    assert result == "simple-test"


def test_transfer_mode_local_provider_uses_git_worktree() -> None:
    assert _transfer_mode_for_provider(ProviderInstanceName("local")) == TransferMode.GIT_WORKTREE


def test_transfer_mode_remote_provider_uses_git_mirror() -> None:
    assert _transfer_mode_for_provider(ProviderInstanceName("docker")) == TransferMode.GIT_MIRROR
    assert _transfer_mode_for_provider(ProviderInstanceName("modal")) == TransferMode.GIT_MIRROR


def _make_config(provider: str = "local", snapshot: SnapshotName | None = None) -> TmrLaunchConfig:
    """Build a TmrLaunchConfig for unit testing.

    Uses model_construct to skip validation of the source_host field,
    which requires a real OnlineHostInterface that these unit tests don't need.
    """
    return TmrLaunchConfig.model_construct(
        source_dir=Path("/tmp/src"),
        source_host=None,
        agent_type=AgentTypeName("claude"),
        provider_name=ProviderInstanceName(provider),
        env_options=AgentEnvironmentOptions(),
        label_options=AgentLabelOptions(),
        snapshot=snapshot,
    )


def test_build_agent_options_rsync_disabled() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config())
    assert opts.data_options.is_rsync_enabled is False


def test_build_agent_options_local_uses_worktree() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"))
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_WORKTREE


def test_build_agent_options_remote_uses_git_mirror() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("modal"))
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_MIRROR


def test_build_agent_options_local_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"))
    assert opts.ready_timeout_seconds == 10.0


def test_build_agent_options_remote_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("docker"))
    assert opts.ready_timeout_seconds == 60.0


def test_build_agent_options_passes_env_and_labels() -> None:
    env = AgentEnvironmentOptions(env_vars=(EnvVar(key="FOO", value="bar"),))
    labels = AgentLabelOptions(labels={"batch": "1"})
    config = _make_config()
    config_with_env_and_labels = TmrLaunchConfig.model_construct(
        source_dir=config.source_dir,
        source_host=None,
        agent_type=config.agent_type,
        provider_name=config.provider_name,
        env_options=env,
        label_options=labels,
        snapshot=None,
    )
    opts = _build_agent_options(AgentName("test"), "branch", config_with_env_and_labels)
    assert opts.environment.env_vars == (EnvVar(key="FOO", value="bar"),)
    assert opts.label_options.labels == {"batch": "1"}


def test_build_agent_options_sets_agent_name() -> None:
    opts = _build_agent_options(AgentName("tmr-my-test-abc123"), "mngr-tmr/my-test", _make_config())
    assert opts.name == AgentName("tmr-my-test-abc123")


def test_build_agent_prompt_contains_test_id() -> None:
    prompt = build_test_agent_prompt("tests/test_foo.py::test_bar", ())
    assert "tests/test_foo.py::test_bar" in prompt
    assert "result.json" in prompt
    assert "IMPROVE_TEST" in prompt
    assert "FIX_TEST" in prompt
    assert "FIX_IMPL" in prompt
    assert "tests_passing_before" in prompt
    assert "tests_passing_after" in prompt
    assert "summary_markdown" in prompt


def test_build_agent_prompt_contains_plugin_name() -> None:
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ())
    assert PLUGIN_NAME in prompt


def test_build_agent_prompt_includes_pytest_flags() -> None:
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ("-m", "release"))
    assert "-m release" in prompt


def test_build_agent_prompt_requests_markdown() -> None:
    prompt = build_test_agent_prompt("t::t", ())
    assert "markdown" in prompt.lower()


def test_build_agent_prompt_instructs_one_entry_per_kind() -> None:
    prompt = build_test_agent_prompt("t::t", ())
    assert "do not duplicate kinds" in prompt.lower()


def test_build_agent_prompt_with_suffix() -> None:
    prompt = build_test_agent_prompt("t::t", (), prompt_suffix="Always run with --verbose flag.")
    assert "Always run with --verbose flag." in prompt


def test_build_agent_prompt_empty_suffix_ignored() -> None:
    prompt_no_suffix = build_test_agent_prompt("t::t", ())
    prompt_empty_suffix = build_test_agent_prompt("t::t", (), prompt_suffix="")
    assert prompt_no_suffix == prompt_empty_suffix


def test_collect_tests_with_real_pytest(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_one(): pass\ndef test_two(): pass\n")
    test_ids = collect_tests(pytest_args=(str(test_file),), source_dir=tmp_path, cg=cg)
    assert len(test_ids) == 2
    assert any("test_one" in tid for tid in test_ids)
    assert any("test_two" in tid for tid in test_ids)


def test_collect_tests_no_tests_raises(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    empty_file = tmp_path / "empty.py"
    empty_file.write_text("x = 1\n")
    with pytest.raises(CollectTestsError):
        collect_tests(pytest_args=(str(empty_file),), source_dir=tmp_path, cg=cg)


def test_collect_tests_bad_file_raises(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    with pytest.raises(CollectTestsError):
        collect_tests(pytest_args=("non_existent_test_file.py",), source_dir=tmp_path, cg=cg)


# --- should_pull_changes tests ---
# Uses shared helpers from testing: make_test_result, SUCCEEDED_FIX, FAILED_FIX, BLOCKED_FIX


def test_should_pull_succeeded_fix_with_tests_passing() -> None:
    assert should_pull_changes(make_test_result(changes=SUCCEEDED_FIX, before=False, after=True)) is True


def test_should_pull_succeeded_fix_tests_were_failing_still_failing() -> None:
    assert should_pull_changes(make_test_result(changes=SUCCEEDED_FIX, before=False, after=False)) is True


def test_should_not_pull_when_errored() -> None:
    assert (
        should_pull_changes(make_test_result(changes=SUCCEEDED_FIX, errored=True, before=False, after=True)) is False
    )


def test_should_not_pull_when_no_succeeded_changes() -> None:
    assert should_pull_changes(make_test_result(changes=FAILED_FIX, before=False, after=False)) is False
    assert should_pull_changes(make_test_result(changes=BLOCKED_FIX, before=False, after=False)) is False


def test_should_not_pull_when_no_changes() -> None:
    assert should_pull_changes(make_test_result(before=True, after=True)) is False


def test_should_not_pull_when_regression() -> None:
    assert should_pull_changes(make_test_result(changes=SUCCEEDED_FIX, before=True, after=False)) is False


def test_should_pull_improvement_tests_still_passing() -> None:
    improved = {ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="improved")}
    assert should_pull_changes(make_test_result(changes=improved, before=True, after=True)) is True


def test_should_not_pull_improvement_that_breaks_tests() -> None:
    improved = {ChangeKind.IMPROVE_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="improved")}
    assert should_pull_changes(make_test_result(changes=improved, before=True, after=False)) is False


def test_build_current_results_pending_agents() -> None:
    """Agents not in final_details should appear as PENDING."""
    agents = [
        TestAgentInfo(
            test_node_id="tests/test_a.py::test_one",
            agent_id=AgentId.generate(),
            agent_name=AgentName("tmr-test-one-abc123"),
            created_at=0.0,
        ),
        TestAgentInfo(
            test_node_id="tests/test_b.py::test_two",
            agent_id=AgentId.generate(),
            agent_name=AgentName("tmr-test-two-def456"),
            created_at=0.0,
        ),
    ]
    results = build_current_results(agents=agents, final_details={}, timed_out_ids=set(), hosts={})
    assert len(results) == 2
    assert report_section_of(results[0]) == ReportSection.RUNNING
    assert report_section_of(results[1]) == ReportSection.RUNNING
    assert "still running" in results[0].summary_markdown


def test_build_current_results_timed_out_agents() -> None:
    """Timed-out agents should appear as ERRORED."""
    agent_id = AgentId.generate()
    agents = [
        TestAgentInfo(
            test_node_id="tests/test_a.py::test_one",
            agent_id=agent_id,
            agent_name=AgentName("tmr-test-one-abc123"),
            created_at=0.0,
        ),
    ]
    results = build_current_results(agents=agents, final_details={}, timed_out_ids={str(agent_id)}, hosts={})
    assert len(results) == 1
    assert results[0].errored is True
    assert report_section_of(results[0]) == ReportSection.BLOCKED


# --- read_agent_result / read_integrator_result tests ---


def _write_result_json(host_dir: Path, agent_id: AgentId, content: str) -> None:
    """Write a result.json for an agent in the expected directory structure."""
    result_dir = host_dir / "agents" / str(agent_id) / "plugin" / PLUGIN_NAME
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(content)


def _make_agent_detail(agent_id: AgentId, host_dir: Path) -> AgentDetails:
    """Build a minimal AgentDetails for testing result reading.

    Uses model_construct to skip validation of the host field, which requires
    a HostDetails that these tests don't need.
    """
    return AgentDetails.model_construct(
        id=agent_id,
        name=AgentName("tmr-test"),
        type="claude",
        command=CommandString("echo"),
        work_dir=host_dir / "workdir",
        initial_branch="mngr-tmr/test",
        create_time=datetime.now(timezone.utc),
        start_on_boot=False,
        state=AgentLifecycleState.DONE,
        host=None,
    )


def test_read_agent_result_parses_changes(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    _write_result_json(
        localhost.host_dir,
        agent_id,
        '{"changes": {"FIX_TEST": {"status": "SUCCEEDED", "summary_markdown": "Fixed it"}},'
        ' "errored": false, "tests_passing_before": false, "tests_passing_after": true,'
        ' "summary_markdown": "All good"}',
    )
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_agent_result(detail, localhost)
    assert ChangeKind.FIX_TEST in result.changes
    assert result.changes[ChangeKind.FIX_TEST].status == ChangeStatus.SUCCEEDED
    assert result.tests_passing_before is False
    assert result.tests_passing_after is True
    assert result.summary_markdown == "All good"


def test_read_agent_result_empty_changes(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    _write_result_json(
        localhost.host_dir,
        agent_id,
        '{"changes": {}, "errored": false, "tests_passing_before": true,'
        ' "tests_passing_after": true, "summary_markdown": "Clean pass"}',
    )
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_agent_result(detail, localhost)
    assert result.changes == {}
    assert result.errored is False


def test_read_agent_result_invalid_json(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    _write_result_json(localhost.host_dir, agent_id, "not json")
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_agent_result(detail, localhost)
    assert result.errored is True
    assert "Failed to read" in result.summary_markdown


def test_read_agent_result_missing_file(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_agent_result(detail, localhost)
    assert result.errored is True


def test_read_integrator_result_parses_merged_failed(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    _write_result_json(
        localhost.host_dir,
        agent_id,
        '{"squashed_branches": ["branch-a", "branch-b"], "squashed_commit_hash": "abc1234",'
        ' "impl_priority": ["branch-d"], "impl_commit_hashes": {"branch-d": "def5678"}, "failed": ["branch-c"]}',
    )
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_integrator_result(detail, localhost, "mngr-tmr/integrated")
    assert result.squashed_branches == ("branch-a", "branch-b")
    assert result.squashed_commit_hash == "abc1234"
    assert result.impl_priority == ("branch-d",)
    assert result.impl_commit_hashes == {"branch-d": "def5678"}
    assert result.failed == ("branch-c",)
    assert result.branch_name == "mngr-tmr/integrated"


def test_read_integrator_result_missing_file(localhost: OnlineHostInterface) -> None:
    agent_id = AgentId.generate()
    detail = _make_agent_detail(agent_id, localhost.host_dir)
    result = read_integrator_result(detail, localhost, "mngr-tmr/integrated")
    assert result.branch_name == "mngr-tmr/integrated"
    assert result.squashed_branches == ()
    assert result.impl_priority == ()
    assert result.failed == ()
