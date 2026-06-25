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
    # The builder must interpolate the given test node id and the outcome
    # filename (a contract with the orchestrator) into the rendered prompt.
    prompt = build_test_agent_prompt("tests/test_foo.py::test_bar", ())
    assert "tests/test_foo.py::test_bar" in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt


def test_build_agent_prompt_includes_pytest_flags() -> None:
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ("-m", "release"))
    assert "-m release" in prompt


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


def test_integrator_prompt_interpolates_framework_constants() -> None:
    # The builder must wire the framework's inputs-dir and outcome-filename
    # constants into the rendered prompt -- a contract with the orchestrator,
    # which rsyncs each mapper's outputs into that dir under that filename.
    prompt = build_integrator_prompt()
    assert REDUCER_INPUTS_DIRNAME in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
