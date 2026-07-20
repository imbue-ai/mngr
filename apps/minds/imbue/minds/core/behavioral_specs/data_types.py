from enum import auto
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class SpecUnitKind(UpperCaseStrEnum):
    """The structural kind of an authored behavioral-spec unit."""

    SCENARIO = auto()
    SCENARIO_OUTLINE = auto()
    RULE = auto()


class SpecStep(FrozenModel):
    """One step of a spec unit: keyword plus text (table/docstring arguments are not carried)."""

    keyword: str = Field(description="The step keyword as written, without trailing space (e.g. 'Given')")
    text: str = Field(description="The step text after the keyword")


class SpecUnit(FrozenModel):
    """One authored unit of the behavioral-spec corpus: a Scenario, Scenario Outline, or Rule."""

    coordinate: str = Field(
        description="Folder names from the corpus root joined with dots, then the raw identity tag"
    )
    kind: SpecUnitKind = Field(description="Structural kind of the unit")
    name: str = Field(description="The unit's name as written after its keyword")
    file: Path = Field(description="Path to the unit's .feature file, rooted at the corpus root as given to the scan")
    line: int = Field(description="1-based line of the unit's declaration header")
    tags: tuple[str, ...] = Field(
        description="All tags on the unit in order, without the '@' sigil; the first is the identity"
    )
    steps: tuple[SpecStep, ...] = Field(
        description="The unit's own steps in order (empty for a Rule; Background steps are not folded in)"
    )
    parent: str | None = Field(description="Coordinate of the enclosing Rule for units nested under one, else None")


class SpecViolation(FrozenModel):
    """One behavioral-spec language violation found while scanning a corpus."""

    file: Path = Field(description="File (or folder) the violation applies to")
    line: int | None = Field(description="1-based line of the violation where available")
    message: str = Field(description="Human-readable description of the violation")
    is_unit_omitted: bool = Field(
        description="True when the problem prevented one or more units from being represented as records"
    )


class CorpusScan(FrozenModel):
    """The result of scanning a behavioral-spec corpus: extracted units plus language violations."""

    units: tuple[SpecUnit, ...] = Field(description="All representable units, in file order then document order")
    violations: tuple[SpecViolation, ...] = Field(description="All language violations found, in deterministic order")
    feature_file_count: int = Field(description="Count of .feature files seen during the scan")


class SpecExamplesTable(FrozenModel):
    """One Examples block of a Scenario Outline, with its table rendered as plain rows of cells."""

    line: int = Field(description="1-based line of the Examples header")
    header: tuple[str, ...] = Field(description="Header cell values in order")
    rows: tuple[tuple[str, ...], ...] = Field(description="Body rows, each a tuple of cell values in order")


class SpecProseFileKind(UpperCaseStrEnum):
    """Which kind of prose file a piece of corpus context came from."""

    OVERVIEW = auto()
    SIDECAR = auto()


class SpecProseFile(FrozenModel):
    """One prose file relevant to a spec unit: a folder overview.md or a file's sidecar."""

    path: Path = Field(description="Path to the prose file, rooted like the unit's file")
    kind: SpecProseFileKind = Field(description="Whether this is a folder overview or a file sidecar")
    content: str = Field(description="Full text content of the prose file")


class ApplicableRuleScope(UpperCaseStrEnum):
    """Where an applicable invariant comes from, per the language's scoping rules."""

    CORPUS = auto()
    FOLDER = auto()
    FILE = auto()


class ApplicableRule(FrozenModel):
    """One invariant (Rule unit) that applies to a spec unit, with its scope resolution."""

    coordinate: str = Field(description="The rule unit's coordinate")
    name: str = Field(description="The rule's name as written after its keyword")
    description: str = Field(description="The rule's description prose (dedented)")
    file: Path = Field(description="Path to the file declaring the rule, rooted like the unit's file")
    scope: ApplicableRuleScope = Field(
        description="CORPUS from the root invariants.feature, FOLDER from a deeper invariants.feature, "
        "FILE from a rule in the unit's own ordinary feature file"
    )


class ExportedEnclosingRule(FrozenModel):
    """Summary of the Rule a unit is nested under, when it is a Rule child."""

    coordinate: str = Field(description="The enclosing rule's coordinate")
    name: str = Field(description="The enclosing rule's name")
    description: str = Field(description="The enclosing rule's description prose (dedented)")


class ExportedSpecUnit(FrozenModel):
    """One spec unit with its full authoring context resolved: the enriched export record."""

    coordinate: str = Field(description="The unit's coordinate")
    kind: SpecUnitKind = Field(description="Structural kind of the unit")
    name: str = Field(description="The unit's name as written after its keyword")
    file: Path = Field(description="Path to the unit's .feature file, rooted at the corpus root as given")
    line: int = Field(description="1-based line of the unit's declaration header")
    tags: tuple[str, ...] = Field(description="All tags on the unit in order, without the '@' sigil")
    parent: str | None = Field(description="Coordinate of the enclosing Rule for units nested under one, else None")
    description: str = Field(description="The unit's own description prose (dedented)")
    raw_steps: tuple[SpecStep, ...] = Field(description="The unit's own steps in order")
    effective_steps: tuple[SpecStep, ...] = Field(
        description="raw_steps with Background steps folded in (feature-level, then rule-level for Rule children)"
    )
    examples: tuple[SpecExamplesTable, ...] = Field(
        description="Examples tables of a Scenario Outline (empty for other kinds)"
    )
    feature_name: str = Field(description="Name of the Feature the unit belongs to")
    feature_description: str = Field(description="The Feature's description prose (dedented)")
    rule: ExportedEnclosingRule | None = Field(
        default=None, description="The enclosing Rule's summary for units nested under one, else None"
    )
    prose: tuple[SpecProseFile, ...] = Field(
        description="Overview files from the corpus root down to the unit's folder, then the file's sidecar"
    )
    applicable_rules: tuple[ApplicableRule, ...] = Field(
        description="Invariants applying to this unit, resolved root -> folder -> file"
    )


class CorpusExport(FrozenModel):
    """The result of exporting a behavioral-spec corpus: enriched units plus language violations."""

    units: tuple[ExportedSpecUnit, ...] = Field(
        description="All representable units with resolved context, in file order then document order"
    )
    violations: tuple[SpecViolation, ...] = Field(description="All language violations found, in deterministic order")
    feature_file_count: int = Field(description="Count of .feature files seen during the scan")


class WitnessMarker(FrozenModel):
    """One ``@pytest.mark.witnesses`` application found in a Python test file."""

    coordinate: str = Field(description="The coordinate the test declares it witnesses")
    file: Path = Field(description="Python file carrying the marker")
    line: int = Field(description="1-based line of the marker call")
    partial: str | None = Field(
        default=None, description="The partial= note (what the test does not cover), when given"
    )


class WitnessProblem(FrozenModel):
    """One problem found while checking witnesses markers against the corpus."""

    file: Path = Field(description="Python file the problem applies to")
    line: int | None = Field(description="1-based line of the problem where available")
    message: str = Field(description="Human-readable description of the problem")
