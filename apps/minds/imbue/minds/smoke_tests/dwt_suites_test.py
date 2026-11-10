import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.minds.smoke_tests.data_types import DwtGroupAttempt
from imbue.minds.smoke_tests.data_types import DwtJunitCase
from imbue.minds.smoke_tests.data_types import DwtTestGroup
from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.dwt_suites import dwt_group_command_line
from imbue.minds.smoke_tests.dwt_suites import dwt_results_markdown
from imbue.minds.smoke_tests.dwt_suites import dwt_subtest_label
from imbue.minds.smoke_tests.dwt_suites import is_dwt_subtest_label
from imbue.minds.smoke_tests.dwt_suites import is_dwt_tests_skip_requested
from imbue.minds.smoke_tests.dwt_suites import merge_dwt_group_attempts
from imbue.minds.smoke_tests.dwt_suites import parse_dwt_offload_config
from imbue.minds.smoke_tests.dwt_suites import parse_junit_cases
from imbue.minds.smoke_tests.dwt_suites import smoke_run_verdict
from imbue.minds.smoke_tests.primitives import SMOKE_SKIP_DWT_TESTS_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeDwtReportError

# The shape of default-workspace-template's offload-modal-chat.toml, sections it
# does not run with (provider, checkpoint) included so the parse is shown to skip them.
_CHAT_CONFIG = r"""
[offload]
max_parallel = 20
test_timeout_secs = 600
sandbox_repo_root = "/home/user/workspace"
sandbox_project_root = "/home/user/workspace/system/apps/chat"

[provider]
type = "modal"
dockerfile = "system/Dockerfile"

[checkpoint]
build_inputs = ["system/Dockerfile"]

[framework]
type = "pytest"
command = "bash -c '[ -d system/apps/chat ] && cd system/apps/chat; exec uv run pytest \"$@\"' --"
run_args = "-m '' --cov-fail-under=0"
discovery_args = "--no-cov --assert=plain -m ''"
env = { DWT_REQUIRE_WORKSPACE_ENV = "1", MNGR_AGENT_ID = "agent-c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1" }

[groups.all]
retry_count = 0
filters = "-m 'not flaky and not real_claude'"

[groups.flaky]
retry_count = 4
filters = "-m 'flaky and not real_claude'"

[report]
output_dir = "test-results/chat"
junit = true
junit_file = "junit.xml"
"""

# The root suite's config has no run_args.
_ROOT_CONFIG = """
[framework]
type = "pytest"
command = "uv run pytest"
env = { DWT_REQUIRE_WORKSPACE_ENV = "1" }

[groups.all]
retry_count = 0
filters = "-m 'not flaky'"

[report]
output_dir = "test-results/root"
"""


def test_a_suite_is_read_with_its_command_arguments_environment_and_groups() -> None:
    suite = parse_dwt_offload_config("offload-modal-chat.toml", _CHAT_CONFIG)

    assert suite.name == "chat"
    assert suite.command == """bash -c '[ -d system/apps/chat ] && cd system/apps/chat; exec uv run pytest "$@"' --"""
    assert suite.run_args == "-m '' --cov-fail-under=0"
    assert suite.environment == {
        "DWT_REQUIRE_WORKSPACE_ENV": "1",
        "MNGR_AGENT_ID": "agent-c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1",
    }
    assert suite.groups == (
        DwtTestGroup(
            name=NonEmptyStr("all"), filters="-m 'not flaky and not real_claude'", retry_count=NonNegativeInt(0)
        ),
        DwtTestGroup(
            name=NonEmptyStr("flaky"), filters="-m 'flaky and not real_claude'", retry_count=NonNegativeInt(4)
        ),
    )


def test_a_suite_without_run_args_adds_none() -> None:
    suite = parse_dwt_offload_config("offload-modal.toml", _ROOT_CONFIG)

    assert suite.name == "root"
    assert suite.run_args == ""
    assert suite.groups == (
        DwtTestGroup(name=NonEmptyStr("all"), filters="-m 'not flaky'", retry_count=NonNegativeInt(0)),
    )


@pytest.mark.parametrize(
    ("config_text", "expected_error"),
    [
        ("[framework\n", "is not TOML"),
        (_ROOT_CONFIG.replace('command = "uv run pytest"\n', ""), "command"),
        (_ROOT_CONFIG.replace('type = "pytest"', 'type = "nextest"'), "runs nextest, not pytest"),
        (_ROOT_CONFIG.replace('output_dir = "test-results/root"', ""), "output_dir"),
        (_ROOT_CONFIG.replace("DWT_REQUIRE_WORKSPACE_ENV", '"NOT A NAME"'), "NOT A NAME"),
        (_ROOT_CONFIG.split("[groups.all]")[0] + '[report]\noutput_dir = "test-results/root"\n', "no test groups"),
    ],
)
def test_a_config_the_suite_cannot_run_as_ci_does_is_refused_naming_the_file(
    config_text: str, expected_error: str
) -> None:
    with pytest.raises(SmokeConfigError, match="offload-modal.toml") as raised:
        parse_dwt_offload_config("offload-modal.toml", config_text)
    assert expected_error in str(raised.value)


def test_a_group_runs_the_suite_s_command_under_its_environment_with_its_run_args_then_the_group_s_filters() -> None:
    suite = parse_dwt_offload_config("offload-modal-chat.toml", _CHAT_CONFIG)

    command_line = dwt_group_command_line(suite, suite.groups[0])

    assert command_line == (
        "env DWT_REQUIRE_WORKSPACE_ENV=1 MNGR_AGENT_ID=agent-c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1 "
        """bash -c '[ -d system/apps/chat ] && cd system/apps/chat; exec uv run pytest "$@"' -- """
        "-m '' --cov-fail-under=0 -m 'not flaky and not real_claude'"
    )


def test_an_environment_value_is_quoted_for_the_shell() -> None:
    suite = parse_dwt_offload_config(
        "offload-modal.toml", _ROOT_CONFIG.replace('DWT_REQUIRE_WORKSPACE_ENV = "1"', 'NOTE = "two words"')
    )

    assert dwt_group_command_line(suite, suite.groups[0]) == "env NOTE='two words' uv run pytest -m 'not flaky'"


# A pytest junit report in the xunit1 family, as the runner asks pytest to write it.
_JUNIT_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="1" skipped="2" tests="8">
<testcase classname="pkg.v1.test_sample" name="test_passes" file="pkg.v1/test_sample.py" line="3" time="0.000"/>
<testcase classname="pkg.v1.test_sample" name="test_fails" file="pkg.v1/test_sample.py" line="7" time="0.000">
<failure message="AssertionError: values differ&#10;assert 1 == 2">def test_fails() -&gt; None:
&gt;       assert 1 == 2, "values differ"
E       AssertionError: values differ</failure></testcase>
<testcase classname="pkg.v1.test_sample" name="test_param[a/b.c]" file="pkg.v1/test_sample.py" line="11" time="0.000"/>
<testcase classname="pkg.v1.test_sample" name="test_skips" file="pkg.v1/test_sample.py" line="16" time="0.000">
<skipped type="pytest.skip" message="no browser here">pkg.v1/test_sample.py:18: no browser here</skipped></testcase>
<testcase classname="pkg.v1.test_sample" name="test_xfails" file="pkg.v1/test_sample.py" line="20" time="0.006">
<skipped type="pytest.xfail" message="known bug"/></testcase>
<testcase classname="pkg.v1.test_sample.TestGrouped" name="test_inside" file="pkg.v1/test_sample.py" line="26" time="0.000"/>
<testcase classname="pkg.v1.test_sample" name="test_errors_in_setup" file="pkg.v1/test_sample.py" line="35" time="0.000">
<error message="failed on setup with &quot;RuntimeError: fixture exploded&quot;">E       RuntimeError: fixture exploded</error></testcase>
<testcase classname="" name="broken.test_broken" file="broken/test_broken.py" time="0.000">
<error message="collection failure">E   ModuleNotFoundError: No module named 'nonexistent_module_xyz'</error></testcase>
<testcase classname="pkg.v1.test_sample" name="test_imported" file="pkg.v1/helpers.py" line="2" time="0.000"/>
</testsuite></testsuites>
"""


def test_a_junit_report_reads_as_one_case_per_test_named_by_its_node_id() -> None:
    cases = parse_junit_cases(_JUNIT_REPORT)

    assert [(str(case.test_id), case.outcome) for case in cases] == [
        ("pkg.v1/test_sample.py::test_passes", DwtTestOutcome.PASSED),
        ("pkg.v1/test_sample.py::test_fails", DwtTestOutcome.FAILED),
        ("pkg.v1/test_sample.py::test_param[a/b.c]", DwtTestOutcome.PASSED),
        ("pkg.v1/test_sample.py::test_skips", DwtTestOutcome.SKIPPED),
        ("pkg.v1/test_sample.py::test_xfails", DwtTestOutcome.SKIPPED),
        ("pkg.v1/test_sample.py::TestGrouped::test_inside", DwtTestOutcome.PASSED),
        ("pkg.v1/test_sample.py::test_errors_in_setup", DwtTestOutcome.FAILED),
        ("broken/test_broken.py", DwtTestOutcome.FAILED),
        # Defined in another file than the one it was collected from, so only its classname places it.
        ("pkg.v1.test_sample::test_imported", DwtTestOutcome.PASSED),
    ]


def test_a_case_s_detail_is_its_failure_text_or_its_skip_reason() -> None:
    detail_by_id = {str(case.test_id): case.detail for case in parse_junit_cases(_JUNIT_REPORT)}

    assert detail_by_id["pkg.v1/test_sample.py::test_passes"] == ""
    assert "AssertionError: values differ" in detail_by_id["pkg.v1/test_sample.py::test_fails"]
    assert detail_by_id["pkg.v1/test_sample.py::test_skips"] == "no browser here"
    assert detail_by_id["pkg.v1/test_sample.py::test_xfails"] == "xfail: known bug"
    assert "RuntimeError: fixture exploded" in detail_by_id["pkg.v1/test_sample.py::test_errors_in_setup"]
    assert "No module named 'nonexistent_module_xyz'" in detail_by_id["broken/test_broken.py"]


def test_a_report_that_is_not_xml_is_refused() -> None:
    with pytest.raises(SmokeDwtReportError, match="not a junit report"):
        parse_junit_cases("<testsuites><testsuite>")


def _case(test_id: str, outcome: DwtTestOutcome, detail: str = "") -> DwtJunitCase:
    return DwtJunitCase(test_id=NonEmptyStr(test_id), outcome=outcome, detail=detail)


def _attempt(
    number: int, exit_code: int | None, cases: tuple[DwtJunitCase, ...] | None, log_tail: str = ""
) -> DwtGroupAttempt:
    return DwtGroupAttempt(number=PositiveInt(number), exit_code=exit_code, cases=cases, log_tail=log_tail)


def _result(test_id: str, outcome: DwtTestOutcome, attempt_count: int, detail: str = "") -> DwtTestResult:
    return DwtTestResult(
        suite_name=NonEmptyStr("chat"),
        test_id=NonEmptyStr(test_id),
        outcome=outcome,
        attempt_count=PositiveInt(attempt_count),
        detail=detail,
    )


def test_a_test_that_passes_on_a_retry_counts_as_passed_after_both_attempts() -> None:
    attempts = (
        _attempt(
            1,
            1,
            (_case("a.py::test_ok", DwtTestOutcome.PASSED), _case("a.py::test_flaky", DwtTestOutcome.FAILED, "boom")),
        ),
        _attempt(2, 0, (_case("a.py::test_flaky", DwtTestOutcome.PASSED),)),
    )

    assert merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("flaky"), attempts) == (
        _result("a.py::test_ok", DwtTestOutcome.PASSED, 1),
        _result("a.py::test_flaky", DwtTestOutcome.PASSED, 2),
    )


def test_a_test_that_fails_every_attempt_keeps_its_last_failure() -> None:
    attempts = (
        _attempt(1, 1, (_case("a.py::test_broken", DwtTestOutcome.FAILED, "first"),)),
        _attempt(2, 1, (_case("a.py::test_broken", DwtTestOutcome.FAILED, "second"),)),
    )

    assert merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("flaky"), attempts) == (
        _result("a.py::test_broken", DwtTestOutcome.FAILED, 2, "second"),
    )


def test_a_group_with_no_tests_to_run_reports_nothing() -> None:
    assert merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("flaky"), (_attempt(1, 5, ()),)) == ()


@pytest.mark.parametrize(
    ("attempt", "expected_detail"),
    [
        (_attempt(1, None, None), "did not finish"),
        (_attempt(1, 0, None, "Traceback: disk full"), "without writing its report"),
        (_attempt(1, 3, (), "INTERNALERROR> boom"), "pytest exited 3"),
        (_attempt(1, 1, (_case("a.py::test_ok", DwtTestOutcome.PASSED),), "coverage floor"), "no failing test"),
    ],
)
def test_a_run_that_went_wrong_is_reported_as_the_group_s_own_failure(
    attempt: DwtGroupAttempt, expected_detail: str
) -> None:
    results = merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("all"), (attempt,))

    problems = [result for result in results if result.test_id == "[all group]"]
    assert len(problems) == 1
    assert problems[0].outcome == DwtTestOutcome.FAILED
    assert expected_detail in problems[0].detail
    assert attempt.log_tail in problems[0].detail


def test_an_interrupted_run_reports_what_it_collected_and_its_own_failure() -> None:
    attempts = (_attempt(1, 2, (_case("broken/test_broken.py", DwtTestOutcome.FAILED, "ModuleNotFoundError"),)),)

    results = merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("all"), attempts)

    assert [(str(result.test_id), result.outcome) for result in results] == [
        ("broken/test_broken.py", DwtTestOutcome.FAILED),
        ("[all group]", DwtTestOutcome.FAILED),
    ]


def test_a_group_the_runner_never_reached_is_reported_as_its_failure() -> None:
    (result,) = merge_dwt_group_attempts(NonEmptyStr("chat"), NonEmptyStr("all"), ())

    assert result.test_id == "[all group]"
    assert result.outcome == DwtTestOutcome.FAILED
    assert "never ran" in result.detail


def test_a_result_s_subtest_label_names_its_suite_and_test_and_is_recognized_as_a_dwt_one() -> None:
    label = dwt_subtest_label(_result("imbue/chat/test_x.py::test_y[a]", DwtTestOutcome.PASSED, 1))

    assert label == "dwt:chat::imbue/chat/test_x.py::test_y[a]"
    assert is_dwt_subtest_label(label) is True
    assert is_dwt_subtest_label("Step 3b: run default-workspace-template's CI suites") is False
    assert is_dwt_subtest_label(None) is False


def test_the_verdict_separates_failed_dwt_tests_from_every_other_failure() -> None:
    verdict = smoke_run_verdict(
        own_failure_count=1,
        failed_subtest_labels=(
            "dwt:chat::imbue/chat/test_x.py::test_y",
            "dwt:root::[all group]",
            "Step 3b: run default-workspace-template's CI suites in the workspace",
            None,
        ),
    )

    assert verdict.dwt_failure_count == 2
    assert verdict.other_failure_count == 3


def test_a_session_whose_only_failures_are_dwt_tests_has_no_other_failure() -> None:
    verdict = smoke_run_verdict(own_failure_count=0, failed_subtest_labels=("dwt:root::a.py::test_b",))

    assert (verdict.dwt_failure_count, verdict.other_failure_count) == (1, 0)


def test_the_summary_counts_each_suite_s_outcomes_and_lists_each_failure() -> None:
    results = (
        _result("imbue/chat/test_x.py::test_passes", DwtTestOutcome.PASSED, 1),
        _result("imbue/chat/test_x.py::test_broken", DwtTestOutcome.FAILED, 5, "boom"),
        DwtTestResult(
            suite_name=NonEmptyStr("root"),
            test_id=NonEmptyStr("a.py::test_xfails"),
            outcome=DwtTestOutcome.SKIPPED,
            attempt_count=PositiveInt(1),
            detail="xfail: known bug",
        ),
    )

    assert dwt_results_markdown(results, template_ref=None) == (
        "## default-workspace-template's CI suites in the workspace\n"
        "\n"
        "| Suite | Passed | Failed | Skipped |\n"
        "| --- | --- | --- | --- |\n"
        "| chat | 1 | 1 | 0 |\n"
        "| root | 0 | 0 | 1 |\n"
        "\n"
        "### Failed\n"
        "\n"
        "- `dwt:chat::imbue/chat/test_x.py::test_broken` (all 5 attempts)\n"
    )


def test_a_summary_without_failures_lists_none() -> None:
    markdown = dwt_results_markdown((_result("a.py::test_ok", DwtTestOutcome.PASSED, 1),), template_ref=None)

    assert "| chat | 1 | 0 | 0 |" in markdown
    assert "### Failed" not in markdown


def test_the_summary_names_the_template_the_workspace_s_row_was_baked_at() -> None:
    markdown = dwt_results_markdown(
        (_result("a.py::test_ok", DwtTestOutcome.PASSED, 1),), template_ref="minds-v0.10.1"
    )

    assert markdown.startswith(
        "## default-workspace-template's CI suites in the workspace\n"
        "\n"
        "Run in a workspace on a pool row baked at template `minds-v0.10.1`.\n"
        "\n"
        "| Suite | Passed | Failed | Skipped |\n"
    )


def test_the_dwt_tests_run_unless_the_environment_says_to_skip_them() -> None:
    assert is_dwt_tests_skip_requested({"PATH": "/usr/bin"}) is False
    assert is_dwt_tests_skip_requested({SMOKE_SKIP_DWT_TESTS_ENV_VAR: ""}) is False
    assert is_dwt_tests_skip_requested({SMOKE_SKIP_DWT_TESTS_ENV_VAR: "0"}) is False


def test_1_skips_the_dwt_tests() -> None:
    assert is_dwt_tests_skip_requested({SMOKE_SKIP_DWT_TESTS_ENV_VAR: "1"}) is True


def test_a_skip_value_other_than_1_or_0_is_refused() -> None:
    with pytest.raises(
        SmokeConfigError, match=r"MINDS_SMOKE_SKIP_DWT_TESTS takes 1 \(skip\) or 0 \(run\), not 'true'"
    ):
        is_dwt_tests_skip_requested({SMOKE_SKIP_DWT_TESTS_ENV_VAR: "true"})
