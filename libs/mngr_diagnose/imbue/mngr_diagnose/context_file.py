import json
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class DiagnoseContext(FrozenModel):
    """Error context read from a JSON file written by the error handler."""

    traceback_str: str | None = Field(default=None, description="Formatted traceback string")
    mngr_version: str = Field(description="mngr version at time of error")
    error_type: str | None = Field(default=None, description="Exception class name")
    error_message: str | None = Field(default=None, description="Exception message string")


def read_diagnose_context(path: Path) -> DiagnoseContext:
    """Read a diagnose context JSON file written by the error handler."""
    raw = json.loads(path.read_text())
    return DiagnoseContext.model_validate(raw)
