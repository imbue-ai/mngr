import base64
import json
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_file.cli.get import _emit_get_result


def test_emit_get_result_human_writes_raw_bytes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template=None)
    content = b"hello world"

    _emit_get_result(Path("/test/file.txt"), content, output_opts)

    captured = capsys.readouterr()
    assert captured.out == "hello world"


def test_emit_get_result_json_includes_base64_content(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSON, format_template=None)
    content = b"binary\x00data"

    _emit_get_result(Path("/test/file.bin"), content, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_read"
    assert parsed["size"] == len(content)
    assert parsed["content_base64"] == base64.b64encode(content).decode("ascii")
    assert base64.b64decode(parsed["content_base64"]) == content


def test_emit_get_result_jsonl_includes_base64_content(capsys: pytest.CaptureFixture[str]) -> None:
    output_opts = OutputOptions(output_format=OutputFormat.JSONL, format_template=None)
    content = b"test data"

    _emit_get_result(Path("/test/file.txt"), content, output_opts)

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["event"] == "file_read"
    assert parsed["content_base64"] == base64.b64encode(content).decode("ascii")
