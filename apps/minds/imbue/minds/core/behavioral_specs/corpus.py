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

import os
import re
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from gherkin.errors import CompositeParserException
from gherkin.errors import ParserError
from gherkin.parser import Parser
from gherkin.token_matcher import TokenMatcher
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.errors import SwitchError
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


# A kebab-case name: lowercase letters/digits in groups separated by single hyphens.
_KEBAB_CASE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Any comment line of the '# language: xx' directive form. The parser consumes a
# leading directive silently (it never shows up in the parsed comments), so the
# raw source is the only reliable place to detect one. Matched anywhere in the
# file (conservative): a spec never needs such a comment, directive or not.
_LANGUAGE_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*#\s*language\s*:", re.IGNORECASE)

# The spec language's exhaustive construct list (per the minds-behavioral-specs
# skill). Other en-dialect spellings (Ability, Business Need, Scenario Template,
# Scenarios, '*') parse fine under gherkin-official but are not in the language.
_ALLOWED_FEATURE_KEYWORDS: Final[tuple[str, ...]] = ("Feature",)
_ALLOWED_BACKGROUND_KEYWORDS: Final[tuple[str, ...]] = ("Background",)
_ALLOWED_SCENARIO_KEYWORDS: Final[tuple[str, ...]] = ("Scenario", "Example", "Scenario Outline")
_ALLOWED_EXAMPLES_KEYWORDS: Final[tuple[str, ...]] = ("Examples",)
_ALLOWED_RULE_KEYWORDS: Final[tuple[str, ...]] = ("Rule",)
_ALLOWED_STEP_KEYWORDS: Final[tuple[str, ...]] = ("Given", "When", "Then", "And", "But")

# Keywords that make a unit a Scenario Outline. 'Scenario Template' is itself a
# violation (not in the construct list) but still denotes an outline, so the
# unit's record stays truthful while validate reports the spelling.
_OUTLINE_KEYWORDS: Final[tuple[str, ...]] = ("Scenario Outline", "Scenario Template")


@pure
def _strip_tag_sigil(tag_name: str) -> str:
    return tag_name.removeprefix("@")


@pure
def _is_kebab_case(name: str) -> bool:
    return _KEBAB_CASE_PATTERN.fullmatch(name) is not None


def _check_tags_are_kebab_case(
    tags: tuple[_GherkinTag, ...],
    file: Path,
    violations: list[SpecViolation],
) -> None:
    for tag in tags:
        if not _is_kebab_case(_strip_tag_sigil(tag.name)):
            violations.append(
                SpecViolation(
                    file=file,
                    line=tag.location.line,
                    message=(
                        f"tag '{tag.name}' is not kebab-case "
                        "(expected lowercase letters/digits separated by single hyphens)"
                    ),
                    is_unit_omitted=False,
                )
            )


@pure
def _coordinate_for(folder_parts: tuple[str, ...], raw_tag: str) -> str:
    return ".".join([*folder_parts, raw_tag])


def _check_declaration_keyword(
    keyword: str,
    allowed_keywords: tuple[str, ...],
    file: Path,
    line: int,
    violations: list[SpecViolation],
) -> None:
    if keyword not in allowed_keywords:
        allowed_rendered = ", ".join(f"'{allowed}'" for allowed in allowed_keywords)
        violations.append(
            SpecViolation(
                file=file,
                line=line,
                message=f"keyword '{keyword}' is not part of the spec language (allowed here: {allowed_rendered})",
                is_unit_omitted=False,
            )
        )


def _check_step_keywords(
    steps: tuple[_GherkinStep, ...],
    file: Path,
    violations: list[SpecViolation],
) -> None:
    for step in steps:
        keyword = step.keyword.strip()
        if keyword not in _ALLOWED_STEP_KEYWORDS:
            allowed_rendered = ", ".join(f"'{allowed}'" for allowed in _ALLOWED_STEP_KEYWORDS)
            violations.append(
                SpecViolation(
                    file=file,
                    line=step.location.line,
                    message=(
                        f"step keyword '{keyword}' is not part of the spec language (allowed: {allowed_rendered})"
                    ),
                    is_unit_omitted=False,
                )
            )


class _ClaimSite(FrozenModel):
    """Where a coordinate was first claimed (for duplicate-claim reporting)."""

    file: Path = Field(description="File containing the first claiming tag")
    line: int = Field(description="1-based line of the first claiming tag")


def _claim_coordinate(
    coordinate: str,
    file: Path,
    line: int,
    claims: dict[str, _ClaimSite],
    violations: list[SpecViolation],
) -> None:
    """Record a coordinate claim, reporting a violation when it was already claimed."""
    existing = claims.get(coordinate)
    if existing is None:
        claims[coordinate] = _ClaimSite(file=file, line=line)
        return
    violations.append(
        SpecViolation(
            file=file,
            line=line,
            message=(
                f"coordinate '{coordinate}' is already claimed at {existing.file}:{existing.line}; "
                "no coordinate may be claimed twice"
            ),
            is_unit_omitted=False,
        )
    )


def _claim_block_tags(
    tags: tuple[_GherkinTag, ...],
    file: Path,
    folder_parts: tuple[str, ...],
    claims: dict[str, _ClaimSite],
    violations: list[SpecViolation],
) -> None:
    """Claim a coordinate for every tag on a Feature or Examples block."""
    for tag in tags:
        coordinate = _coordinate_for(folder_parts, _strip_tag_sigil(tag.name))
        _claim_coordinate(coordinate, file, tag.location.line, claims, violations)


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
    claims: dict[str, _ClaimSite],
) -> SpecUnit | None:
    _check_declaration_keyword(scenario.keyword, _ALLOWED_SCENARIO_KEYWORDS, file, scenario.location.line, violations)
    _check_step_keywords(scenario.steps, file, violations)
    _check_tags_are_kebab_case(scenario.tags, file, violations)
    for examples in scenario.examples:
        _check_declaration_keyword(
            examples.keyword, _ALLOWED_EXAMPLES_KEYWORDS, file, examples.location.line, violations
        )
        _check_tags_are_kebab_case(examples.tags, file, violations)
    tags = tuple(_strip_tag_sigil(tag.name) for tag in scenario.tags)
    if not tags:
        violations.append(_missing_tag_violation(file, scenario.location.line, scenario.keyword, scenario.name))
        return None
    coordinate = _coordinate_for(folder_parts, tags[0])
    _claim_coordinate(coordinate, file, scenario.tags[0].location.line, claims, violations)
    for examples in scenario.examples:
        _claim_block_tags(examples.tags, file, folder_parts, claims, violations)
    kind = SpecUnitKind.SCENARIO_OUTLINE if scenario.keyword in _OUTLINE_KEYWORDS else SpecUnitKind.SCENARIO
    if kind == SpecUnitKind.SCENARIO and scenario.examples:
        violations.append(
            SpecViolation(
                file=file,
                line=scenario.examples[0].location.line,
                message=(
                    f"Examples blocks belong to a Scenario Outline, not a {scenario.keyword}; "
                    "declare the unit as a Scenario Outline or drop the Examples"
                ),
                is_unit_omitted=False,
            )
        )
    return SpecUnit(
        coordinate=coordinate,
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
    claims: dict[str, _ClaimSite],
) -> SpecUnit | None:
    _check_declaration_keyword(rule.keyword, _ALLOWED_RULE_KEYWORDS, file, rule.location.line, violations)
    _check_tags_are_kebab_case(rule.tags, file, violations)
    tags = tuple(_strip_tag_sigil(tag.name) for tag in rule.tags)
    if not tags:
        violations.append(_missing_tag_violation(file, rule.location.line, rule.keyword, rule.name))
        return None
    coordinate = _coordinate_for(folder_parts, tags[0])
    _claim_coordinate(coordinate, file, rule.tags[0].location.line, claims, violations)
    return SpecUnit(
        coordinate=coordinate,
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
    claims: dict[str, _ClaimSite],
) -> list[SpecUnit]:
    units: list[SpecUnit] = []
    if document.feature is None:
        violations.append(
            SpecViolation(
                file=file,
                line=None,
                message="a .feature file must contain exactly one Feature declaration",
                is_unit_omitted=False,
            )
        )
        return units
    if document.feature.language != "en":
        violations.append(
            SpecViolation(
                file=file,
                line=document.feature.location.line,
                message=(
                    f"document parses with Gherkin dialect '{document.feature.language}'; "
                    "only the default English keywords are allowed"
                ),
                is_unit_omitted=False,
            )
        )
    feature = document.feature
    _check_declaration_keyword(feature.keyword, _ALLOWED_FEATURE_KEYWORDS, file, feature.location.line, violations)
    _check_tags_are_kebab_case(feature.tags, file, violations)
    _claim_block_tags(feature.tags, file, folder_parts, claims, violations)
    for child in document.feature.children:
        if child.background is not None:
            _check_declaration_keyword(
                child.background.keyword,
                _ALLOWED_BACKGROUND_KEYWORDS,
                file,
                child.background.location.line,
                violations,
            )
            _check_step_keywords(child.background.steps, file, violations)
        elif child.scenario is not None:
            scenario_unit = _unit_from_scenario(
                child.scenario, file, folder_parts, parent=None, violations=violations, claims=claims
            )
            if scenario_unit is not None:
                units.append(scenario_unit)
        elif child.rule is not None:
            rule_unit = _unit_from_rule(child.rule, file, folder_parts, violations=violations, claims=claims)
            if rule_unit is not None:
                units.append(rule_unit)
            # Children of an untagged Rule are still representable units of their
            # own; they just cannot reference a parent coordinate.
            parent_coordinate = rule_unit.coordinate if rule_unit is not None else None
            for rule_child in child.rule.children:
                if rule_child.background is not None:
                    _check_declaration_keyword(
                        rule_child.background.keyword,
                        _ALLOWED_BACKGROUND_KEYWORDS,
                        file,
                        rule_child.background.location.line,
                        violations,
                    )
                    _check_step_keywords(rule_child.background.steps, file, violations)
                elif rule_child.scenario is not None:
                    child_unit = _unit_from_scenario(
                        rule_child.scenario,
                        file,
                        folder_parts,
                        parent=parent_coordinate,
                        violations=violations,
                        claims=claims,
                    )
                    if child_unit is not None:
                        units.append(child_unit)
                else:
                    raise SwitchError(f"Rule child envelope with no known node type in {file}")
        else:
            raise SwitchError(f"Feature child envelope with no known node type in {file}")
    return units


def _check_no_language_header(source_text: str, file: Path, violations: list[SpecViolation]) -> None:
    for line_number, line_text in enumerate(source_text.splitlines(), start=1):
        if _LANGUAGE_HEADER_PATTERN.match(line_text):
            violations.append(
                SpecViolation(
                    file=file,
                    line=line_number,
                    message="'# language:' headers are not allowed; specs use the default English keywords only",
                    is_unit_omitted=False,
                )
            )


def _parse_feature_file(
    feature_file: Path,
    source_text: str,
    violations: list[SpecViolation],
) -> _GherkinDocument | None:
    """Parse one .feature file, recording parse failures as violations and returning None for them."""
    try:
        parsed = Parser().parse(source_text, TokenMatcher())
    except CompositeParserException as exc:
        for parse_error in exc.errors:
            violations.append(
                SpecViolation(
                    file=feature_file,
                    line=parse_error.location["line"],
                    message=f"gherkin parse error: {parse_error}; the file's units were omitted from records",
                    is_unit_omitted=True,
                )
            )
        return None
    except ParserError as exc:
        violations.append(
            SpecViolation(
                file=feature_file,
                line=None,
                message=f"gherkin parse error: {exc}; the file's units were omitted from records",
                is_unit_omitted=True,
            )
        )
        return None
    return _GherkinDocument.model_validate(parsed)


@pure
def spec_unit_matches_tag(unit: SpecUnit, tag_or_coordinate: str) -> bool:
    """True when the value exactly matches one of the unit's raw tags or its coordinate.

    A single leading '@' sigil on the value is tolerated (tags are written
    with one in .feature files but recorded bare).
    """
    bare_value = tag_or_coordinate.removeprefix("@")
    return bare_value in unit.tags or bare_value == unit.coordinate


@pure
def spec_unit_matches_name_substring(unit: SpecUnit, name_substring: str) -> bool:
    """True when the unit's name contains the value, case-insensitively."""
    return name_substring.lower() in unit.name.lower()


@pure
def spec_unit_matches_step_substring(unit: SpecUnit, step_substring: str) -> bool:
    """True when any of the unit's own step texts contains the value, case-insensitively."""
    return any(step_substring.lower() in step.text.lower() for step in unit.steps)


@pure
def spec_unit_kind_record_value(kind: SpecUnitKind) -> str:
    """Render a unit kind as its JSONL record spelling."""
    match kind:
        case SpecUnitKind.SCENARIO:
            return "scenario"
        case SpecUnitKind.SCENARIO_OUTLINE:
            return "scenario-outline"
        case SpecUnitKind.RULE:
            return "rule"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def spec_unit_to_record(unit: SpecUnit) -> dict[str, Any]:
    """Render a unit as the JSON object emitted (one per line) by ``minds specs list``/``query``."""
    return {
        "coordinate": unit.coordinate,
        "kind": spec_unit_kind_record_value(unit.kind),
        "name": unit.name,
        "file": str(unit.file),
        "line": unit.line,
        "tags": list(unit.tags),
        "steps": [{"keyword": step.keyword, "text": step.text} for step in unit.steps],
        "parent": unit.parent,
    }


def _scan_corpus_structure(corpus_root: Path, violations: list[SpecViolation]) -> list[Path]:
    """Walk the corpus tree, record folder/file naming violations, and return the .feature files.

    Hidden entries (names starting with '.') are tooling artifacts, not corpus
    content, and are skipped entirely. Every visible non-hidden file must be a
    .feature or .md file.
    """
    feature_files: list[Path] = []
    # Path.walk needs Python 3.12; the repo's type-check floor is 3.11.
    for folder_name, child_folder_names, file_names in os.walk(corpus_root):
        folder = Path(folder_name)
        # Pruning in place steers the walk; sorting keeps traversal deterministic.
        child_folder_names[:] = sorted(name for name in child_folder_names if not name.startswith("."))
        for child_folder_name in child_folder_names:
            if not _is_kebab_case(child_folder_name):
                violations.append(
                    SpecViolation(
                        file=folder / child_folder_name,
                        line=None,
                        message=(
                            f"folder name '{child_folder_name}' is not kebab-case "
                            "(expected lowercase letters/digits separated by single hyphens)"
                        ),
                        is_unit_omitted=False,
                    )
                )
        visible_file_names = sorted(name for name in file_names if not name.startswith("."))
        feature_basenames = {Path(name).stem for name in visible_file_names if name.endswith(".feature")}
        for file_name in visible_file_names:
            file = folder / file_name
            basename = Path(file_name).stem
            if file_name.endswith(".feature"):
                feature_files.append(file)
                _check_file_basename_is_kebab_case(file, basename, violations)
                if basename == "overview":
                    violations.append(
                        SpecViolation(
                            file=file,
                            line=None,
                            message=(
                                "'overview' is a reserved basename for the folder's overview.md prose file; "
                                "no .feature file may be named 'overview'"
                            ),
                            is_unit_omitted=False,
                        )
                    )
            elif file_name.endswith(".md"):
                _check_file_basename_is_kebab_case(file, basename, violations)
                if basename != "overview" and basename not in feature_basenames:
                    violations.append(
                        SpecViolation(
                            file=file,
                            line=None,
                            message=(
                                f"markdown file '{file_name}' has no matching '{basename}.feature' in its folder; "
                                "every .md except overview.md must be the sidecar of a same-basename .feature"
                            ),
                            is_unit_omitted=False,
                        )
                    )
            else:
                violations.append(
                    SpecViolation(
                        file=file,
                        line=None,
                        message=f"unexpected file '{file_name}': only .feature and .md files belong in a spec corpus folder",
                        is_unit_omitted=False,
                    )
                )
    return sorted(feature_files)


def _check_file_basename_is_kebab_case(file: Path, basename: str, violations: list[SpecViolation]) -> None:
    if not _is_kebab_case(basename):
        violations.append(
            SpecViolation(
                file=file,
                line=None,
                message=(
                    f"file basename '{basename}' is not kebab-case "
                    "(expected lowercase letters/digits separated by single hyphens)"
                ),
                is_unit_omitted=False,
            )
        )


def scan_corpus(corpus_root: Path) -> CorpusScan:
    """Walk a behavioral-spec corpus and return its units and language violations.

    Raises SpecCorpusRootNotFoundError if the root is not an existing directory.
    """
    if not corpus_root.is_dir():
        raise SpecCorpusRootNotFoundError(f"Spec corpus root is not a directory: {corpus_root}")
    units: list[SpecUnit] = []
    violations: list[SpecViolation] = []
    claims: dict[str, _ClaimSite] = {}
    feature_files = _scan_corpus_structure(corpus_root, violations)
    for feature_file in feature_files:
        folder_parts = feature_file.relative_to(corpus_root).parent.parts
        source_text = feature_file.read_text(encoding="utf-8")
        _check_no_language_header(source_text, feature_file, violations)
        document = _parse_feature_file(feature_file, source_text, violations)
        if document is None:
            continue
        units.extend(_extract_units_from_document(document, feature_file, folder_parts, violations, claims))

    return CorpusScan(units=tuple(units), violations=tuple(violations), feature_file_count=len(feature_files))
