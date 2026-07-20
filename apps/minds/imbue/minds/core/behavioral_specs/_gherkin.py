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


class GherkinNode(FrozenModel):
    """Base for pydantic mirrors of gherkin AST nodes; unknown AST fields are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=False)


class GherkinLocation(GherkinNode):
    """Line/column position of an AST node in its source file."""

    line: int = Field(description="1-based source line")


class GherkinTag(GherkinNode):
    """A tag as written in the source, including the '@' sigil."""

    location: GherkinLocation = Field(description="Position of the tag")
    name: str = Field(description="Tag text including the leading '@'")


class GherkinStep(GherkinNode):
    """A single step line of a scenario or background."""

    location: GherkinLocation = Field(description="Position of the step")
    keyword: str = Field(description="Step keyword as parsed, with trailing space (e.g. 'Given ')")
    text: str = Field(description="Step text after the keyword")


class GherkinTableCell(GherkinNode):
    """One cell of an Examples table row."""

    value: str = Field(description="Cell text")


class GherkinTableRow(GherkinNode):
    """One row of an Examples table (header or body)."""

    location: GherkinLocation = Field(description="Position of the row")
    cells: tuple[GherkinTableCell, ...] = Field(description="Cells of the row in order")


class GherkinExamples(GherkinNode):
    """An Examples block of a Scenario Outline."""

    location: GherkinLocation = Field(description="Position of the Examples header")
    tags: tuple[GherkinTag, ...] = Field(description="Tags on the Examples block")
    keyword: str = Field(description="Examples keyword as parsed")
    table_header: GherkinTableRow | None = Field(
        default=None, alias="tableHeader", description="Header row of the Examples table, if present"
    )
    table_body: tuple[GherkinTableRow, ...] = Field(
        default=(), alias="tableBody", description="Body rows of the Examples table"
    )


class GherkinScenario(GherkinNode):
    """A Scenario or Scenario Outline node."""

    location: GherkinLocation = Field(description="Position of the declaration header")
    tags: tuple[GherkinTag, ...] = Field(description="Tags on the unit")
    keyword: str = Field(description="Declaration keyword as parsed")
    name: str = Field(description="Unit name after the keyword")
    description: str = Field(default="", description="Free prose under the declaration header")
    steps: tuple[GherkinStep, ...] = Field(description="Steps of the unit in order")
    examples: tuple[GherkinExamples, ...] = Field(description="Examples blocks (Scenario Outline only)")


class GherkinBackground(GherkinNode):
    """A Background node (never a unit; carried for keyword validation and step folding)."""

    location: GherkinLocation = Field(description="Position of the declaration header")
    keyword: str = Field(description="Declaration keyword as parsed")
    steps: tuple[GherkinStep, ...] = Field(description="Steps of the background in order")


class GherkinRuleChild(GherkinNode):
    """One child envelope of a Rule: exactly one of background/scenario is set."""

    background: GherkinBackground | None = Field(default=None, description="Background child, if this is one")
    scenario: GherkinScenario | None = Field(default=None, description="Scenario child, if this is one")


class GherkinRule(GherkinNode):
    """A Rule node."""

    location: GherkinLocation = Field(description="Position of the declaration header")
    tags: tuple[GherkinTag, ...] = Field(description="Tags on the Rule")
    keyword: str = Field(description="Declaration keyword as parsed")
    name: str = Field(description="Rule name after the keyword")
    description: str = Field(default="", description="Free prose under the declaration header")
    children: tuple[GherkinRuleChild, ...] = Field(description="Child envelopes in document order")


class GherkinFeatureChild(GherkinNode):
    """One child envelope of a Feature: exactly one of background/scenario/rule is set."""

    background: GherkinBackground | None = Field(default=None, description="Background child, if this is one")
    scenario: GherkinScenario | None = Field(default=None, description="Scenario child, if this is one")
    rule: GherkinRule | None = Field(default=None, description="Rule child, if this is one")


class GherkinFeature(GherkinNode):
    """The Feature node of a document."""

    location: GherkinLocation = Field(description="Position of the Feature header")
    tags: tuple[GherkinTag, ...] = Field(description="Tags on the Feature")
    language: str = Field(description="Dialect the document was parsed with (e.g. 'en')")
    keyword: str = Field(description="Feature keyword as parsed")
    name: str = Field(description="Feature name after the keyword")
    description: str = Field(default="", description="Free prose under the Feature header")
    children: tuple[GherkinFeatureChild, ...] = Field(description="Child envelopes in document order")


class GherkinDocument(GherkinNode):
    """A parsed gherkin document."""

    feature: GherkinFeature | None = Field(default=None, description="The Feature, or None for an empty document")


def parse_feature_file(
    feature_file: Path,
    source_text: str,
    violations: list[SpecViolation],
) -> GherkinDocument | None:
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
    return GherkinDocument.model_validate(parsed)
