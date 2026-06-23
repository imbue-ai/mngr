"""Unit tests for TMR prompt builders and pytest discovery."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.recipe import CollectTestsError
from imbue.mngr_tmr.recipe import collect_tests


def test_build_agent_prompt_contains_test_id() -> None:
    prompt = build_test_agent_prompt("tests/test_foo.py::test_bar", ())
    assert "tests/test_foo.py::test_bar" in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
    assert "IMPROVE_TEST" in prompt
    assert "FIX_TEST" in prompt
    assert "FIX_IMPL" in prompt
    assert "tests_passing_before" in prompt
    assert "tests_passing_after" in prompt
    assert "summary_markdown" in prompt


def test_build_agent_prompt_includes_pytest_flags() -> None:
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ("-m", "release"))
    assert "-m release" in prompt


def test_build_agent_prompt_requests_markdown() -> None:
    prompt = build_test_agent_prompt("t::t", ())
    assert "markdown" in prompt.lower()


def test_build_agent_prompt_instructs_one_entry_per_kind() -> None:
    prompt = build_test_agent_prompt("t::t", ())
    assert "do not duplicate kinds" in prompt.lower()


def test_build_agent_prompt_anchors_to_tutorial_and_allows_deletion() -> None:
    """The convergence objective: bounded by the tutorial block, deletion is a
    first-class action, and leaving a converged test unchanged is a valid outcome.
    """
    prompt = build_test_agent_prompt("t::t", ())
    assert "tutorial block" in prompt
    # Deletion / simplification of over-fitted assertions must be explicit.
    assert "REMOVE" in prompt
    # "No change needed" must be an allowed, good outcome.
    assert "leave the changes object empty" in prompt


def test_build_agent_prompt_has_fixme_channel() -> None:
    """Mappers must be able to flag cross-cutting setup blockers as FIXME(tmr)."""
    prompt = build_test_agent_prompt("t::t", ())
    assert "FIXME(tmr):" in prompt


def test_integrator_prompt_has_normalize_stage() -> None:
    """The reducer must normalize: extract shared scaffolding (preserving the
    tutorial 1:1) and triage FIXME(tmr) blockers into normalizations/escalations.
    """
    prompt = build_integrator_prompt()
    assert "FIXME(tmr):" in prompt
    assert "normalizations" in prompt
    assert "escalations" in prompt
    # The tutorial-1:1 extraction guardrail must be present.
    assert "tutorial block" in prompt


def test_collect_tests_with_real_pytest(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_one(): pass\ndef test_two(): pass\n")
    test_ids = collect_tests(pytest_args=(str(test_file),), source_dir=tmp_path, cg=cg)
    assert len(test_ids) == 2
    assert any("test_one" in tid for tid in test_ids)
    assert any("test_two" in tid for tid in test_ids)


def test_collect_tests_no_tests_raises(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    empty_file = tmp_path / "empty.py"
    empty_file.write_text("x = 1\n")
    with pytest.raises(CollectTestsError):
        collect_tests(pytest_args=(str(empty_file),), source_dir=tmp_path, cg=cg)


def test_collect_tests_bad_file_raises(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    with pytest.raises(CollectTestsError):
        collect_tests(pytest_args=("non_existent_test_file.py",), source_dir=tmp_path, cg=cg)


def test_integrator_prompt_references_inputs_dir_and_predicate() -> None:
    prompt = build_integrator_prompt()
    assert REDUCER_INPUTS_DIRNAME in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
    # The integrator must encode the should-pull predicate itself.
    assert "SUCCEEDED" in prompt
    assert "tests_passing_before" in prompt
    assert "tests_passing_after" in prompt
    assert "git bundle list-heads" in prompt
    assert "outputs.tar.gz" in prompt
