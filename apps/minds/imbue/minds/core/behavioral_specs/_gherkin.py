"""Private gherkin-official AST mirrors and the document parse step, shared by corpus.py and export.py.

The gherkin parser returns plain nested dicts; the pydantic models below
mirror exactly the subset of the AST this package consumes, so the rest of
the code works with typed, validated objects. Unknown AST fields are ignored.
"""

from pathlib import Path

from gherkin.errors import CompositeParserException
from gherkin.errors import ParserError
from gherkin.parser import Parser
from gherkin.token_matcher import TokenMatcher
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.core.behavioral_specs.data_types import SpecViolation


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


class _GherkinTableCell(_GherkinNode):
    """One cell of an Examples table row."""

    value: str = Field(description="Cell text")


class _GherkinTableRow(_GherkinNode):
    """One row of an Examples table (header or body)."""

    location: _GherkinLocation = Field(description="Position of the row")
    cells: tuple[_GherkinTableCell, ...] = Field(description="Cells of the row in order")


class _GherkinExamples(_GherkinNode):
    """An Examples block of a Scenario Outline."""

    location: _GherkinLocation = Field(description="Position of the Examples header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the Examples block")
    keyword: str = Field(description="Examples keyword as parsed")
    table_header: _GherkinTableRow | None = Field(
        default=None, alias="tableHeader", description="Header row of the Examples table, if present"
    )
    table_body: tuple[_GherkinTableRow, ...] = Field(
        default=(), alias="tableBody", description="Body rows of the Examples table"
    )


class _GherkinScenario(_GherkinNode):
    """A Scenario or Scenario Outline node."""

    location: _GherkinLocation = Field(description="Position of the declaration header")
    tags: tuple[_GherkinTag, ...] = Field(description="Tags on the unit")
    keyword: str = Field(description="Declaration keyword as parsed")
    name: str = Field(description="Unit name after the keyword")
    description: str = Field(default="", description="Free prose under the declaration header")
    steps: tuple[_GherkinStep, ...] = Field(description="Steps of the unit in order")
    examples: tuple[_GherkinExamples, ...] = Field(description="Examples blocks (Scenario Outline only)")


class _GherkinBackground(_GherkinNode):
    """A Background node (never a unit; carried for keyword validation and step folding)."""

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
    description: str = Field(default="", description="Free prose under the declaration header")
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
    description: str = Field(default="", description="Free prose under the Feature header")
    children: tuple[_GherkinFeatureChild, ...] = Field(description="Child envelopes in document order")


class _GherkinDocument(_GherkinNode):
    """A parsed gherkin document."""

    feature: _GherkinFeature | None = Field(default=None, description="The Feature, or None for an empty document")


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
