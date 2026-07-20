"""Unit tests for the task-file map-reduce recipe and its packet loading."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.imbue_common.errors import SwitchError
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_tmr.task_file_recipe import TaskFileMapReduceRecipe
from imbue.mngr_tmr.task_file_recipe import TaskPacketLoadError
from imbue.mngr_tmr.task_file_recipe import load_task_packets


def _write_tasks_file(tasks_file: Path, packets: list[dict]) -> Path:
    tasks_file.write_text("".join(json.dumps(packet) + "\n" for packet in packets))
    return tasks_file


def _valid_packet(task_id: str = "authentication.fresh-code") -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "display_id": task_id.replace(".", "-"),
        "kind": "scenario",
        "context": {"coordinate": task_id, "effective_steps": []},
    }


def _make_ctx(temp_mngr_ctx: MngrContext, tmp_path: Path) -> MapReduceContext:
    return MapReduceContext(
        mngr_ctx=temp_mngr_ctx,
        source_dir=tmp_path,
        run_name="20260101000000",
        output_dir=tmp_path / "out",
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )


def test_load_task_packets_reads_jsonl_and_skips_blank_lines(tmp_path: Path) -> None:
    tasks_file = _write_tasks_file(tmp_path / "tasks.jsonl", [_valid_packet("a.one"), _valid_packet("a.two")])
    tasks_file.write_text(tasks_file.read_text() + "\n\n")

    packets = load_task_packets(tasks_file)

    assert [packet.id for packet in packets] == ["a.one", "a.two"]
    assert packets[0].display_id == "a-one"
    assert packets[0].kind == "scenario"
    assert packets[0].context == {"coordinate": "a.one", "effective_steps": []}


def test_load_task_packets_rejects_invalid_json_with_line_number(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.jsonl"
    tasks_file.write_text(json.dumps(_valid_packet()) + "\nnot json\n")

    with pytest.raises(TaskPacketLoadError, match=r"tasks\.jsonl:2: invalid JSON"):
        load_task_packets(tasks_file)


def test_load_task_packets_rejects_packets_missing_fields(tmp_path: Path) -> None:
    tasks_file = _write_tasks_file(tmp_path / "tasks.jsonl", [{"schema_version": 1, "kind": "scenario"}])

    with pytest.raises(TaskPacketLoadError, match=r"tasks\.jsonl:1: invalid task packet"):
        load_task_packets(tasks_file)


def test_load_task_packets_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    packet = _valid_packet()
    packet["schema_version"] = 99
    tasks_file = _write_tasks_file(tmp_path / "tasks.jsonl", [packet])

    with pytest.raises(TaskPacketLoadError, match="unsupported schema_version 99"):
        load_task_packets(tasks_file)


def test_load_task_packets_rejects_duplicate_ids(tmp_path: Path) -> None:
    tasks_file = _write_tasks_file(tmp_path / "tasks.jsonl", [_valid_packet("a.one"), _valid_packet("a.one")])

    with pytest.raises(TaskPacketLoadError, match="duplicate task id 'a.one'"):
        load_task_packets(tasks_file)


def test_load_task_packets_rejects_an_empty_file(tmp_path: Path) -> None:
    tasks_file = tmp_path / "tasks.jsonl"
    tasks_file.write_text("\n")

    with pytest.raises(TaskPacketLoadError, match="no task packets found"):
        load_task_packets(tasks_file)


def _make_recipe(tmp_path: Path, packets: list[dict], name: str = "tmr-specs") -> TaskFileMapReduceRecipe:
    tasks_file = _write_tasks_file(tmp_path / "tasks.jsonl", packets)
    mapper_prompt = tmp_path / "mapper.j2"
    mapper_prompt.write_text("TASK {{ task_id }} ({{ kind }})\n{{ context_json }}\n{{ outcome_filename }}\n")
    reducer_prompt = tmp_path / "reducer.j2"
    reducer_prompt.write_text("REDUCE {{ inputs_dirname }}\n")
    return TaskFileMapReduceRecipe(
        name=name,
        packets=load_task_packets(tasks_file),
        tasks_file=tasks_file,
        mapper_prompt_path=mapper_prompt,
        reducer_prompt_path=reducer_prompt,
    )


def test_discover_maps_packets_to_tasks(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    recipe = _make_recipe(tmp_path, [_valid_packet("a.one"), _valid_packet("a.two")])

    tasks = recipe.discover(_make_ctx(temp_mngr_ctx, tmp_path))

    assert [(task.id, task.display_id) for task in tasks] == [("a.one", "a-one"), ("a.two", "a-two")]


def test_build_mapper_prompt_renders_the_packet_context(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    recipe = _make_recipe(tmp_path, [_valid_packet("a.one")])

    prompt = recipe.build_mapper_prompt(
        _make_ctx(temp_mngr_ctx, tmp_path), MapReduceTask(id="a.one", display_id="a-one")
    )

    assert "TASK a.one (scenario)" in prompt
    assert '"coordinate": "a.one"' in prompt
    assert "testing_agent_outcome.json" in prompt


def test_build_mapper_prompt_rejects_a_foreign_task(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    recipe = _make_recipe(tmp_path, [_valid_packet("a.one")])

    with pytest.raises(SwitchError):
        recipe.build_mapper_prompt(_make_ctx(temp_mngr_ctx, tmp_path), MapReduceTask(id="a.other"))


def test_recipe_name_validation_rejects_unsafe_slugs(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Invalid recipe name"):
        _make_recipe(tmp_path, [_valid_packet()], name="bad name!")
