"""Unit tests for the spec-anchored TMR outcome models, parsers, and report rendering."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from inline_snapshot import snapshot

from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_specs.data_types import SpecCoverage
from imbue.mngr_tmr.report import Change
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.spec_report import SpecChangeKind
from imbue.mngr_tmr.spec_report import SpecReportRow
from imbue.mngr_tmr.spec_report import SpecUnitVerdict
from imbue.mngr_tmr.spec_report import UnitVerdictRecord
from imbue.mngr_tmr.spec_report import coverage_of_verdict
from imbue.mngr_tmr.spec_report import generate_spec_html_report
from imbue.mngr_tmr.spec_report import load_matrix_records
from imbue.mngr_tmr.spec_report import parse_spec_outcome_json
from imbue.mngr_tmr.spec_report import spec_report_section_of

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
    good_line = json.dumps({"coordinate": "authentication.fresh-code", "coverage": "none", "witnesses": []})
    matrix_path.write_text(f"{good_line}\nnot json at all\n")

    record_by_coordinate = load_matrix_records(matrix_path)

    assert record_by_coordinate is not None
    assert set(record_by_coordinate) == {"authentication.fresh-code"}
    assert record_by_coordinate["authentication.fresh-code"].coverage == SpecCoverage.NONE


def _row(
    *,
    changes: dict[SpecChangeKind, Change] | None = None,
    units: tuple[UnitVerdictRecord, ...] = (),
    errored: bool = False,
) -> SpecReportRow:
    return SpecReportRow(
        task_id="authentication/signin.feature",
        agent_name=AgentName(f"agent-{uuid4().hex}"),
        changes=changes or {},
        units=units,
        errored=errored,
    )


def _unit(verdict: SpecUnitVerdict) -> UnitVerdictRecord:
    return UnitVerdictRecord(coordinate=f"authentication.{uuid4().hex}", verdict=verdict)


def test_spec_report_section_errored_is_failed() -> None:
    assert spec_report_section_of(_row(errored=True)) == ReportSection.FAILED


def test_spec_report_section_no_outcome_is_running() -> None:
    assert spec_report_section_of(_row()) == ReportSection.RUNNING


def test_spec_report_section_all_blocked_changes_is_blocked() -> None:
    changes = {SpecChangeKind.CREATE_TEST: Change(status=ChangeStatus.BLOCKED, summary_markdown="x")}
    assert spec_report_section_of(_row(changes=changes)) == ReportSection.BLOCKED


def test_spec_report_section_test_kinds_are_non_impl_fixes() -> None:
    changes = {SpecChangeKind.CREATE_TEST: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="x")}
    assert spec_report_section_of(_row(changes=changes)) == ReportSection.NON_IMPL_FIXES


def test_spec_report_section_fix_impl_alone_is_impl_fixes() -> None:
    changes = {SpecChangeKind.FIX_IMPL: Change(status=ChangeStatus.SUCCEEDED, summary_markdown="x")}
    assert spec_report_section_of(_row(changes=changes)) == ReportSection.IMPL_FIXES


def test_spec_report_section_converged_units_with_no_changes_is_clean_pass() -> None:
    units = (_unit(SpecUnitVerdict.FULL), _unit(SpecUnitVerdict.PARTIAL_STEADY))
    assert spec_report_section_of(_row(units=units)) == ReportSection.CLEAN_PASS


def test_spec_report_section_unconverged_units_with_no_changes_is_blocked() -> None:
    units = (_unit(SpecUnitVerdict.FULL), _unit(SpecUnitVerdict.PARTIAL_IMPROVABLE))
    assert spec_report_section_of(_row(units=units)) == ReportSection.BLOCKED


def _mapper_metadata(agent_name: str, task_id: str, branch_name: str) -> AgentMetadata:
    return AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=AgentName(agent_name),
        task_id=task_id,
        branch_name=branch_name,
        error_summary=None,
    )


def _write_agent_outcome(output_dir: Path, agent_name: str, outcome: dict[str, object]) -> None:
    outcome_dir = output_dir / agent_name / "test_output"
    outcome_dir.mkdir(parents=True)
    (outcome_dir / "testing_agent_outcome.json").write_text(json.dumps(outcome))


def test_generate_spec_html_report_renders_rows_matrix_and_escalations(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    mapper_name = f"spec-mapper-{uuid4().hex}"
    reducer_name = f"spec-reducer-{uuid4().hex}"
    _write_agent_outcome(
        output_dir,
        mapper_name,
        {
            "changes": {"CREATE_TEST": {"status": "SUCCEEDED", "summary_markdown": "Witnessed signin"}},
            "units": [
                {
                    "coordinate": "authentication.fresh-code",
                    "verdict": "FULL",
                    "witnesses": [{"test": "apps/minds/test_auth.py::test_fresh", "partial": None}],
                },
                {
                    "coordinate": "authentication.prefetch",
                    "verdict": "PARTIAL_IMPROVABLE",
                    "witnesses": [{"test": "apps/minds/test_auth.py::test_prefetch", "partial": "no unspent check"}],
                    "blockers": ["needs a Docker daemon"],
                    "spec_problems": [
                        {"problem": "Contradicts the landing flow", "proposed_edit": "Drop the redirect step"}
                    ],
                    "summary_markdown": "See <script>alert(1)</script> notes",
                },
            ],
            "errored": False,
            "tests_passing_before": None,
            "tests_passing_after": True,
            "summary_markdown": "Witnessed the signin file.",
        },
    )
    reducer_outcome_dir = output_dir / reducer_name / "test_output"
    reducer_outcome_dir.mkdir(parents=True)
    (reducer_outcome_dir / "integrator_outcome.json").write_text(
        json.dumps(
            {
                "squashed_branches": ["tmr-specs/run/authentication.signin"],
                "squashed_commit_hash": "abc1234",
                "normalizations": [{"summary_markdown": "Consolidated two signin fixtures"}],
                "escalations": [{"title": "Docker needed", "detail_markdown": "bridge scenarios undrivable"}],
            }
        )
    )
    (reducer_outcome_dir / "matrix.jsonl").write_text(
        json.dumps(
            {
                "coordinate": "authentication.fresh-code",
                "coverage": "full",
                "witnesses": [{"test": "apps/minds/test_auth.py::test_fresh", "partial": None}],
            }
        )
        + "\n"
        + json.dumps({"coordinate": "authentication.prefetch", "coverage": "none", "witnesses": []})
        + "\n"
    )

    report_path = generate_spec_html_report(
        [_mapper_metadata(mapper_name, "authentication/signin.feature", "tmr-specs/run/authentication.signin")],
        output_dir,
        integrator_metadata=AgentMetadata(
            kind=AgentKind.REDUCER,
            agent_name=AgentName(reducer_name),
            task_id=None,
            branch_name="tmr-specs/run/reducer",
            error_summary=None,
        ),
        run_commands=[("Reintegrate", "mngr tmr-specs --name tmr-specs --reintegrate --run-name r1")],
        corpus_violation_paths=(),
    )

    report_html = report_path.read_text()
    assert "authentication.fresh-code" in report_html
    assert "FULL" in report_html
    # Verified column: fresh-code agrees (full), prefetch disagrees (claimed partial, verified none).
    assert "coverage-agree" in report_html
    assert "coverage-disagree" in report_html
    # Spec escalations carry the proposed corpus edit.
    assert "Spec escalations" in report_html
    assert "Drop the redirect step" in report_html
    # Integrator panels render, and no violation banner appears for a clean gate.
    assert "Consolidated two signin fixtures" in report_html
    assert "Docker needed" in report_html
    assert "Corpus violation" not in report_html
    # Raw HTML in agent markdown is escaped, not executed.
    assert "<script>alert(1)</script>" not in report_html


def test_generate_spec_html_report_shows_violation_banner_when_gate_trips(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    mapper_name = f"spec-mapper-{uuid4().hex}"
    _write_agent_outcome(output_dir, mapper_name, {"units": [], "changes": {}})

    report_path = generate_spec_html_report(
        [_mapper_metadata(mapper_name, "authentication/signin.feature", "b1")],
        output_dir,
        corpus_violation_paths=("apps/minds/specs/authentication/signin.feature",),
    )

    report_html = report_path.read_text()
    assert "Corpus violation" in report_html
    assert "apps/minds/specs/authentication/signin.feature" in report_html
