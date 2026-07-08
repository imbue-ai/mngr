"""Unit tests for the TMR recipe's variant identity and prompt overrides."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.mngr_tmr.recipe import TestMapReduceRecipe
from imbue.mngr_tmr.recipe import _build_run_commands


def test_recipe_name_defaults_to_tmr() -> None:
    assert TestMapReduceRecipe().name == "tmr"


def test_recipe_name_accepts_variant_slug() -> None:
    assert TestMapReduceRecipe(name="tmr-minds").name == "tmr-minds"


@pytest.mark.parametrize("bad_name", ["tmr/minds", "tmr minds", "-leading-dash", "has.dot", ""])
def test_recipe_name_rejects_unsafe_slugs(bad_name: str) -> None:
    # The name becomes a branch/agent/host name segment, so unsafe characters
    # must be rejected at construction rather than producing broken git refs.
    with pytest.raises(ValidationError):
        TestMapReduceRecipe(name=bad_name)


def test_run_commands_omit_name_flag_for_default_variant() -> None:
    commands = dict(_build_run_commands("20260101000000"))
    assert "--name" not in commands["Reintegrate"]


def test_run_commands_include_name_flag_for_custom_variant() -> None:
    # A non-default variant must round-trip its --name into the reintegrate hint
    # so the suggested command resolves the same run.
    commands = dict(_build_run_commands("20260101000000", recipe_name="tmr-minds"))
    assert "--name tmr-minds" in commands["Reintegrate"]
    assert "--run-name 20260101000000" in commands["Reintegrate"]


def test_recipe_carries_prompt_override_paths() -> None:
    recipe = TestMapReduceRecipe(
        mapper_prompt_path=Path("prompts/m.j2"),
        reducer_prompt_path=Path("prompts/r.j2"),
    )
    assert recipe.mapper_prompt_path == Path("prompts/m.j2")
    assert recipe.reducer_prompt_path == Path("prompts/r.j2")
