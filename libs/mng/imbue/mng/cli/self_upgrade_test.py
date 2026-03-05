import json
from pathlib import Path

import pytest

from imbue.concurrency_group.errors import ProcessError
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.self_upgrade import _emit_self_upgrade_result
from imbue.mng.cli.self_upgrade import _require_uv_tool_for_self_upgrade
from imbue.mng.cli.self_upgrade import _run_uv_tool_upgrade
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat

# =============================================================================
# Tests for _require_uv_tool_for_self_upgrade
# =============================================================================


def test_require_uv_tool_for_self_upgrade_raises_when_no_receipt() -> None:
    """_require_uv_tool_for_self_upgrade should raise AbortError when receipt_path is None."""
    with pytest.raises(AbortError, match="not installed via 'uv tool install'"):
        _require_uv_tool_for_self_upgrade(None)


def test_require_uv_tool_for_self_upgrade_succeeds_when_receipt_exists(tmp_path: Path) -> None:
    """_require_uv_tool_for_self_upgrade should not raise when receipt_path is provided."""
    fake_receipt = tmp_path / "uv-receipt.toml"
    fake_receipt.write_text("")
    _require_uv_tool_for_self_upgrade(fake_receipt)


# =============================================================================
# Tests for _run_uv_tool_upgrade
# =============================================================================


class _SuccessResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class _SuccessConcurrencyGroup:
    """Concrete fake that returns a successful process result."""

    def __init__(self, stdout: str) -> None:
        self._stdout = stdout

    def run_process_to_completion(self, command: tuple[str, ...]) -> _SuccessResult:
        return _SuccessResult(self._stdout)


class _FailingConcurrencyGroup:
    """Concrete fake that raises ProcessError on run_process_to_completion."""

    def __init__(self, stderr: str, stdout: str = "") -> None:
        self._stderr = stderr
        self._stdout = stdout

    def run_process_to_completion(self, command: tuple[str, ...]) -> None:
        raise ProcessError(
            command=command,
            stderr=self._stderr,
            stdout=self._stdout,
            returncode=1,
        )


def test_run_uv_tool_upgrade_returns_stripped_stdout() -> None:
    """_run_uv_tool_upgrade should return stripped stdout on success."""
    cg = _SuccessConcurrencyGroup("  Updated mng v1.0 -> v1.1  \n")
    result = _run_uv_tool_upgrade(cg)
    assert result == "Updated mng v1.0 -> v1.1"


def test_run_uv_tool_upgrade_raises_abort_on_process_error_with_stderr() -> None:
    """_run_uv_tool_upgrade should raise AbortError with stderr message on failure."""
    cg = _FailingConcurrencyGroup(stderr="error: package not found\n")
    with pytest.raises(AbortError, match="Failed to upgrade mng: error: package not found"):
        _run_uv_tool_upgrade(cg)


def test_run_uv_tool_upgrade_raises_abort_on_process_error_with_stdout_fallback() -> None:
    """_run_uv_tool_upgrade should fall back to stdout when stderr is empty."""
    cg = _FailingConcurrencyGroup(stderr="", stdout="something went wrong\n")
    with pytest.raises(AbortError, match="Failed to upgrade mng: something went wrong"):
        _run_uv_tool_upgrade(cg)


def test_run_uv_tool_upgrade_preserves_original_exception() -> None:
    """_run_uv_tool_upgrade should chain the original ProcessError."""
    cg = _FailingConcurrencyGroup(stderr="fail")
    with pytest.raises(AbortError) as exc_info:
        _run_uv_tool_upgrade(cg)
    assert exc_info.value.original_exception is not None
    assert isinstance(exc_info.value.original_exception, ProcessError)


# =============================================================================
# Tests for _emit_self_upgrade_result
# =============================================================================


def test_emit_self_upgrade_result_human_with_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_self_upgrade_result should print stdout in HUMAN format."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_self_upgrade_result("Updated mng v1.0.0 -> v1.1.0", output_opts)

    captured = capsys.readouterr()
    assert "Updated mng v1.0.0 -> v1.1.0" in captured.out


def test_emit_self_upgrade_result_human_no_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_self_upgrade_result should print a default message when stdout is empty."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_self_upgrade_result("", output_opts)

    captured = capsys.readouterr()
    assert "mng upgraded successfully" in captured.out


def test_emit_self_upgrade_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_self_upgrade_result should output valid JSON."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_self_upgrade_result("Updated mng v1.0.0 -> v1.1.0", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["upgraded"] is True
    assert data["message"] == "Updated mng v1.0.0 -> v1.1.0"


def test_emit_self_upgrade_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_self_upgrade_result should output JSONL with event type."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_self_upgrade_result("Updated mng v1.0.0 -> v1.1.0", output_opts)

    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "self_upgraded"
    assert data["message"] == "Updated mng v1.0.0 -> v1.1.0"
