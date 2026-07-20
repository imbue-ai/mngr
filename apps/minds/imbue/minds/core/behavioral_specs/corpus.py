"""Scanning of the behavioral-spec corpus (``apps/minds/specs`` in this repo).

The single entry point is :func:`scan_corpus`, a pure-ish reader: it walks a
corpus root, parses every ``.feature`` file with ``gherkin-official`` (the
arbiter of syntactic validity per the minds-behavioral-specs skill), extracts
one :class:`SpecUnit` per authored unit (Scenario, Scenario Outline, Rule),
and collects every language violation as data rather than raising. Callers
decide what to do with violations (the ``minds specs`` CLI prints them).

The gherkin parser returns plain nested dicts; the private ``_Gherkin*``
pydantic models below mirror exactly the subset of the AST this module
consumes, so the rest of the code works with typed, validated objects.
"""

from pathlib import Path

from gherkin.parser import Parser
from gherkin.token_matcher import TokenMatcher
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.core.behavioral_specs.data_types import CorpusScan
from imbue.minds.core.behavioral_specs.data_types import SpecStep
from imbue.minds.core.behavioral_specs.data_types import SpecUnit
from imbue.minds.core.behavioral_specs.data_types import SpecUnitKind
from imbue.minds.core.behavioral_specs.data_types import SpecViolation
from imbue.minds.errors import SpecCorpusRootNotFoundError


class _GherkinNode(FrozenModel):
    """Base for pydantic mirrors of gherkin AST nodes; unknown AST fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=False)


class _GherkinLocation(_GherkinNode):
    """Line/column position of an AST node in its source file."""

    line: int = Field(description="1-based source line")


class _GherkinTag(_GherkinNode):
    """A tag as written in the source, including the '@' sigil."""

    location: _GherkinLocation = Field(description="Position of the tag")
    name: str = Field(description="Tag text including the leading '@'")


class _GherkinStep(_GherkinNode):
    """A single step line of a scenario or background."""

    location: _GherkinLocation = Field(description="Position of the step")
    keyword: str = Field(description="Step keyword as parsed, with trailing space (e.g. 'Given ')")
    text: str = Field(description="Step text after the keyword")


class _GherkinExamples(_GherkinNode):
    """An Examples block of a Scenario Outline."""

    location: _GherkinLocation = Field(description="Position of the Examples header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the Examples block")
    keyword: str = Field(description="Examples keyword as parsed")


class _GherkinScenario(_GherkinNode):
    """A Scenario or Scenario Outline node."""

    location: _GherkinLocation = Field(description="Position of the declaration header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the unit")
    keyword: str = Field(description="Declaration keyword as parsed")
    name: str = Field(description="Unit name after the keyword")
    steps: tuple[_GherkinStep, ...] = Field(description="Steps of the unit in order")
    examples: tuple[_GherkinExamples, ...] = Field(description="Examples blocks (Scenario Outline only)")


class _GherkinBackground(_GherkinNode):
    """A Background node (never a unit; carried only for keyword validation)."""

    location: _GherkinLocation = Field(description="Position of the declaration header")
    keyword: str = Field(description="Declaration keyword as parsed")
    steps: tuple[_GherkinStep, ...] = Field(description="Steps of the background in order")


class _GherkinRuleChild(_GherkinNode):
    """One child envelope of a Rule: exactly one of background/scenario is set."""

    background: _GherkinBackground | None = Field(default=None, description="Background child, if this is one")
    scenario: _GherkinScenario | None = Field(default=None, description="Scenario child, if this is one")


class _GherkinRule(_GherkinNode):
    """A Rule node."""

    location: _GherkinLocation = Field(description="Position of the declaration header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the Rule")
    keyword: str = Field(description="Declaration keyword as parsed")
    name: str = Field(description="Rule name after the keyword")
    children: tuple[_GherkinRuleChild, ...] = Field(description="Child envelopes in document order")


class _GherkinFeatureChild(_GherkinNode):
    """One child envelope of a Feature: exactly one of background/scenario/rule is set."""

    background: _GherkinBackground | None = Field(default=None, description="Background child, if this is one")
    scenario: _GherkinScenario | None = Field(default=None, description="Scenario child, if this is one")
    rule: _GherkinRule | None = Field(default=None, description="Rule child, if this is one")


class _GherkinFeature(_GherkinNode):
    """The Feature node of a document."""

    location: _GherkinLocation = Field(description="Position of the Feature header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the Feature")
    language: str = Field(description="Dialect the document was parsed with (e.g. 'en')")
    keyword: str = Field(description="Feature keyword as parsed")
    name: str = Field(description="Feature name after the keyword")
    children: tuple[_GherkinFeatureChild, ...] = Field(description="Child envelopes in document order")


class _GherkinDocument(_GherkinNode):
    """A parsed gherkin document."""

    feature: _GherkinFeature | None = Field(default=None, description="The Feature, or None for an empty document")


@pure
def _strip_tag_sigil(tag_name: str) -> str:
    return tag_name.removeprefix("@")


@pure
def _coordinate_for(folder_parts: tuple[str, ...], raw_tag: str) -> str:
    return ".".join([*folder_parts, raw_tag])


def _missing_tag_violation(file: Path, line: int, unit_keyword: str, unit_name: str) -> SpecViolation:
    return SpecViolation(
        file=file,
        line=line,
        message=(
            f"{unit_keyword} '{unit_name}' must carry at least one tag (the first tag is the unit's identity); "
            "the unit was omitted from records"
        ),
        is_unit_omitted=True,
    )


def _unit_from_scenario(
    scenario: _GherkinScenario,
    file: Path,
    folder_parts: tuple[str, ...],
    parent: str | None,
    violations: list[SpecViolation],
) -> SpecUnit | None:
    tags = tuple(_strip_tag_sigil(tag.name) for tag in scenario.tags)
    if not tags:
        violations.append(_missing_tag_violation(file, scenario.location.line, scenario.keyword, scenario.name))
        return None
    kind = SpecUnitKind.SCENARIO_OUTLINE if scenario.examples else SpecUnitKind.SCENARIO
    return SpecUnit(
        coordinate=_coordinate_for(folder_parts, tags[0]),
        kind=kind,
        name=scenario.name,
        file=file,
        line=scenario.location.line,
        tags=tags,
        steps=tuple(SpecStep(keyword=step.keyword.strip(), text=step.text) for step in scenario.steps),
        parent=parent,
    )


def _unit_from_rule(
    rule: _GherkinRule,
    file: Path,
    folder_parts: tuple[str, ...],
    violations: list[SpecViolation],
) -> SpecUnit | None:
    tags = tuple(_strip_tag_sigil(tag.name) for tag in rule.tags)
    if not tags:
        violations.append(_missing_tag_violation(file, rule.location.line, rule.keyword, rule.name))
        return None
    return SpecUnit(
        coordinate=_coordinate_for(folder_parts, tags[0]),
        kind=SpecUnitKind.RULE,
        name=rule.name,
        file=file,
        line=rule.location.line,
        tags=tags,
        steps=(),
        parent=None,
    )


def _extract_units_from_document(
    document: _GherkinDocument,
    file: Path,
    folder_parts: tuple[str, ...],
    violations: list[SpecViolation],
) -> list[SpecUnit]:
    units: list[SpecUnit] = []
    if document.feature is None:
        return units
    for child in document.feature.children:
        if child.scenario is not None:
            scenario_unit = _unit_from_scenario(child.scenario, file, folder_parts, parent=None, violations=violations)
            if scenario_unit is not None:
                units.append(scenario_unit)
        elif child.rule is not None:
            rule_unit = _unit_from_rule(child.rule, file, folder_parts, violations=violations)
            if rule_unit is not None:
                units.append(rule_unit)
            # Children of an untagged Rule are still representable units of their
            # own; they just cannot reference a parent coordinate.
            parent_coordinate = rule_unit.coordinate if rule_unit is not None else None
            for rule_child in child.rule.children:
                if rule_child.scenario is not None:
                    child_unit = _unit_from_scenario(
                        rule_child.scenario, file, folder_parts, parent=parent_coordinate, violations=violations
                    )
                    if child_unit is not None:
                        units.append(child_unit)
    return units


def scan_corpus(corpus_root: Path) -> CorpusScan:
    """Walk a behavioral-spec corpus and return its units and language violations.

    Raises SpecCorpusRootNotFoundError if the root is not an existing directory.
    """
    if not corpus_root.is_dir():
        raise SpecCorpusRootNotFoundError(f"Spec corpus root is not a directory: {corpus_root}")

    feature_files = sorted(corpus_root.rglob("*.feature"))
    units: list[SpecUnit] = []
    violations: list[SpecViolation] = []
    for feature_file in feature_files:
        folder_parts = feature_file.relative_to(corpus_root).parent.parts
        source_text = feature_file.read_text(encoding="utf-8")
        parsed = Parser().parse(source_text, TokenMatcher())
        document = _GherkinDocument.model_validate(parsed)
        units.extend(_extract_units_from_document(document, feature_file, folder_parts, violations))

    return CorpusScan(units=tuple(units), violations=tuple(violations), feature_file_count=len(feature_files))
