"""Unit tests for test-mapreduce API functions."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.launching import _build_agent_options
from imbue.mngr_tmr.prompts import INTEGRATOR_INPUTS_DIRNAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import build_local_integrator_prompt
from imbue.mngr_tmr.prompts import build_remote_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.utils import CollectTestsError
from imbue.mngr_tmr.utils import collect_tests
from imbue.mngr_tmr.utils import dedup_name
from imbue.mngr_tmr.utils import make_run_name
from imbue.mngr_tmr.utils import sanitize_test_name_for_agent
from imbue.mngr_tmr.utils import transfer_mode_for_provider


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


def test_sanitize_simple_test_name() -> None:
    assert sanitize_test_name_for_agent("tests/test_foo.py::test_bar") == "test-bar"


def test_sanitize_nested_test_name() -> None:
    assert sanitize_test_name_for_agent("tests/test_foo.py::TestClass::test_method") == "test-method"


def test_sanitize_parametrized_test_name() -> None:
    result = sanitize_test_name_for_agent("tests/test_foo.py::test_bar[param1-param2]")
    assert result == "test-bar-param1-param2-"[:40].rstrip("-")


def test_sanitize_truncates_long_names() -> None:
    long_name = "tests/test_foo.py::test_" + "a" * 100
    result = sanitize_test_name_for_agent(long_name)
    assert len(result) <= 40


def test_sanitize_special_characters() -> None:
    result = sanitize_test_name_for_agent("tests/test_foo.py::test_with spaces_and___underscores")
    assert " " not in result
    assert "--" not in result


def test_sanitize_single_part() -> None:
    result = sanitize_test_name_for_agent("simple_test")
    assert result == "simple-test"


def test_transfer_mode_local_provider_uses_git_worktree() -> None:
    assert transfer_mode_for_provider(ProviderInstanceName("local")) == TransferMode.GIT_WORKTREE


def test_transfer_mode_remote_provider_uses_git_mirror() -> None:
    assert transfer_mode_for_provider(ProviderInstanceName("docker")) == TransferMode.GIT_MIRROR
    assert transfer_mode_for_provider(ProviderInstanceName("modal")) == TransferMode.GIT_MIRROR


def _make_config(provider: str = "local", snapshot: SnapshotName | None = None) -> TmrLaunchConfig:
    """Build a TmrLaunchConfig for unit testing.

    Uses model_construct to skip validation of the source_host field,
    which requires a real OnlineHostInterface that these unit tests don't need.
    """
    return TmrLaunchConfig.model_construct(
        source_dir=Path("/tmp/src"),
        source_host=None,
        base_commit="0" * 40,
        agent_type=AgentTypeName("claude"),
        provider_name=ProviderInstanceName(provider),
        env_options=AgentEnvironmentOptions(),
        label_options=AgentLabelOptions(),
        snapshot=snapshot,
    )


def test_build_agent_options_rsync_disabled() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config(), AgentKind.TESTING_AGENT)
    assert opts.data_options.is_rsync_enabled is False


def test_build_agent_options_local_uses_worktree() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"), AgentKind.TESTING_AGENT)
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_WORKTREE


def test_build_agent_options_remote_uses_git_mirror() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("modal"), AgentKind.TESTING_AGENT)
    assert opts.git is not None
    assert opts.transfer_mode == TransferMode.GIT_MIRROR


def test_build_agent_options_local_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"), AgentKind.TESTING_AGENT)
    assert opts.ready_timeout_seconds == 10.0


def test_build_agent_options_remote_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("docker"), AgentKind.TESTING_AGENT)
    assert opts.ready_timeout_seconds == 60.0


def test_build_agent_options_passes_env_and_labels() -> None:
    env = AgentEnvironmentOptions(env_vars=(EnvVar(key="FOO", value="bar"),))
    labels = AgentLabelOptions(labels={"batch": "1"})
    config = _make_config()
    config_with_env_and_labels = TmrLaunchConfig.model_construct(
        source_dir=config.source_dir,
        source_host=None,
        base_commit=config.base_commit,
        agent_type=config.agent_type,
        provider_name=config.provider_name,
        env_options=env,
        label_options=labels,
        snapshot=None,
    )
    opts = _build_agent_options(AgentName("test"), "branch", config_with_env_and_labels, AgentKind.TESTING_AGENT)
    assert opts.environment.env_vars == (EnvVar(key="FOO", value="bar"),)
    # tmr_role is stamped automatically; everything else is preserved.
    assert opts.label_options.labels == {"batch": "1", "tmr_role": AgentKind.TESTING_AGENT.value}


def test_build_agent_options_sets_agent_name() -> None:
    opts = _build_agent_options(
        AgentName("tmr-my-test-abc123"), "mngr-tmr/my-test", _make_config(), AgentKind.TESTING_AGENT
    )
    assert opts.name == AgentName("tmr-my-test-abc123")


def test_build_agent_options_stamps_role_label_for_each_kind() -> None:
    for kind in (AgentKind.TESTING_AGENT, AgentKind.SNAPSHOTTER, AgentKind.INTEGRATOR):
        opts = _build_agent_options(AgentName("test"), "branch", _make_config(), kind)
        assert opts.label_options.labels.get("tmr_role") == kind.value


def test_build_agent_options_target_path_pins_work_dir() -> None:
    opts = _build_agent_options(
        AgentName("test"), "branch", _make_config("modal"), AgentKind.TESTING_AGENT, target_path=Path("/code")
    )
    assert opts.target_path == Path("/code")


def test_build_agent_options_transfer_mode_override_wins() -> None:
    opts = _build_agent_options(
        AgentName("test"),
        "branch",
        _make_config("modal"),
        AgentKind.TESTING_AGENT,
        transfer_mode=TransferMode.GIT_WORKTREE,
    )
    assert opts.transfer_mode == TransferMode.GIT_WORKTREE


def test_build_agent_prompt_contains_test_id() -> None:
    prompt = build_test_agent_prompt("tests/test_foo.py::test_bar", ())
    assert "tests/test_foo.py::test_bar" in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
    assert "IMPROVE_TEST" in prompt
    assert "FIX_TEST" in prompt
    assert "FIX_IMPL" in prompt
    assert "tests_passing_before" in prompt
    assert "tests_passing_after" in prompt
    assert "summary_markdown" in prompt


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


# --- integrator prompt tests ---


def test_local_integrator_prompt_lists_branches() -> None:
    branches = ["mngr-tmr/r/a", "mngr-tmr/r/b"]
    prompt = build_local_integrator_prompt(branches)
    for branch in branches:
        assert branch in prompt
    assert "already exist" in prompt
    assert "cherry-pick" in prompt.lower()
    assert "outputs.tar.gz" in prompt


def test_remote_integrator_prompt_references_inputs_dir_and_predicate() -> None:
    prompt = build_remote_integrator_prompt()
    assert INTEGRATOR_INPUTS_DIRNAME in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
    # The remote prompt must encode the should-pull predicate itself.
    assert "SUCCEEDED" in prompt
    assert "tests_passing_before" in prompt
    assert "tests_passing_after" in prompt
    assert "git bundle list-heads" in prompt
    assert "outputs.tar.gz" in prompt
