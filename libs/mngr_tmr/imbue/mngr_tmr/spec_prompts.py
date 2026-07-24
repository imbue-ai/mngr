"""Prompts sent to spec-mapper agents and the spec reducer.

The prompt bodies live as Jinja2 templates under ``prompt_assets/``
(``spec_mapper.j2`` / ``spec_reducer.j2``); this module assembles the
context dicts they render against, exactly as :mod:`imbue.mngr_tmr.prompts`
does for the docstring-anchored recipe.

The packaged mapper template defines two named block slots a variant fills
via ``{% extends %}``: ``project_guidance`` (test-placement judgment for the
target project) and ``infra_blockers`` (host-capability knowledge). The
contract body lives only in the packaged template.
"""

from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import PUBLISH_OUTPUTS_SNIPPET
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import resolve_template

MATRIX_ARTIFACT_FILENAME = "matrix.jsonl"

_SPEC_MAPPER_TEMPLATE = "spec_mapper.j2"
_SPEC_REDUCER_TEMPLATE = "spec_reducer.j2"


class SpecUnitPromptView(FrozenModel):
    """One spec unit as the mapper prompt's task table presents it."""

    coordinate: str = Field(description="The unit's coordinate")
    kind: str = Field(description="Unit kind in record spelling (scenario | scenario-outline | rule)")
    name: str = Field(description="The unit's name as written in the .feature file")
    line: int = Field(description="1-based line of the unit's declaration header")
    parent: str | None = Field(description="Coordinate of the enclosing Rule for nested units, else None")
    invariants: tuple[str, ...] = Field(description="Coordinates of every Rule in scope for this unit")


def _specs_matrix_command(corpus_root: str, test_roots: tuple[str, ...]) -> str:
    tests_arguments = "".join(f" --tests {test_root}" for test_root in test_roots)
    return f"mngr specs matrix --root {corpus_root}{tests_arguments}"


def build_spec_mapper_prompt(
    feature_path: str,
    units: tuple[SpecUnitPromptView, ...],
    corpus_root: str,
    test_roots: tuple[str, ...],
    testing_flags: tuple[str, ...],
    template_path: Path | None = None,
) -> str:
    """Build the prompt/initial message for a spec-mapper agent.

    ``feature_path`` and ``corpus_root`` are repo-relative (the mapper reads
    the corpus files in its own checkout). ``template_path`` overrides the
    packaged spec_mapper template when provided.
    """
    flags_suffix = " " + " ".join(testing_flags) if testing_flags else ""
    template = resolve_template(_SPEC_MAPPER_TEMPLATE, template_path)
    return template.render(
        feature_path=feature_path,
        units=units,
        corpus_root=corpus_root,
        specs_matrix_cmd=_specs_matrix_command(corpus_root, test_roots),
        flags_suffix=flags_suffix,
        outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        publish_snippet=PUBLISH_OUTPUTS_SNIPPET,
    )


def build_spec_reducer_prompt(
    corpus_root: str,
    test_roots: tuple[str, ...],
    template_path: Path | None = None,
) -> str:
    """Build the spec reducer's initial message.

    Integration mirrors TMR (should-pull, squash test kinds, cherry-pick impl
    by priority); normalize additionally audits witness links by running
    ``mngr specs matrix`` over the integrated tree and shipping the artifact.
    """
    template = resolve_template(_SPEC_REDUCER_TEMPLATE, template_path)
    return template.render(
        inputs_dirname=REDUCER_INPUTS_DIRNAME,
        mapper_outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        reducer_outcome_filename=INTEGRATOR_OUTCOME_FILENAME,
        publish_snippet=PUBLISH_OUTPUTS_SNIPPET,
        corpus_root=corpus_root,
        specs_matrix_cmd=_specs_matrix_command(corpus_root, test_roots),
        matrix_filename=MATRIX_ARTIFACT_FILENAME,
    )
