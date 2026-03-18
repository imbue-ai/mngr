"""Unit tests for test-mapreduce API functions."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import EnvVar
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng_tmr.api import CollectTestsError
from imbue.mng_tmr.api import PLUGIN_NAME
from imbue.mng_tmr.api import _build_agent_options
from imbue.mng_tmr.api import _build_agent_prompt
from imbue.mng_tmr.api import _build_grouped_tables
from imbue.mng_tmr.api import _build_stacked_bar
from imbue.mng_tmr.api import _copy_mode_for_provider
from imbue.mng_tmr.api import _render_markdown
from imbue.mng_tmr.api import _sanitize_test_name_for_agent
from imbue.mng_tmr.api import _short_random_id
from imbue.mng_tmr.api import build_current_results
from imbue.mng_tmr.api import collect_tests
from imbue.mng_tmr.api import generate_html_report
from imbue.mng_tmr.data_types import TestAgentInfo
from imbue.mng_tmr.data_types import TestMapReduceResult
from imbue.mng_tmr.data_types import TestOutcome
from imbue.mng_tmr.data_types import TmrLaunchConfig


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


def test_copy_mode_local_provider_uses_worktree() -> None:
    assert _copy_mode_for_provider(ProviderInstanceName("local")) == WorkDirCopyMode.WORKTREE


def test_copy_mode_remote_provider_uses_clone() -> None:
    assert _copy_mode_for_provider(ProviderInstanceName("docker")) == WorkDirCopyMode.CLONE
    assert _copy_mode_for_provider(ProviderInstanceName("modal")) == WorkDirCopyMode.CLONE


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
    assert opts.git.copy_mode == WorkDirCopyMode.WORKTREE


def test_build_agent_options_remote_uses_clone() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("modal"))
    assert opts.git is not None
    assert opts.git.copy_mode == WorkDirCopyMode.CLONE


def test_build_agent_options_local_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("local"))
    assert opts.ready_timeout_seconds == 10.0


def test_build_agent_options_remote_ready_timeout() -> None:
    opts = _build_agent_options(AgentName("test"), "branch", _make_config("docker"))
    assert opts.ready_timeout_seconds == 60.0


def test_build_agent_options_passes_env_and_labels() -> None:
    config = _make_config()
    env = AgentEnvironmentOptions(env_vars=(EnvVar(key="FOO", value="bar"),))
    labels = AgentLabelOptions(labels={"batch": "1"})
    updated = config.model_construct(
        **{**config.__dict__, "env_options": env, "label_options": labels},
    )
    opts = _build_agent_options(AgentName("test"), "branch", updated)
    assert opts.environment.env_vars == (EnvVar(key="FOO", value="bar"),)
    assert opts.label_options.labels == {"batch": "1"}


def test_build_agent_options_sets_host_name_to_agent_name() -> None:
    """Verify _build_agent_options sets agent name (used as host name by callers)."""
    opts = _build_agent_options(AgentName("tmr-my-test-abc123"), "mng-tmr/my-test", _make_config())
    assert opts.name == AgentName("tmr-my-test-abc123")


def test_build_agent_prompt_contains_test_id() -> None:
    prompt = _build_agent_prompt("tests/test_foo.py::test_bar", ())
    assert "tests/test_foo.py::test_bar" in prompt
    assert "RUN_SUCCEEDED" in prompt
    assert "FIX_TEST_SUCCEEDED" in prompt
    assert "FIX_IMPL_SUCCEEDED" in prompt
    assert "FIX_UNCERTAIN" in prompt
    assert "result.json" in prompt


def test_build_agent_prompt_contains_plugin_name() -> None:
    prompt = _build_agent_prompt("tests/test_x.py::test_y", ())
    assert PLUGIN_NAME in prompt


def test_build_agent_prompt_includes_pytest_flags() -> None:
    prompt = _build_agent_prompt("tests/test_x.py::test_y", ("-m", "release"))
    assert "-m release" in prompt


def test_build_agent_prompt_requests_markdown() -> None:
    prompt = _build_agent_prompt("t::t", ())
    assert "markdown" in prompt.lower()


def test_build_agent_prompt_with_suffix() -> None:
    prompt = _build_agent_prompt("t::t", (), prompt_suffix="Always run with --verbose flag.")
    assert "Always run with --verbose flag." in prompt


def test_build_agent_prompt_no_suffix_by_default() -> None:
    prompt = _build_agent_prompt("t::t", ())
    assert prompt.endswith("FIX_UNCERTAIN.\n")


def test_build_agent_prompt_empty_suffix_ignored() -> None:
    prompt_no_suffix = _build_agent_prompt("t::t", ())
    prompt_empty_suffix = _build_agent_prompt("t::t", (), prompt_suffix="")
    assert prompt_no_suffix == prompt_empty_suffix


def test_render_markdown_bold() -> None:
    result = _render_markdown("**bold**")
    assert "<strong>bold</strong>" in result


def test_render_markdown_plain_text() -> None:
    result = _render_markdown("plain text")
    assert "plain text" in result


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


def test_build_stacked_bar_empty() -> None:
    assert _build_stacked_bar({}, 0) == ""


def test_build_stacked_bar_single_outcome() -> None:
    bar_html = _build_stacked_bar({TestOutcome.RUN_SUCCEEDED: 5}, 5)
    assert "width: 100.0%" in bar_html
    assert "RUN_SUCCEEDED: 5" in bar_html


def test_build_stacked_bar_multiple_outcomes() -> None:
    bar_html = _build_stacked_bar({TestOutcome.RUN_SUCCEEDED: 3, TestOutcome.FIX_IMPL_FAILED: 2}, 5)
    assert "RUN_SUCCEEDED: 3" in bar_html
    assert "FIX_IMPL_FAILED: 2" in bar_html


def test_build_grouped_tables_groups_by_outcome() -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::a", agent_name=AgentName("a"), outcome=TestOutcome.RUN_SUCCEEDED, summary="ok"
        ),
        TestMapReduceResult(
            test_node_id="t::b",
            agent_name=AgentName("b"),
            outcome=TestOutcome.FIX_IMPL_SUCCEEDED,
            summary="fixed",
            branch_name="mng-tmr/b",
        ),
    ]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("FIX_IMPL_SUCCEEDED") < tables_html.index("RUN_SUCCEEDED")


def test_build_grouped_tables_shows_branch() -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::c",
            agent_name=AgentName("c"),
            outcome=TestOutcome.FIX_TEST_SUCCEEDED,
            summary="fixed",
            branch_name="mng-tmr/c-abc123",
        ),
    ]
    assert "mng-tmr/c-abc123" in _build_grouped_tables(results)


def test_build_grouped_tables_renders_markdown_summary() -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::d",
            agent_name=AgentName("d"),
            outcome=TestOutcome.RUN_SUCCEEDED,
            summary="Test **passed** with `no issues`.",
        ),
    ]
    tables_html = _build_grouped_tables(results)
    assert "<strong>passed</strong>" in tables_html
    assert "<code>no issues</code>" in tables_html


def test_generate_html_report(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="tests/test_a.py::test_pass",
            agent_name=AgentName("tmr-test-pass"),
            outcome=TestOutcome.RUN_SUCCEEDED,
            summary="Passed immediately",
        ),
        TestMapReduceResult(
            test_node_id="tests/test_b.py::test_fixed",
            agent_name=AgentName("tmr-test-fixed"),
            outcome=TestOutcome.FIX_IMPL_SUCCEEDED,
            summary="Fixed missing import",
            branch_name="mng-tmr/test-fixed",
        ),
    ]
    output_path = tmp_path / "report.html"
    result_path = generate_html_report(results, output_path)
    assert result_path == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "Test Map-Reduce Report" in content
    assert "RUN_SUCCEEDED" in content
    assert "FIX_IMPL_SUCCEEDED" in content
    assert 'class="bar"' in content


def test_generate_html_report_groups_run_succeeded_last(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::pass1", agent_name=AgentName("a1"), outcome=TestOutcome.RUN_SUCCEEDED, summary="ok"
        ),
        TestMapReduceResult(
            test_node_id="t::fail1", agent_name=AgentName("a2"), outcome=TestOutcome.FIX_IMPL_FAILED, summary="failed"
        ),
    ]
    output_path = tmp_path / "grouped.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    assert content.index("FIX_IMPL_FAILED") < content.index("RUN_SUCCEEDED")


def test_generate_html_report_creates_parent_dirs(tmp_path: Path) -> None:
    output_path = tmp_path / "subdir" / "nested" / "report.html"
    results = [
        TestMapReduceResult(
            test_node_id="tests/test.py::test_x",
            agent_name=AgentName("tmr-test-x"),
            outcome=TestOutcome.RUN_SUCCEEDED,
            summary="ok",
        ),
    ]
    generate_html_report(results, output_path)
    assert output_path.exists()


def test_generate_html_report_all_outcomes(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id=f"t::test_{outcome.name.lower()}",
            agent_name=AgentName(f"tmr-{outcome.name.lower()}"),
            outcome=outcome,
            summary=f"Summary for {outcome.value}",
        )
        for outcome in TestOutcome
    ]
    output_path = tmp_path / "all_outcomes.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    for outcome in TestOutcome:
        assert outcome.value in content


def test_generate_html_report_empty_results(tmp_path: Path) -> None:
    output_path = tmp_path / "empty.html"
    generate_html_report([], output_path)
    assert "0 test(s)" in output_path.read_text()


def test_generate_html_report_with_integrator_branch(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::a",
            agent_name=AgentName("a"),
            outcome=TestOutcome.FIX_IMPL_SUCCEEDED,
            summary="fixed",
            branch_name="mng-tmr/a",
        ),
    ]
    output_path = tmp_path / "integrator.html"
    generate_html_report(results, output_path, integrator_branch="mng-tmr/integrated-abc123")
    content = output_path.read_text()
    assert "integrator" in content
    assert "mng-tmr/integrated-abc123" in content


def test_generate_html_report_without_integrator_branch(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::a", agent_name=AgentName("a"), outcome=TestOutcome.RUN_SUCCEEDED, summary="ok"
        ),
    ]
    output_path = tmp_path / "no_integrator.html"
    generate_html_report(results, output_path)
    content = output_path.read_text()
    assert "Integrated branch:" not in content


def test_generate_html_report_integrator_branch_html_escaped(tmp_path: Path) -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::a", agent_name=AgentName("a"), outcome=TestOutcome.RUN_SUCCEEDED, summary="ok"
        ),
    ]
    output_path = tmp_path / "escape.html"
    generate_html_report(results, output_path, integrator_branch="<script>alert('xss')</script>")
    content = output_path.read_text()
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


def test_build_stacked_bar_pending_outcome() -> None:
    bar_html = _build_stacked_bar({TestOutcome.PENDING: 3}, 3)
    assert "PENDING: 3" in bar_html
    assert "rgb(3, 169, 244)" in bar_html


def test_build_grouped_tables_pending_first() -> None:
    results = [
        TestMapReduceResult(
            test_node_id="t::a", agent_name=AgentName("a"), outcome=TestOutcome.PENDING, summary="running"
        ),
        TestMapReduceResult(
            test_node_id="t::b", agent_name=AgentName("b"), outcome=TestOutcome.RUN_SUCCEEDED, summary="ok"
        ),
    ]
    tables_html = _build_grouped_tables(results)
    assert tables_html.index("PENDING") < tables_html.index("RUN_SUCCEEDED")


def test_build_current_results_pending_agents() -> None:
    """Agents not in final_details should get PENDING outcome."""
    agents = [
        TestAgentInfo(
            test_node_id="tests/test_a.py::test_one",
            agent_id=AgentId.generate(),
            agent_name=AgentName("tmr-test-one-abc123"),
        ),
        TestAgentInfo(
            test_node_id="tests/test_b.py::test_two",
            agent_id=AgentId.generate(),
            agent_name=AgentName("tmr-test-two-def456"),
        ),
    ]
    results = build_current_results(
        agents=agents,
        final_details={},
        timed_out_ids=set(),
        hosts={},
    )
    assert len(results) == 2
    assert results[0].outcome == TestOutcome.PENDING
    assert results[1].outcome == TestOutcome.PENDING
    assert "still running" in results[0].summary


def test_build_current_results_timed_out_agents() -> None:
    """Timed-out agents should get TIMED_OUT outcome."""
    agent_id = AgentId.generate()
    agents = [
        TestAgentInfo(
            test_node_id="tests/test_a.py::test_one",
            agent_id=agent_id,
            agent_name=AgentName("tmr-test-one-abc123"),
        ),
    ]
    results = build_current_results(
        agents=agents,
        final_details={},
        timed_out_ids={str(agent_id)},
        hosts={},
    )
    assert len(results) == 1
    assert results[0].outcome == TestOutcome.TIMED_OUT
