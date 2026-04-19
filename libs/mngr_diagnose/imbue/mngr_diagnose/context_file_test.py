import json
from pathlib import Path

from imbue.mngr_diagnose.context_file import DiagnoseContext
from imbue.mngr_diagnose.context_file import read_diagnose_context


def test_read_full_context(tmp_path: Path) -> None:
    data = {
        "traceback_str": "Traceback:\n  ValueError: oops",
        "mngr_version": "0.2.4",
        "error_type": "ValueError",
        "error_message": "oops",
    }
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(data))

    ctx = read_diagnose_context(path)
    assert ctx.traceback_str == "Traceback:\n  ValueError: oops"
    assert ctx.mngr_version == "0.2.4"
    assert ctx.error_type == "ValueError"
    assert ctx.error_message == "oops"


def test_read_minimal_context(tmp_path: Path) -> None:
    data = {
        "mngr_version": "0.2.4",
    }
    path = tmp_path / "ctx.json"
    path.write_text(json.dumps(data))

    ctx = read_diagnose_context(path)
    assert ctx.traceback_str is None
    assert ctx.mngr_version == "0.2.4"
    assert ctx.error_type is None
    assert ctx.error_message is None


def test_diagnose_context_is_frozen() -> None:
    ctx = DiagnoseContext(mngr_version="0.2.4")
    assert ctx.model_config.get("frozen") is True
