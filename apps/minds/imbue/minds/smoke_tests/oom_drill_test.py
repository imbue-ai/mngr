import json
from pathlib import Path
from typing import Any

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt
from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.oom_drill import OOM_DRILL_SUITE_NAME
from imbue.minds.smoke_tests.oom_drill import oom_drill_markdown
from imbue.minds.smoke_tests.oom_drill import read_oom_drill_result
from imbue.minds.smoke_tests.workspace_runner import RunnerEnd

_POLICY_BANDS = [1000, 960, 943, 910, 900, 850, 600, 560, 300]
_DEADLINE_SECONDS = 420.0


def _sleeper_kill(pid: int, band: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "comm": "python3",
        "band": band,
        "judgement": {"verdict": "right", "top": {"pid": pid, "comm": "python3", "oom_score_adj": band}},
    }


def _write_drill_results(
    results_dir: Path, verdict: dict[str, Any] | None, exit_code: int | None, drill_log: str = ""
) -> Path:
    """What the drill's runner leaves, down to the empty verdict file its redirect leaves when the drill printed
    nothing."""
    results_dir.mkdir(parents=True)
    (results_dir / "verdict.json").write_text(json.dumps(verdict) if verdict is not None else "")
    if exit_code is not None:
        (results_dir / "drill.exit").write_text(f"{exit_code}\n")
    (results_dir / "drill.log").write_text(drill_log)
    return results_dir


def test_a_drill_that_shed_every_sleeper_in_band_order_passes(tmp_path: Path) -> None:
    verdict = {
        "passed": True,
        "failure": "",
        "bands": _POLICY_BANDS,
        "sleeper_bands_in_shed_order": _POLICY_BANDS,
        "kills": [_sleeper_kill(10147 + index, band) for index, band in enumerate(_POLICY_BANDS)],
    }
    results_dir = _write_drill_results(tmp_path / "oom-drill", verdict, exit_code=0)

    result = read_oom_drill_result(results_dir, RunnerEnd.FINISHED, _DEADLINE_SECONDS)

    assert (result.suite_name, result.outcome, result.detail) == (OOM_DRILL_SUITE_NAME, DwtTestOutcome.PASSED, "")


def test_a_drill_that_judged_a_kill_wrong_fails_with_the_shed_order_and_the_wrong_pick(tmp_path: Path) -> None:
    shed = [1000, 960, 943, 910, 900, 850]
    wrong_pick = {
        "pid": 559,
        "comm": "screen-watch",
        "band": None,
        "judgement": {"verdict": "wrong", "top": {"pid": 7498, "comm": "xfce4-session", "oom_score_adj": 850}},
    }
    verdict = {
        "passed": False,
        "failure": "wrong victim: pid 559 (screen-watch), predicted pid 7498",
        "bands": _POLICY_BANDS,
        "sleeper_bands_in_shed_order": shed,
        "kills": [*(_sleeper_kill(9403 + index, band) for index, band in enumerate(shed)), wrong_pick],
    }
    results_dir = _write_drill_results(tmp_path / "oom-drill", verdict, exit_code=1)

    result = read_oom_drill_result(results_dir, RunnerEnd.FINISHED, _DEADLINE_SECONDS)

    assert result.outcome == DwtTestOutcome.FAILED
    assert "The drill failed: wrong victim: pid 559 (screen-watch), predicted pid 7498" in result.detail
    assert (
        "Sleepers shed, in order: 1000, 960, 943, 910, 900, 850 (of 1000, 960, 943, 910, 900, 850, 600, 560, 300)"
        in (result.detail)
    )
    assert "pid 9403 python3, the sleeper at 1000: right" in result.detail
    assert "pid 559 screen-watch: wrong, predicted pid 7498 xfce4-session at 850" in result.detail


def test_a_drill_that_printed_no_verdict_fails_with_its_exit_code_and_its_log(tmp_path: Path) -> None:
    drill_log = (
        "Traceback (most recent call last):\n"
        '  File "<stdin>", line 344, in set_own_adj\n'
        "PermissionError: [Errno 13] Permission denied\n"
    )
    results_dir = _write_drill_results(tmp_path / "oom-drill", verdict=None, exit_code=1, drill_log=drill_log)

    result = read_oom_drill_result(results_dir, RunnerEnd.FINISHED, _DEADLINE_SECONDS)

    assert result.outcome == DwtTestOutcome.FAILED
    assert "The drill printed no verdict and exited 1." in result.detail
    assert "PermissionError: [Errno 13] Permission denied" in result.detail


def test_a_drill_whose_runner_did_not_finish_fails_saying_how_it_ended(tmp_path: Path) -> None:
    results_dir = _write_drill_results(tmp_path / "oom-drill", verdict=None, exit_code=None)

    vanished = read_oom_drill_result(results_dir, RunnerEnd.VANISHED, _DEADLINE_SECONDS)
    out_of_time = read_oom_drill_result(results_dir, RunnerEnd.OUT_OF_TIME, _DEADLINE_SECONDS)

    assert (vanished.outcome, out_of_time.outcome) == (DwtTestOutcome.FAILED, DwtTestOutcome.FAILED)
    assert "The drill's runner ended without finishing" in vanished.detail
    assert "The drill did not finish within 7 minutes, so its runner was stopped." in out_of_time.detail


def _drill_result(outcome: DwtTestOutcome, detail: str) -> DwtTestResult:
    return DwtTestResult(
        suite_name=NonEmptyStr(OOM_DRILL_SUITE_NAME),
        test_id=NonEmptyStr("system/services/oom_priority/bin/oom_drill.py"),
        outcome=outcome,
        attempt_count=PositiveInt(1),
        detail=detail,
    )


def test_the_summary_of_a_passed_drill_names_the_template_it_ran_against() -> None:
    summary = oom_drill_markdown(_drill_result(DwtTestOutcome.PASSED, ""), template_ref="minds-v0.10.5")

    assert summary == (
        "## default-workspace-template's OOM drill in the workspace\n"
        "\n"
        "Run in a workspace on a pool row baked at template `minds-v0.10.5`.\n"
        "\n"
        "Passed: every sleeper was shed in band order, each the victim the drill predicted.\n"
    )


def test_the_summary_of_a_failed_drill_carries_its_failure() -> None:
    detail = "The drill failed: the hog died\nSleepers shed, in order: none (of 1000, 300)"

    summary = oom_drill_markdown(_drill_result(DwtTestOutcome.FAILED, detail), template_ref=None)

    assert summary == (
        f"## default-workspace-template's OOM drill in the workspace\n\nFailed:\n\n```text\n{detail}\n```\n"
    )
