"""Unit tests for framework CLI helpers."""

from pathlib import Path
from typing import Any

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.cli import disable_modal_initial_snapshot
from imbue.mngr_mapreduce.cli import emit_agents_launched
from imbue.mngr_mapreduce.cli import emit_report_path
from imbue.mngr_mapreduce.cli import emit_task_count


def _human_output_opts() -> OutputOptions:
    return OutputOptions(output_format=OutputFormat.HUMAN)


def test_emit_task_count_human(capsys: object) -> None:
    emit_task_count(5, _human_output_opts())


def test_emit_agents_launched_human(capsys: object) -> None:
    emit_agents_launched(3, _human_output_opts())


def test_emit_report_path_human(capsys: object, tmp_path: object) -> None:
    emit_report_path(Path("/tmp/report.html"), _human_output_opts())


def test_emit_task_count_json() -> None:
    emit_task_count(10, OutputOptions(output_format=OutputFormat.JSON))


def test_emit_agents_launched_jsonl(capsys: Any) -> None:
    emit_agents_launched(7, OutputOptions(output_format=OutputFormat.JSONL))
    captured = capsys.readouterr()
    assert '"event": "agents_launched"' in captured.out


def test_emit_report_path_json() -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSON))


def test_emit_report_path_jsonl() -> None:
    emit_report_path(Path("/tmp/report.html"), OutputOptions(output_format=OutputFormat.JSONL))


def test_emit_task_count_jsonl(capsys: Any) -> None:
    emit_task_count(3, OutputOptions(output_format=OutputFormat.JSONL))
    captured = capsys.readouterr()
    assert '"event": "tasks_discovered"' in captured.out


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
