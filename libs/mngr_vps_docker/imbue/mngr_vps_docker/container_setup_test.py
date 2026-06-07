import base64
import subprocess
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_vps_docker.container_setup import _build_start_container_script
from imbue.mngr_vps_docker.container_setup import _remote_sh_command
from imbue.mngr_vps_docker.container_setup import start_container


class _StubOuter(MutableModel):
    """Records each idempotent command and returns a canned result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: CommandResult = Field(description="Result to return from execute_idempotent_command")
    recorded_commands: list[str] = Field(default_factory=list, description="Commands recorded in order")

    def get_name(self) -> str:
        return "stub-outer"

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)
        return self.result


def _outer(result: CommandResult) -> OuterHostInterface:
    return cast(OuterHostInterface, _StubOuter(result=result))


def _decode_remote_command(command: str) -> str:
    encoded = command.split(" | ", 1)[0].removeprefix("echo ")
    return base64.b64decode(encoded).decode("utf-8")


def test_build_start_container_script_shell_quotes_name() -> None:
    # A hostile name must be shell-quoted so it can't break out of the assignment.
    script = _build_start_container_script("evil; rm -rf /")
    assert "name='evil; rm -rf /'" in script
    assert "__CONTAINER_NAME__" not in script


def test_build_start_container_script_has_recovery_shape() -> None:
    script = _build_start_container_script("my-container")
    # Fast path: a plain docker start.
    assert 'docker start "$name"' in script
    # Recovery only fires on the gVisor self-overlay filestore collision.
    assert "gvisor.filestore" in script
    assert "repeated submounts" in script
    # Reap is scoped to this container id AND runsc (never a broad pattern).
    assert 'grep -F "$cid" | grep runsc' in script
    # Stale on-disk filestore is cleared from the container's overlay dirs.
    assert 'rm -f "$d"/.gvisor.filestore.*' in script


def test_start_container_script_is_valid_posix_sh() -> None:
    # Guard against quoting/syntax regressions in the embedded recovery script.
    script = _build_start_container_script("minds-dev-josh-1-lima-4")
    check = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True)
    assert check.returncode == 0, check.stderr


def test_remote_sh_command_round_trips() -> None:
    script = _build_start_container_script("c1")
    command = _remote_sh_command(script)
    assert command.endswith("| base64 -d | sh")
    assert _decode_remote_command(command) == script


def test_start_container_runs_wrapped_script_on_success() -> None:
    outer = _outer(CommandResult(stdout="c1\n", stderr="", success=True))
    start_container(outer, "c1")
    recorded = cast(_StubOuter, outer).recorded_commands
    assert len(recorded) == 1
    # The single round-trip carries the full start+recovery script.
    assert _decode_remote_command(recorded[0]) == _build_start_container_script("c1")


def test_start_container_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(MngrError, match="docker start c1 failed: boom"):
        start_container(outer, "c1")
