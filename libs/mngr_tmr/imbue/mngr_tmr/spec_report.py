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

import json
from enum import auto
from pathlib import Path
from typing import Any
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_specs.data_types import SpecCoverage
from imbue.mngr_tmr.report import Change
from imbue.mngr_tmr.report import ChangeStatus
from imbue.mngr_tmr.report import TestRunInfo


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
        raise ValueError(f"Unknown coverage record value: {value!r}")


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
