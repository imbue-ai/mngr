"""Unit tests for the spec-anchored TMR outcome models and parsers."""

import json
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.mngr_specs.data_types import SpecCoverage
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.spec_report import SpecChangeKind
from imbue.mngr_tmr.spec_report import SpecUnitVerdict
from imbue.mngr_tmr.spec_report import coverage_of_verdict
from imbue.mngr_tmr.spec_report import load_matrix_records
from imbue.mngr_tmr.spec_report import parse_spec_outcome_json

_FULL_OUTCOME_JSON = json.dumps(
    {
        "changes": {
            "CREATE_TEST": {"status": "SUCCEEDED", "summary_markdown": "Created two witnesses"},
            "FIX_IMPL": {"status": "BLOCKED", "summary_markdown": "Auth handler diverges from spec"},
        },
        "units": [
            {
                "coordinate": "authentication.fresh-code",
                "verdict": "FULL",
                "witnesses": [{"test": "apps/minds/test_auth.py::test_fresh_code", "partial": None}],
                "blockers": [],
                "spec_problems": [],
                "summary_markdown": "Fully witnessed.",
            },
            {
                "coordinate": "authentication.single-use-codes",
                "verdict": "PARTIAL_STEADY",
                "witnesses": [
                    {
                        "test": "apps/minds/test_auth.py::test_spent_code_refused",
                        "partial": "does not exercise concurrent interleavings",
                    }
                ],
                "blockers": ["needs a Docker daemon to drive the full flow"],
                "spec_problems": [
                    {
                        "problem": "The rule seems to forbid a flow signin.feature requires",
                        "proposed_edit": "Scope the rule to exclude the refresh flow",
                    }
                ],
                "summary_markdown": "Steady partial; the residue is untestable in kind.",
            },
        ],
        "errored": False,
        "tests_passing_before": None,
        "tests_passing_after": True,
        "summary_markdown": "Witnessed the signin file.",
        "test_runs": [{"run_name": "try_1", "description_markdown": "initial run"}],
    }
)


def test_parse_spec_outcome_json_round_trips_the_full_schema() -> None:
    result = parse_spec_outcome_json(_FULL_OUTCOME_JSON)

    assert result.changes[SpecChangeKind.CREATE_TEST].status == ChangeStatus.SUCCEEDED
    assert result.changes[SpecChangeKind.FIX_IMPL].status == ChangeStatus.BLOCKED
    assert result.errored is False
    assert result.tests_passing_before is None
    assert result.tests_passing_after is True
    assert [unit.coordinate for unit in result.units] == snapshot(
        ["authentication.fresh-code", "authentication.single-use-codes"]
    )
    steady_unit = result.units[1]
    assert steady_unit.verdict == SpecUnitVerdict.PARTIAL_STEADY
    assert steady_unit.witnesses[0].partial == "does not exercise concurrent interleavings"
    assert steady_unit.blockers == ("needs a Docker daemon to drive the full flow",)
    assert steady_unit.spec_problems[0].proposed_edit == "Scope the rule to exclude the refresh flow"
    assert result.test_runs[0].run_name == "try_1"


def test_parse_spec_outcome_json_defaults_for_minimal_payload() -> None:
    result = parse_spec_outcome_json("{}")

    assert result.changes == {}
    assert result.units == ()
    assert result.errored is False
    assert result.tests_passing_before is None
    assert result.tests_passing_after is None
    assert result.summary_markdown == ""
    assert result.test_runs == ()


def test_parse_spec_outcome_json_rejects_unknown_verdict() -> None:
    raw = json.dumps({"units": [{"coordinate": "a.b", "verdict": "PARTIALLY_DONE"}]})

    with pytest.raises(ValueError):
        parse_spec_outcome_json(raw)


def test_parse_spec_outcome_json_rejects_unknown_change_kind() -> None:
    raw = json.dumps({"changes": {"FIX_TUTORIAL": {"status": "SUCCEEDED", "summary_markdown": "x"}}})

    with pytest.raises(ValueError):
        parse_spec_outcome_json(raw)


def test_coverage_of_verdict_projects_onto_matrix_vocabulary() -> None:
    assert coverage_of_verdict(SpecUnitVerdict.NONE) == SpecCoverage.NONE
    assert coverage_of_verdict(SpecUnitVerdict.PARTIAL_IMPROVABLE) == SpecCoverage.PARTIAL
    assert coverage_of_verdict(SpecUnitVerdict.PARTIAL_STEADY) == SpecCoverage.PARTIAL
    assert coverage_of_verdict(SpecUnitVerdict.FULL) == SpecCoverage.FULL


def _write_matrix_artifact(path: Path, lines: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_load_matrix_records_indexes_by_coordinate(tmp_path: Path) -> None:
    matrix_path = tmp_path / "matrix.jsonl"
    _write_matrix_artifact(
        matrix_path,
        [
            {
                "coordinate": "authentication.fresh-code",
                "kind": "scenario",
                "name": "Opening a fresh login URL signs the user in",
                "file": "apps/minds/specs/authentication/signin.feature",
                "line": 10,
                "coverage": "full",
                "witnesses": [{"test": "apps/minds/test_auth.py::test_fresh_code", "partial": None}],
            },
            {
                "coordinate": "authentication.single-use-codes",
                "kind": "rule",
                "name": "A one-time code grants at most one session, ever",
                "file": "apps/minds/specs/authentication/invariants.feature",
                "line": 7,
                "coverage": "partial",
                "witnesses": [{"test": "apps/minds/test_auth.py::test_spent", "partial": "no interleavings"}],
            },
        ],
    )

    record_by_coordinate = load_matrix_records(matrix_path)

    assert record_by_coordinate is not None
    assert record_by_coordinate["authentication.fresh-code"].coverage == SpecCoverage.FULL
    partial_record = record_by_coordinate["authentication.single-use-codes"]
    assert partial_record.coverage == SpecCoverage.PARTIAL
    assert partial_record.witnesses[0].partial == "no interleavings"


def test_load_matrix_records_returns_none_when_artifact_is_absent(tmp_path: Path) -> None:
    assert load_matrix_records(tmp_path / "matrix.jsonl") is None


def test_load_matrix_records_skips_malformed_lines_but_keeps_the_rest(tmp_path: Path) -> None:
    matrix_path = tmp_path / "matrix.jsonl"
    good_line = json.dumps(
        {"coordinate": "authentication.fresh-code", "coverage": "none", "witnesses": []}
    )
    matrix_path.write_text(f"{good_line}\nnot json at all\n")

    record_by_coordinate = load_matrix_records(matrix_path)

    assert record_by_coordinate is not None
    assert set(record_by_coordinate) == {"authentication.fresh-code"}
    assert record_by_coordinate["authentication.fresh-code"].coverage == SpecCoverage.NONE
