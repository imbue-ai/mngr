"""Unit tests for TMR prompt builders and pytest discovery."""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_task_file_mapper_prompt
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


def test_build_agent_prompt_is_docstring_anchored() -> None:
    # The generic prompt anchors scope on the docstring, not the tutorial block.
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ())
    assert "docstring" in prompt.lower()


def test_build_agent_prompt_omits_e2e_run_name_guidance_by_default() -> None:
    # Non-e2e tests get no --mngr-e2e-run-name guidance (the flag is e2e-only).
    prompt = build_test_agent_prompt("libs/mngr_aws/.../test_release_aws.py::test_x", ())
    assert "--mngr-e2e-run-name" not in prompt


def test_build_agent_prompt_includes_e2e_run_name_guidance_when_provided() -> None:
    prompt = build_test_agent_prompt(
        "libs/mngr/imbue/mngr/e2e/tutorial/test_basic.py::test_help",
        (),
        e2e_run_name="tmr_run1",
    )
    assert "--mngr-e2e-run-name" in prompt
    assert "tmr_run1_try_1" in prompt


def test_build_agent_prompt_uses_override_template(tmp_path: Path) -> None:
    # An override template file replaces the packaged mapper prompt, and still
    # receives the same render context (here, the run command).
    override = tmp_path / "custom_mapper.j2"
    override.write_text("CUSTOM MAPPER for {{ run_cmd }}\n")
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", ("-m", "release"), template_path=override)
    assert prompt.startswith("CUSTOM MAPPER for pytest tests/test_x.py::test_y -m release")


def test_build_agent_prompt_override_can_extend_packaged_template(tmp_path: Path) -> None:
    # An override may include the packaged template by name, so a variant can
    # prepend guidance without duplicating the whole prompt.
    override = tmp_path / "extended_mapper.j2"
    override.write_text("MINDS-SPECIFIC PREAMBLE\n{% include 'mapper.j2' %}\n")
    prompt = build_test_agent_prompt("tests/test_x.py::test_y", (), template_path=override)
    assert prompt.startswith("MINDS-SPECIFIC PREAMBLE")
    # The included packaged template still renders its docstring-anchored body.
    assert "docstring" in prompt.lower()


def test_committed_minds_mapper_template_renders() -> None:
    # The minds variant ships apps/minds/tmr/mapper.j2; render it through the
    # builder to prove it is valid Jinja against the mapper context and is
    # minds-tailored (no mngr-only tutorial guidance, no leading blank line).
    repo_root = Path(__file__).resolve().parents[4]
    minds_template = repo_root / "apps" / "minds" / "tmr" / "mapper.j2"
    if not minds_template.is_file():
        pytest.skip("apps/minds tree not present (running outside the monorepo checkout)")
    prompt = build_test_agent_prompt("apps/minds/test_x.py::test_y", ("-m", "release"), template_path=minds_template)
    assert prompt.startswith("You are working on the tests for the minds app")
    assert "docstring" in prompt.lower()
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt
    # mngr-only guidance must not leak into the minds variant.
    assert "FIX_TUTORIAL" not in prompt
    assert "mega_tutorial" not in prompt
    assert "--mngr-e2e-run-name" not in prompt


def test_build_integrator_prompt_uses_override_template(tmp_path: Path) -> None:
    override = tmp_path / "custom_reducer.j2"
    override.write_text("CUSTOM REDUCER reading {{ inputs_dirname }}\n")
    prompt = build_integrator_prompt(template_path=override)
    assert prompt.startswith("CUSTOM REDUCER reading ")
    assert REDUCER_INPUTS_DIRNAME in prompt


def test_build_task_file_mapper_prompt_renders_the_packet_context(tmp_path: Path) -> None:
    # The task-file mapper prompt has no packaged default: the override is the
    # template, and it receives the packet fields plus the shared outcome/publish
    # context.
    template = tmp_path / "task_mapper.j2"
    template.write_text("DO {{ task_id }} ({{ kind }})\n{{ context_json }}\n{{ outcome_filename }}\n")
    prompt = build_task_file_mapper_prompt(
        task_id="authentication.fresh-code",
        kind="scenario",
        context_json='{"coordinate": "authentication.fresh-code"}',
        template_path=template,
    )
    assert prompt.startswith("DO authentication.fresh-code (scenario)")
    assert '{"coordinate": "authentication.fresh-code"}' in prompt
    assert TESTING_AGENT_OUTCOME_FILENAME in prompt


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
