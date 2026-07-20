"""Enriched export of the behavioral-spec corpus.

:func:`scan_corpus` extracts the bare units; this module resolves everything
a consumer (a test-writing agent, a planner) needs around each unit: the
unit's effective steps (Background folded in), Examples rows for Scenario
Outlines, Feature/Rule descriptions, the prose context (folder overviews plus
the file's sidecar), and the invariants that apply to the unit, resolved
root -> folder -> file per the language's scoping rules (see the
minds-behavioral-specs skill).

The single entry point is :func:`export_corpus`; ``minds specs export``
renders the result as JSONL, and ``minds specs plan --for-tmr`` wraps each
record in a task packet.
"""

import textwrap
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.core.behavioral_specs._gherkin import GherkinDocument
from imbue.minds.core.behavioral_specs._gherkin import GherkinExamples
from imbue.minds.core.behavioral_specs._gherkin import GherkinStep
from imbue.minds.core.behavioral_specs._gherkin import parse_feature_file
from imbue.minds.core.behavioral_specs.corpus import scan_corpus
from imbue.minds.core.behavioral_specs.corpus import spec_unit_kind_record_value
from imbue.minds.core.behavioral_specs.data_types import ApplicableRule
from imbue.minds.core.behavioral_specs.data_types import ApplicableRuleScope
from imbue.minds.core.behavioral_specs.data_types import CorpusExport
from imbue.minds.core.behavioral_specs.data_types import ExportedEnclosingRule
from imbue.minds.core.behavioral_specs.data_types import ExportedSpecUnit
from imbue.minds.core.behavioral_specs.data_types import SpecExamplesTable
from imbue.minds.core.behavioral_specs.data_types import SpecProseFile
from imbue.minds.core.behavioral_specs.data_types import SpecProseFileKind
from imbue.minds.core.behavioral_specs.data_types import SpecStep
from imbue.minds.core.behavioral_specs.data_types import SpecUnit
from imbue.minds.core.behavioral_specs.data_types import SpecUnitKind
from imbue.minds.core.behavioral_specs.data_types import SpecViolation

# Version of the `minds specs export` JSONL record shape.
EXPORT_RECORD_SCHEMA_VERSION: Final[int] = 1

# Version of the `minds specs plan --for-tmr` task-packet shape (consumed by
# the TMR task-file recipe, e.g. `mngr tmr-tasks`).
TMR_TASK_PACKET_SCHEMA_VERSION: Final[int] = 1

# The reserved filename whose Rules are folder/corpus-scoped invariants.
_INVARIANTS_FILENAME: Final[str] = "invariants.feature"

# The reserved basename of a folder's prose overview file.
_OVERVIEW_FILENAME: Final[str] = "overview.md"


class _UnitEnrichment(FrozenModel):
    """AST detail for one unit beyond what SpecUnit carries."""

    description: str = Field(description="The unit's description prose (dedented)")
    examples: tuple[SpecExamplesTable, ...] = Field(description="Examples tables (Scenario Outline only)")


class _FileEnrichment(FrozenModel):
    """Per-file AST detail the export folds into each of the file's units."""

    feature_name: str = Field(description="Name of the file's Feature")
    feature_description: str = Field(description="The Feature's description prose (dedented)")
    feature_background_steps: tuple[SpecStep, ...] = Field(
        description="Steps of the file's feature-level Background (empty when none)"
    )
    unit_enrichment_by_line: dict[int, _UnitEnrichment] = Field(
        description="Enrichment for every unit of the file, keyed by declaration line"
    )
    rule_background_steps_by_line: dict[int, tuple[SpecStep, ...]] = Field(
        description="Rule-level Background steps, keyed by the enclosing Rule's declaration line"
    )
    enclosing_rule_line_by_child_line: dict[int, int] = Field(
        description="Maps a Rule child's declaration line to its enclosing Rule's declaration line"
    )


@pure
def _dedent_description(description: str) -> str:
    return textwrap.dedent(description).strip()


@pure
def _steps_of(gherkin_steps: tuple[GherkinStep, ...]) -> tuple[SpecStep, ...]:
    return tuple(SpecStep(keyword=step.keyword.strip(), text=step.text) for step in gherkin_steps)


@pure
def _examples_table_of(examples: GherkinExamples) -> SpecExamplesTable:
    header = () if examples.table_header is None else tuple(cell.value for cell in examples.table_header.cells)
    rows = tuple(tuple(cell.value for cell in row.cells) for row in examples.table_body)
    return SpecExamplesTable(line=examples.location.line, header=header, rows=rows)


def _enrichment_from_document(document: GherkinDocument, file: Path) -> _FileEnrichment:
    """Pull the export context out of one parsed document.

    Callers only parse files that produced units during the corpus scan, so
    the document always has a Feature here.
    """
    if document.feature is None:
        raise SwitchError(f"unit-bearing file {file} re-parsed with no Feature declaration")
    feature = document.feature
    feature_background_steps: tuple[SpecStep, ...] = ()
    unit_enrichment_by_line: dict[int, _UnitEnrichment] = {}
    rule_background_steps_by_line: dict[int, tuple[SpecStep, ...]] = {}
    enclosing_rule_line_by_child_line: dict[int, int] = {}
    for child in feature.children:
        if child.background is not None:
            feature_background_steps = _steps_of(child.background.steps)
        elif child.scenario is not None:
            scenario = child.scenario
            unit_enrichment_by_line[scenario.location.line] = _UnitEnrichment(
                description=_dedent_description(scenario.description),
                examples=tuple(_examples_table_of(examples) for examples in scenario.examples),
            )
        elif child.rule is not None:
            rule = child.rule
            unit_enrichment_by_line[rule.location.line] = _UnitEnrichment(
                description=_dedent_description(rule.description),
                examples=(),
            )
            for rule_child in rule.children:
                if rule_child.background is not None:
                    rule_background_steps_by_line[rule.location.line] = _steps_of(rule_child.background.steps)
                elif rule_child.scenario is not None:
                    child_scenario = rule_child.scenario
                    unit_enrichment_by_line[child_scenario.location.line] = _UnitEnrichment(
                        description=_dedent_description(child_scenario.description),
                        examples=tuple(_examples_table_of(examples) for examples in child_scenario.examples),
                    )
                    enclosing_rule_line_by_child_line[child_scenario.location.line] = rule.location.line
                else:
                    raise SwitchError(f"Rule child envelope with no known node type in {file}")
        else:
            raise SwitchError(f"Feature child envelope with no known node type in {file}")
    return _FileEnrichment(
        feature_name=feature.name,
        feature_description=_dedent_description(feature.description),
        feature_background_steps=feature_background_steps,
        unit_enrichment_by_line=unit_enrichment_by_line,
        rule_background_steps_by_line=rule_background_steps_by_line,
        enclosing_rule_line_by_child_line=enclosing_rule_line_by_child_line,
    )


def _enrich_unit_files(unit_files: tuple[Path, ...]) -> dict[Path, _FileEnrichment]:
    """Re-parse every unit-bearing file and build its enrichment.

    Parse failures cannot recur here (the corpus scan already parsed these
    exact files successfully), so a failure is a bug, not data.
    """
    enrichment_by_file: dict[Path, _FileEnrichment] = {}
    for unit_file in unit_files:
        discarded_violations: list[SpecViolation] = []
        document = parse_feature_file(unit_file, unit_file.read_text(encoding="utf-8"), discarded_violations)
        if document is None:
            raise SwitchError(f"unit-bearing file {unit_file} failed to re-parse during export")
        enrichment_by_file[unit_file] = _enrichment_from_document(document, unit_file)
    return enrichment_by_file


def _collect_prose(unit_file: Path, corpus_root: Path) -> tuple[SpecProseFile, ...]:
    """Collect the prose context for a unit: overviews root -> folder, then the sidecar."""
    folder_parts = unit_file.parent.relative_to(corpus_root).parts
    prose: list[SpecProseFile] = []
    for depth in range(len(folder_parts) + 1):
        overview_path = corpus_root.joinpath(*folder_parts[:depth]) / _OVERVIEW_FILENAME
        if overview_path.is_file():
            prose.append(
                SpecProseFile(
                    path=overview_path,
                    kind=SpecProseFileKind.OVERVIEW,
                    content=overview_path.read_text(encoding="utf-8"),
                )
            )
    sidecar_path = unit_file.with_suffix(".md")
    if sidecar_path.is_file():
        prose.append(
            SpecProseFile(
                path=sidecar_path,
                kind=SpecProseFileKind.SIDECAR,
                content=sidecar_path.read_text(encoding="utf-8"),
            )
        )
    return tuple(prose)


@pure
def _applicable_rule_from_unit(
    rule_unit: SpecUnit,
    scope: ApplicableRuleScope,
    enrichment_by_file: dict[Path, _FileEnrichment],
) -> ApplicableRule:
    description = enrichment_by_file[rule_unit.file].unit_enrichment_by_line[rule_unit.line].description
    return ApplicableRule(
        coordinate=rule_unit.coordinate,
        name=rule_unit.name,
        description=description,
        file=rule_unit.file,
        scope=scope,
    )


@pure
def resolve_applicable_rules(
    unit: SpecUnit,
    corpus_root: Path,
    rule_units_by_file: dict[Path, tuple[SpecUnit, ...]],
    enrichment_by_file: dict[Path, _FileEnrichment],
) -> tuple[ApplicableRule, ...]:
    """Resolve the invariants applying to a unit, ordered root -> folder -> file.

    A Rule in a folder's invariants.feature binds that folder and everything
    below it (the corpus root's binds the whole corpus); a Rule in an ordinary
    feature file binds that file's units. A unit never lists itself.
    """
    folder_parts = unit.file.parent.relative_to(corpus_root).parts
    applicable: list[ApplicableRule] = []
    for depth in range(len(folder_parts) + 1):
        invariants_file = corpus_root.joinpath(*folder_parts[:depth]) / _INVARIANTS_FILENAME
        scope = ApplicableRuleScope.CORPUS if depth == 0 else ApplicableRuleScope.FOLDER
        for rule_unit in rule_units_by_file.get(invariants_file, ()):
            if rule_unit.coordinate != unit.coordinate:
                applicable.append(_applicable_rule_from_unit(rule_unit, scope, enrichment_by_file))
    if unit.file.name != _INVARIANTS_FILENAME:
        for rule_unit in rule_units_by_file.get(unit.file, ()):
            if rule_unit.coordinate != unit.coordinate:
                applicable.append(_applicable_rule_from_unit(rule_unit, ApplicableRuleScope.FILE, enrichment_by_file))
    return tuple(applicable)


def _export_unit(
    unit: SpecUnit,
    corpus_root: Path,
    enrichment_by_file: dict[Path, _FileEnrichment],
    rule_units_by_file: dict[Path, tuple[SpecUnit, ...]],
    unit_by_coordinate: dict[str, SpecUnit],
) -> ExportedSpecUnit:
    enrichment = enrichment_by_file[unit.file]
    unit_enrichment = enrichment.unit_enrichment_by_line[unit.line]
    enclosing_rule_line = enrichment.enclosing_rule_line_by_child_line.get(unit.line)
    rule_background_steps = (
        enrichment.rule_background_steps_by_line.get(enclosing_rule_line, ())
        if enclosing_rule_line is not None
        else ()
    )
    effective_steps = enrichment.feature_background_steps + rule_background_steps + unit.steps
    enclosing_rule: ExportedEnclosingRule | None = None
    if unit.parent is not None and enclosing_rule_line is not None:
        parent_unit = unit_by_coordinate[unit.parent]
        enclosing_rule = ExportedEnclosingRule(
            coordinate=parent_unit.coordinate,
            name=parent_unit.name,
            description=enrichment.unit_enrichment_by_line[enclosing_rule_line].description,
        )
    return ExportedSpecUnit(
        coordinate=unit.coordinate,
        kind=unit.kind,
        name=unit.name,
        file=unit.file,
        line=unit.line,
        tags=unit.tags,
        parent=unit.parent,
        description=unit_enrichment.description,
        raw_steps=unit.steps,
        effective_steps=effective_steps,
        examples=unit_enrichment.examples,
        feature_name=enrichment.feature_name,
        feature_description=enrichment.feature_description,
        rule=enclosing_rule,
        prose=_collect_prose(unit.file, corpus_root),
        applicable_rules=resolve_applicable_rules(unit, corpus_root, rule_units_by_file, enrichment_by_file),
    )


def export_corpus(corpus_root: Path) -> CorpusExport:
    """Scan a behavioral-spec corpus and resolve the full authoring context of each unit.

    Raises SpecCorpusRootNotFoundError if the root is not an existing directory.
    """
    scan = scan_corpus(corpus_root)
    unit_files = tuple(sorted({unit.file for unit in scan.units}))
    enrichment_by_file = _enrich_unit_files(unit_files)
    rule_units_by_file: dict[Path, list[SpecUnit]] = {}
    for unit in scan.units:
        if unit.kind == SpecUnitKind.RULE:
            rule_units_by_file.setdefault(unit.file, []).append(unit)
    frozen_rule_units_by_file = {file: tuple(units) for file, units in rule_units_by_file.items()}
    unit_by_coordinate = {unit.coordinate: unit for unit in scan.units}
    exported_units = tuple(
        _export_unit(unit, corpus_root, enrichment_by_file, frozen_rule_units_by_file, unit_by_coordinate)
        for unit in scan.units
    )
    return CorpusExport(
        units=exported_units,
        violations=scan.violations,
        feature_file_count=scan.feature_file_count,
    )


@pure
def exported_unit_to_record(unit: ExportedSpecUnit) -> dict[str, Any]:
    """Render an exported unit as the JSON object ``minds specs export`` emits (sans schema_version)."""
    return {
        "coordinate": unit.coordinate,
        "kind": spec_unit_kind_record_value(unit.kind),
        "name": unit.name,
        "file": str(unit.file),
        "line": unit.line,
        "tags": list(unit.tags),
        "parent": unit.parent,
        "description": unit.description,
        "raw_steps": [{"keyword": step.keyword, "text": step.text} for step in unit.raw_steps],
        "effective_steps": [{"keyword": step.keyword, "text": step.text} for step in unit.effective_steps],
        "examples": [
            {"line": examples.line, "header": list(examples.header), "rows": [list(row) for row in examples.rows]}
            for examples in unit.examples
        ],
        "feature": {"name": unit.feature_name, "description": unit.feature_description},
        "rule": (
            None
            if unit.rule is None
            else {
                "coordinate": unit.rule.coordinate,
                "name": unit.rule.name,
                "description": unit.rule.description,
            }
        ),
        "prose": [
            {"path": str(prose.path), "kind": prose.kind.value.lower(), "content": prose.content}
            for prose in unit.prose
        ],
        "applicable_rules": [
            {
                "coordinate": rule.coordinate,
                "name": rule.name,
                "description": rule.description,
                "file": str(rule.file),
                "scope": rule.scope.value.lower(),
            }
            for rule in unit.applicable_rules
        ],
    }


@pure
def exported_unit_to_tmr_task_packet(unit: ExportedSpecUnit) -> dict[str, Any]:
    """Render an exported unit as a TMR task packet (consumed by `mngr tmr-tasks`)."""
    return {
        "schema_version": TMR_TASK_PACKET_SCHEMA_VERSION,
        "id": unit.coordinate,
        "display_id": unit.coordinate.replace(".", "-"),
        "kind": spec_unit_kind_record_value(unit.kind),
        "context": exported_unit_to_record(unit),
    }
