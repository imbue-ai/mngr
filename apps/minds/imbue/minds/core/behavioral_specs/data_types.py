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
    parent: str | None = Field(
        description="Coordinate of the enclosing Rule for units nested under one, else None"
    )


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
