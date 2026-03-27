import json
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.put import _emit_put_result


def test_emit_put_result_human_writes_message(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 1024, output_opts)

    captured = capsys.readouterr()
    assert "1024" in captured.out
    assert "/test/file.txt" in captured.out


def test_emit_put_result_json_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 512, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_written"
    assert parsed["size"] == 512
    assert parsed["path"] == "/test/file.txt"


def test_emit_put_result_jsonl_emits_event(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSONL, format_template=None)

    _emit_put_result(Path("/test/file.txt"), 256, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_written"
    assert parsed["size"] == 256
