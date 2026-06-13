from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import HostName
from imbue.mngr_smolvm.errors import SmolvmCommandError
from imbue.mngr_smolvm.smolvm_cli import get_smolvm_version
from imbue.mngr_smolvm.smolvm_cli import smolvm_machine_name


def _write_version_stub(tmp_path: Path, script_body: str) -> Path:
    """Write an executable stub standing in for the smolvm binary."""
    stub_path = tmp_path / "smolvm-stub"
    stub_path.write_text(f"#!/bin/sh\n{script_body}\n")
    stub_path.chmod(0o755)
    return stub_path


def test_smolvm_machine_name_applies_prefix() -> None:
    assert smolvm_machine_name(HostName("my-host"), "mngr-") == "mngr-my-host"


def test_smolvm_machine_name_custom_prefix() -> None:
    assert smolvm_machine_name(HostName("h"), "custom-") == "custom-h"


def test_get_smolvm_version_parses_version(cg: ConcurrencyGroup, tmp_path: Path) -> None:
    stub_path = _write_version_stub(tmp_path, "echo 'smolvm 1.2.3'")
    assert get_smolvm_version(cg, str(stub_path)) == (1, 2, 3)


def test_get_smolvm_version_raises_command_error_on_nonzero_exit(cg: ConcurrencyGroup, tmp_path: Path) -> None:
    """A failing `--version` probe surfaces as SmolvmCommandError, not a leaked
    ProcessError, so callers like discover_hosts can degrade gracefully."""
    stub_path = _write_version_stub(tmp_path, "echo 'boom' >&2\nexit 3")
    with pytest.raises(SmolvmCommandError) as exc_info:
        get_smolvm_version(cg, str(stub_path))
    assert exc_info.value.returncode == 3
    assert "boom" in exc_info.value.stderr


def test_get_smolvm_version_raises_command_error_on_unparseable_output(cg: ConcurrencyGroup, tmp_path: Path) -> None:
    stub_path = _write_version_stub(tmp_path, "echo 'no version here'")
    with pytest.raises(SmolvmCommandError):
        get_smolvm_version(cg, str(stub_path))
