"""Unit tests for framework CLI helpers."""

import json
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.cli import disable_modal_initial_snapshot
from imbue.mngr_mapreduce.cli import emit_agents_launched
from imbue.mngr_mapreduce.cli import emit_report_path
from imbue.mngr_mapreduce.cli import emit_task_count


def _human_output_opts() -> OutputOptions:
    return OutputOptions(output_format=OutputFormat.HUMAN)


def test_emit_task_count_human_writes_pluralized_count_line(capsys: pytest.CaptureFixture[str]) -> None:
    emit_task_count(5, _human_output_opts())
    assert capsys.readouterr().out == snapshot("Discovered 5 task(s)\n")


def test_emit_agents_launched_human_writes_launched_count_line(capsys: pytest.CaptureFixture[str]) -> None:
    emit_agents_launched(3, _human_output_opts())
    assert capsys.readouterr().out == snapshot("Launched 3 agent(s)\n")


def test_emit_report_path_human_writes_report_path_line(capsys: pytest.CaptureFixture[str]) -> None:
    emit_report_path(Path("/tmp/report.html"), _human_output_opts())
    assert capsys.readouterr().out == snapshot("Report: /tmp/report.html\n")


def test_emit_task_count_json_emits_nothing_until_final_output(capsys: pytest.CaptureFixture[str]) -> None:
    # JSON mode stays silent until the command's single terminating object;
    # per-step events must not leak to stdout.
    emit_task_count(10, OutputOptions(output_format=OutputFormat.JSON))
    assert capsys.readouterr().out == ""


def test_emit_report_path_json_emits_nothing_until_final_output(capsys: pytest.CaptureFixture[str]) -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSON))
    assert capsys.readouterr().out == ""


def test_emit_agents_launched_jsonl_emits_event_with_count(capsys: pytest.CaptureFixture[str]) -> None:
    emit_agents_launched(7, OutputOptions(output_format=OutputFormat.JSONL))
    assert json.loads(capsys.readouterr().out) == snapshot({"event": "agents_launched", "count": 7})


def test_emit_task_count_jsonl_emits_event_with_count(capsys: pytest.CaptureFixture[str]) -> None:
    emit_task_count(3, OutputOptions(output_format=OutputFormat.JSONL))
    assert json.loads(capsys.readouterr().out) == snapshot({"event": "tasks_discovered", "count": 3})


def test_emit_report_path_jsonl_emits_event_with_path(capsys: pytest.CaptureFixture[str]) -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSONL))
    assert json.loads(capsys.readouterr().out) == snapshot({"event": "report_generated", "path": "/tmp/report.html"})


def test_disable_modal_initial_snapshot_skips_non_modal_providers(temp_mngr_ctx: MngrContext) -> None:
    """A non-modal provider name leaves config.providers untouched."""
    before = dict(temp_mngr_ctx.config.providers)
    disable_modal_initial_snapshot(temp_mngr_ctx, "local")
    disable_modal_initial_snapshot(temp_mngr_ctx, "docker")
    assert dict(temp_mngr_ctx.config.providers) == before


def test_disable_modal_initial_snapshot_silent_when_modal_backend_unregistered(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With --provider modal but no modal backend registered (the test fixture
    only registers local + ssh), the helper silently no-ops; the caller will
    surface the UnknownBackendError later when it tries to actually use modal.
    """
    before = dict(temp_mngr_ctx.config.providers)
    disable_modal_initial_snapshot(temp_mngr_ctx, "modal")
    assert dict(temp_mngr_ctx.config.providers) == before
