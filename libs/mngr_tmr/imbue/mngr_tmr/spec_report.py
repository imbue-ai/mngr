"""Outcome models and parsers for the spec-anchored TMR recipe.

The mapper outcome schema is two-layered: task-level ``changes`` (the
commit contract the reducer's cherry-pick mechanics key on, mirroring
TMR) plus per-coordinate ``units`` verdicts (the coverage claims the
report keys on). Verdicts and coverage reuse layer 1's vocabulary
(``imbue.mngr_specs``) so mapper claims, matrix snapshots, and reducer
verification stay directly comparable.

The reducer outcome schema is TMR's ``IntegratorResult``, unchanged;
verified coverage comes from the ``matrix.jsonl`` artifact the reducer
ships in its outputs (parsed here by :func:`load_matrix_records`).
"""

import html
import json
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Any
from typing import assert_never

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from loguru import logger
from markdown_it import MarkdownIt
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentName
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_specs.data_types import SpecCoverage
from imbue.mngr_specs.witnesses import spec_coverage_record_value
from imbue.mngr_tmr.report import Change
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.report import EXTRACTED_TEST_OUTPUT_DIR
from imbue.mngr_tmr.report import IntegratorResult
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.report import SECTION_COLORS
from imbue.mngr_tmr.report import SECTION_LABELS
from imbue.mngr_tmr.report import SECTION_ORDER
from imbue.mngr_tmr.report import TestRunInfo
from imbue.mngr_tmr.report import format_changes
from imbue.mngr_tmr.report import load_integrator_outcome
from imbue.mngr_tmr.report import merged_status_html
from imbue.mngr_tmr.report import read_static
from imbue.mngr_tmr.spec_prompts import MATRIX_ARTIFACT_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


class MatrixRecordParseError(MngrError, ValueError):
    """Raised when a matrix JSONL record carries an unknown coverage spelling."""

    ...


class SpecChangeKind(UpperCaseStrEnum):
    """What kind of change a spec mapper attempted.

    There is deliberately no spec-edit kind: the corpus is read-only to the
    whole pipeline, so "the spec looks wrong" is representable only as a
    ``SpecProblem`` escalation, never as a commit.
    """

    CREATE_TEST = auto()
    IMPROVE_TEST = auto()
    FIX_TEST = auto()
    FIX_IMPL = auto()


class SpecUnitVerdict(UpperCaseStrEnum):
    """The end state a mapper claims for one spec unit.

    The four legal states of (coverage, steadiness): partial coverage is the
    only level where steadiness varies, so the pair collapses into one enum
    and invalid combinations are unrepresentable. ``PARTIAL_STEADY`` is the
    honest-partial fixed point: every ``partial=`` note names residue that is
    untestable in kind. ``PARTIAL_IMPROVABLE`` is honest unfinished work.
    """

    NONE = auto()
    PARTIAL_IMPROVABLE = auto()
    PARTIAL_STEADY = auto()
    FULL = auto()


@pure
def coverage_of_verdict(verdict: SpecUnitVerdict) -> SpecCoverage:
    """Project a verdict onto matrix's coverage vocabulary for claimed-vs-verified comparison."""
    match verdict:
        case SpecUnitVerdict.NONE:
            return SpecCoverage.NONE
        case SpecUnitVerdict.PARTIAL_IMPROVABLE | SpecUnitVerdict.PARTIAL_STEADY:
            return SpecCoverage.PARTIAL
        case SpecUnitVerdict.FULL:
            return SpecCoverage.FULL
        case _ as unreachable:
            assert_never(unreachable)


class WitnessClaim(FrozenModel):
    """One witnessing test of a spec unit, in the same shape as a matrix record's witness objects."""

    test: str = Field(description="The pytest node id of the witnessing test")
    partial: str | None = Field(description="The marker's partial= note (what the test does not cover), or None")


class SpecProblem(FrozenModel):
    """A spec-seems-wrong escalation: the only channel by which the fleet can propose a corpus change."""

    problem: str = Field(description="What looks wrong about the spec unit")
    proposed_edit: str = Field(description="The corpus edit the mapper would propose (never applied by the fleet)")


class UnitVerdictRecord(FrozenModel):
    """A mapper's per-coordinate claim: end state, witnesses, and any blockers or spec problems."""

    coordinate: str = Field(description="The spec unit's coordinate")
    verdict: SpecUnitVerdict = Field(description="The claimed end state for this unit")
    witnesses: tuple[WitnessClaim, ...] = Field(default=(), description="Witnessing tests touched or created")
    blockers: tuple[str, ...] = Field(default=(), description="Environment/infra blockers hit for this unit")
    spec_problems: tuple[SpecProblem, ...] = Field(default=(), description="Spec-seems-wrong escalations")
    summary_markdown: str = Field(default="", description="Markdown summary of this unit's outcome")


class SpecTaskResult(FrozenModel):
    """Result reported by a spec mapper for its feature-file task, read from its outcome JSON."""

    changes: dict[SpecChangeKind, Change] = Field(
        default_factory=dict, description="Changes the mapper attempted, keyed by kind"
    )
    units: tuple[UnitVerdictRecord, ...] = Field(
        default=(), description="Per-coordinate verdicts for every unit in the task's scope"
    )
    errored: bool = Field(
        default=False, description="Whether an infrastructure error prevented the mapper from working"
    )
    tests_passing_before: bool | None = Field(
        default=None,
        description="Were the pre-existing witnessing tests passing before changes? None when none existed.",
    )
    tests_passing_after: bool | None = Field(
        default=None, description="Are the touched witnessing tests passing after all changes? None if unknown."
    )
    summary_markdown: str = Field(default="", description="Overall markdown summary of what happened")
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="Test runs performed, in order")


class MatrixCoverageRecord(FrozenModel):
    """One unit's verified coverage, parsed from the reducer's ``matrix.jsonl`` artifact."""

    coordinate: str = Field(description="The spec unit's coordinate")
    coverage: SpecCoverage = Field(description="Coverage measured by `mngr specs matrix` on the integrated tree")
    witnesses: tuple[WitnessClaim, ...] = Field(default=(), description="The witnessing tests matrix found")


@pure
def _parse_coverage_record_value(value: str) -> SpecCoverage:
    """Parse the lowercase coverage spelling used by matrix JSONL records."""
    if value == "full":
        return SpecCoverage.FULL
    elif value == "partial":
        return SpecCoverage.PARTIAL
    elif value == "none":
        return SpecCoverage.NONE
    else:
        raise MatrixRecordParseError(f"Unknown coverage record value: {value!r}")


@pure
def _parse_witness_claims(raw_witnesses: Any) -> tuple[WitnessClaim, ...]:
    return tuple(
        WitnessClaim(test=entry.get("test", ""), partial=entry.get("partial")) for entry in raw_witnesses or ()
    )


@pure
def _parse_unit_verdict_record(entry: dict[str, Any]) -> UnitVerdictRecord:
    return UnitVerdictRecord(
        coordinate=entry.get("coordinate", ""),
        verdict=SpecUnitVerdict(entry["verdict"]),
        witnesses=_parse_witness_claims(entry.get("witnesses")),
        blockers=tuple(entry.get("blockers", ())),
        spec_problems=tuple(
            SpecProblem(problem=problem.get("problem", ""), proposed_edit=problem.get("proposed_edit", ""))
            for problem in entry.get("spec_problems", ())
        ),
        summary_markdown=entry.get("summary_markdown", ""),
    )


@pure
def parse_spec_outcome_json(raw: str) -> SpecTaskResult:
    """Parse a spec mapper's outcome JSON string into a SpecTaskResult.

    Lenient about unknown extra keys (agent output), strict about the enum
    vocabularies. Raises json.JSONDecodeError, KeyError, or ValueError on
    invalid data.
    """
    data = json.loads(raw)
    changes = {
        SpecChangeKind(kind_str): Change(
            status=ChangeStatus(entry["status"]),
            summary_markdown=entry.get("summary_markdown", ""),
        )
        for kind_str, entry in data.get("changes", {}).items()
    }
    units = tuple(_parse_unit_verdict_record(entry) for entry in data.get("units", ()))
    test_runs = tuple(
        TestRunInfo(
            run_name=run_entry.get("run_name", ""),
            description_markdown=run_entry.get("description_markdown", ""),
        )
        for run_entry in data.get("test_runs", ())
    )
    return SpecTaskResult(
        changes=changes,
        units=units,
        errored=data.get("errored", False),
        tests_passing_before=data.get("tests_passing_before"),
        tests_passing_after=data.get("tests_passing_after"),
        summary_markdown=data.get("summary_markdown", ""),
        test_runs=test_runs,
    )


def load_matrix_records(matrix_path: Path) -> dict[str, MatrixCoverageRecord] | None:
    """Load the reducer's matrix artifact, indexed by coordinate; None when absent.

    Malformed lines are skipped with a warning (the artifact is agent-produced
    subprocess output, not user configuration).
    """
    try:
        raw_lines = matrix_path.read_text().splitlines()
    except (FileNotFoundError, OSError):
        return None
    record_by_coordinate: dict[str, MatrixCoverageRecord] = {}
    for raw_line in raw_lines:
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        try:
            entry = json.loads(stripped_line)
            record = MatrixCoverageRecord(
                coordinate=entry["coordinate"],
                coverage=_parse_coverage_record_value(entry.get("coverage", "none")),
                witnesses=_parse_witness_claims(entry.get("witnesses")),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Skipping malformed matrix record line in {}: {}", matrix_path, exc)
            continue
        record_by_coordinate[record.coordinate] = record
    return record_by_coordinate


class SpecReportRow(FrozenModel):
    """Renderable state of one feature-file task: metadata joined with its parsed outcome, if any."""

    task_id: str = Field(description="The task id (root-relative feature-file path)")
    agent_name: AgentName = Field(description="Name of the mapper agent for this task")
    changes: dict[SpecChangeKind, Change] = Field(default_factory=dict, description="Changes, keyed by kind")
    units: tuple[UnitVerdictRecord, ...] = Field(default=(), description="Per-coordinate verdicts")
    errored: bool = Field(default=False, description="Whether an error prevented the agent from working")
    tests_passing_after: bool | None = Field(default=None, description="Are touched witnesses passing?")
    summary_markdown: str = Field(default="", description="Markdown summary from the agent")
    branch_name: str | None = Field(default=None, description="Git branch name for the agent's changes, or None")


# Outcome JSON for a given agent is immutable once present; cache like the TMR report does.
_SPEC_OUTCOME_CACHE: dict[AgentName, SpecTaskResult] = {}

_NON_IMPL_SPEC_CHANGE_KINDS = frozenset(
    {SpecChangeKind.CREATE_TEST, SpecChangeKind.IMPROVE_TEST, SpecChangeKind.FIX_TEST}
)

_STEADY_VERDICTS = frozenset({SpecUnitVerdict.FULL, SpecUnitVerdict.PARTIAL_STEADY})

_jinja_env = Environment(
    loader=PackageLoader("imbue.mngr_tmr", "spec_report_assets"),
    autoescape=select_autoescape(["html", "j2"]),
)

# The "js-default" preset disables raw HTML, so agent-authored markdown
# cannot inject markup into the report (summaries render through |safe).
_strict_markdown = MarkdownIt("js-default")


def render_markdown_without_raw_html(text: str) -> str:
    return _strict_markdown.render(text)


def _load_spec_outcome(agent_name: AgentName, output_dir: Path) -> SpecTaskResult | None:
    """Read and cache a spec mapper's outcome from its extracted output dir."""
    cached = _SPEC_OUTCOME_CACHE.get(agent_name)
    if cached is not None:
        return cached
    path = output_dir / str(agent_name) / EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        outcome = parse_spec_outcome_json(raw)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to parse spec outcome for agent '{}': {}", agent_name, exc)
        return None
    _SPEC_OUTCOME_CACHE[agent_name] = outcome
    return outcome


@pure
def _spec_row_from_metadata(meta: AgentMetadata, outcome: SpecTaskResult | None) -> SpecReportRow:
    if meta.error_summary is not None:
        return SpecReportRow(
            task_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            errored=True,
            summary_markdown=meta.error_summary,
            branch_name=meta.branch_name,
        )
    if outcome is None:
        return SpecReportRow(
            task_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            summary_markdown="Agent is still running...",
            branch_name=meta.branch_name,
        )
    return SpecReportRow(
        task_id=meta.task_id or str(meta.agent_name),
        agent_name=meta.agent_name,
        changes=outcome.changes,
        units=outcome.units,
        errored=outcome.errored,
        tests_passing_after=outcome.tests_passing_after,
        summary_markdown=outcome.summary_markdown,
        branch_name=meta.branch_name,
    )


def _build_spec_rows(agents: Sequence[AgentMetadata], output_dir: Path) -> list[SpecReportRow]:
    rows: list[SpecReportRow] = []
    for meta in agents:
        if meta.kind is not AgentKind.MAPPER:
            continue
        outcome = _load_spec_outcome(meta.agent_name, output_dir) if meta.error_summary is None else None
        rows.append(_spec_row_from_metadata(meta, outcome))
    return rows


@pure
def spec_report_section_of(row: SpecReportRow) -> ReportSection:
    """Derive a report section from a spec row, mirroring the TMR derivation.

    CLEAN_PASS means the task converged with no changes needed: every unit is
    at its fixed point (FULL or PARTIAL_STEADY) and nothing was touched.
    """
    if row.errored:
        return ReportSection.FAILED
    if not row.changes and not row.units:
        return ReportSection.RUNNING
    if row.changes and all(change.status == ChangeStatus.BLOCKED for change in row.changes.values()):
        return ReportSection.BLOCKED
    if any(kind in _NON_IMPL_SPEC_CHANGE_KINDS for kind in row.changes):
        return ReportSection.NON_IMPL_FIXES
    if SpecChangeKind.FIX_IMPL in row.changes:
        return ReportSection.IMPL_FIXES
    if not row.changes and row.units and all(unit.verdict in _STEADY_VERDICTS for unit in row.units):
        return ReportSection.CLEAN_PASS
    return ReportSection.BLOCKED


@pure
def _format_task_id(task_id: str) -> str:
    """HTML-escape the task id, then add a soft wrap hint after each path separator."""
    return html.escape(task_id).replace("/", "/<wbr>")


@pure
def _build_spec_row_view(row: SpecReportRow, integrator: IntegratorResult | None) -> dict[str, object]:
    return {
        "task_id_html": _format_task_id(row.task_id),
        "agent_name": str(row.agent_name),
        "branch_name": row.branch_name,
        "changes_html": format_changes(row.changes) if row.changes else "-",
        "merged_html": merged_status_html(row.branch_name, integrator),
        "summary_html": render_markdown_without_raw_html(row.summary_markdown) if row.summary_markdown else "",
    }


def _build_spec_section_views(
    rows: list[SpecReportRow],
    integrator: IntegratorResult | None,
) -> list[dict[str, object]]:
    grouped: dict[ReportSection, list[SpecReportRow]] = {}
    for row in rows:
        grouped.setdefault(spec_report_section_of(row), []).append(row)

    sections: list[dict[str, object]] = []
    for section in SECTION_ORDER:
        group = grouped.get(section)
        if not group:
            continue
        if section == ReportSection.IMPL_FIXES and integrator is not None and integrator.impl_priority:
            priority_order = {branch: i for i, branch in enumerate(integrator.impl_priority)}
            group = sorted(group, key=lambda r: priority_order.get(r.branch_name or "", len(priority_order)))
        col_count = 5 if section not in (ReportSection.RUNNING, ReportSection.CLEAN_PASS) else (
            2 if section == ReportSection.RUNNING else 3
        )
        sections.append(
            {
                "kind": section.value,
                "label": SECTION_LABELS[section],
                "color": SECTION_COLORS[section],
                "anchor": f"sec-{section.value}",
                "rows": [_build_spec_row_view(row, integrator) for row in group],
                "count": len(group),
                "col_count": col_count,
            }
        )
    return sections


@pure
def _witness_views(witnesses: tuple[WitnessClaim, ...]) -> list[dict[str, object]]:
    return [{"test": claim.test, "partial": claim.partial} for claim in witnesses]


def _build_coverage_rows(
    rows: list[SpecReportRow],
    verified_by_coordinate: dict[str, MatrixCoverageRecord] | None,
) -> list[dict[str, object]]:
    """One coverage-matrix row per reported unit, in task order.

    "Claimed" is the mapper's verdict; "verified" is what `mngr specs matrix`
    measured on the integrated tree (absent until the reducer ships the
    artifact). A row is agreeing when the verdict's coverage projection equals
    the verified coverage.
    """
    coverage_rows: list[dict[str, object]] = []
    for row in rows:
        for unit in row.units:
            verified_record = (verified_by_coordinate or {}).get(unit.coordinate)
            verified_value = (
                spec_coverage_record_value(verified_record.coverage) if verified_record is not None else None
            )
            is_agreeing = (
                None
                if verified_record is None
                else verified_record.coverage == coverage_of_verdict(unit.verdict)
            )
            witnesses = verified_record.witnesses if verified_record is not None else unit.witnesses
            coverage_rows.append(
                {
                    "coordinate": unit.coordinate,
                    "task_id_html": _format_task_id(row.task_id),
                    "claimed": unit.verdict.value,
                    "verified": verified_value,
                    "is_agreeing": is_agreeing,
                    "witnesses": _witness_views(witnesses),
                    "blockers": list(unit.blockers),
                    "has_spec_problems": bool(unit.spec_problems),
                    "summary_html": render_markdown_without_raw_html(unit.summary_markdown) if unit.summary_markdown else "",
                }
            )
    return coverage_rows


def _build_spec_escalation_views(rows: list[SpecReportRow]) -> list[dict[str, object]]:
    """Aggregate every unit's spec problems: the only channel proposing corpus edits."""
    views: list[dict[str, object]] = []
    for row in rows:
        for unit in row.units:
            for problem in unit.spec_problems:
                views.append(
                    {
                        "coordinate": unit.coordinate,
                        "problem_html": render_markdown_without_raw_html(problem.problem),
                        "proposed_edit_html": render_markdown_without_raw_html(problem.proposed_edit),
                    }
                )
    return views


def generate_spec_html_report(
    agents: Sequence[AgentMetadata],
    output_dir: Path,
    *,
    integrator_metadata: AgentMetadata | None = None,
    run_commands: list[tuple[str, str]] | None = None,
    corpus_violation_paths: tuple[str, ...] | None = None,
) -> Path:
    """Generate the spec-TMR HTML report and return its path (``output_dir/index.html``).

    ``corpus_violation_paths`` is the egress gate's finding for the applied
    reducer branch: None when the gate has not run (no branch yet), empty when
    clean, non-empty when the branch touches the corpus (in which case the
    branch event was withheld and the report shows a violation banner).
    """
    rows = _build_spec_rows(agents, output_dir)
    integrator = load_integrator_outcome(integrator_metadata, output_dir) if integrator_metadata is not None else None
    verified_by_coordinate = (
        load_matrix_records(
            output_dir / str(integrator_metadata.agent_name) / EXTRACTED_TEST_OUTPUT_DIR / MATRIX_ARTIFACT_FILENAME
        )
        if integrator_metadata is not None
        else None
    )

    sections = _build_spec_section_views(rows, integrator)
    toc_links = [
        {"anchor": s["anchor"], "color": s["color"], "label": s["label"], "count": s["count"]} for s in sections
    ]
    coverage_rows = _build_coverage_rows(rows, verified_by_coordinate)
    spec_escalation_views = _build_spec_escalation_views(rows)
    escalation_views = (
        [{"title": e.title, "detail_html": render_markdown_without_raw_html(e.detail_markdown)} for e in integrator.escalations]
        if integrator is not None
        else []
    )
    normalization_views = (
        [{"summary_html": render_markdown_without_raw_html(n.summary_markdown)} for n in integrator.normalizations]
        if integrator is not None
        else []
    )

    template = _jinja_env.get_template("spec_report.html.j2")
    report_html = template.render(
        rows=rows,
        sections=sections,
        toc_links=toc_links,
        integrator=integrator,
        coverage_rows=coverage_rows,
        spec_escalation_views=spec_escalation_views,
        escalation_views=escalation_views,
        normalization_views=normalization_views,
        corpus_violation_paths=corpus_violation_paths,
        run_commands=run_commands or [],
        css=read_static("report.css"),
    )
    output_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("Spec-TMR HTML report written to {}", output_path)
    return output_path
