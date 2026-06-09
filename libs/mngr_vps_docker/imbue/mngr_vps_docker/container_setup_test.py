import base64
import subprocess

from imbue.mngr_vps_docker.container_setup import _build_start_container_script
from imbue.mngr_vps_docker.container_setup import _remote_sh_command


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
    encoded = command.split(" | ", 1)[0].removeprefix("echo ")
    assert base64.b64decode(encoded).decode("utf-8") == script
