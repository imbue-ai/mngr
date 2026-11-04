"""The app-mngr workspace shell, run against a fake ``mngr`` that records how it was called."""

import json
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.smoke_tests.primitives import SmokeWorkspaceExecError
from imbue.minds.smoke_tests.workspace_exec import AppMngrWorkspaceShell


def _write_fake_mngr(tmp_path: Path, envelope: str, exit_code: int) -> Path:
    """A ``mngr`` that records its argv, environment and directory as JSON, then answers ``envelope``."""
    fake_mngr = tmp_path / "bin" / "mngr"
    fake_mngr.parent.mkdir(parents=True)
    record_path = tmp_path / "call.json"
    fake_mngr.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}}, open({str(record_path)!r}, 'w'))\n"
        f"print({envelope!r})\n"
        f"sys.exit({exit_code})\n"
    )
    fake_mngr.chmod(0o755)
    return fake_mngr


def _shell(tmp_path: Path, fake_mngr: Path) -> AppMngrWorkspaceShell:
    launch_cwd = tmp_path / "studio-cwd"
    launch_cwd.mkdir()
    return AppMngrWorkspaceShell(
        mngr_executable=fake_mngr,
        launch_cwd=launch_cwd,
        mngr_host_dir=tmp_path / "data" / "production" / "state" / "mngr",
        mngr_prefix=NonEmptyStr("minds-"),
        workspace_address=NonEmptyStr("agent-1@host-1.imbue_cloud_alice"),
    )


def test_a_command_s_stdout_is_what_mngr_reports_the_workspace_printed(tmp_path: Path) -> None:
    fake_mngr = _write_fake_mngr(tmp_path, exec_json_envelope("hello from the workspace 6142\n"), 0)
    shell = _shell(tmp_path, fake_mngr)

    stdout = shell.run("echo hello", PurePosixPath("/home/user/workspace"), 30.0)

    assert stdout == "hello from the workspace 6142\n"
    call = json.loads((tmp_path / "call.json").read_text())
    assert call["argv"][:4] == ["exec", "agent-1@host-1.imbue_cloud_alice", "echo hello", "--cwd"]
    assert Path(call["cwd"]).resolve() == shell.launch_cwd.resolve()
    assert call["env"]["MNGR_HOST_DIR"] == str(tmp_path / "data" / "production" / "state" / "mngr")
    assert call["env"]["MNGR_PREFIX"] == "minds-"
    assert "PYTEST_CURRENT_TEST" not in call["env"]


def test_a_command_that_failed_in_the_workspace_raises_with_what_it_printed_there(tmp_path: Path) -> None:
    envelope = exec_json_envelope("", success=False, stderr="tar: offload-modal*.toml: Cannot stat 7731")
    shell = _shell(tmp_path, _write_fake_mngr(tmp_path, envelope, 1))

    with pytest.raises(SmokeWorkspaceExecError, match="Cannot stat 7731"):
        shell.run("tar -czf - offload-modal*.toml", PurePosixPath("/home/user/workspace"), 30.0)


def test_an_app_without_its_mngr_yet_is_refused_by_path(tmp_path: Path) -> None:
    shell = _shell(tmp_path, tmp_path / "not-there" / "mngr")

    with pytest.raises(SmokeWorkspaceExecError, match="not-there"):
        shell.run("true", PurePosixPath("/home/user/workspace"), 30.0)
