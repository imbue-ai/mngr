"""Unit tests for the module-level outer-host docker helpers in instance.py.

These helpers were extracted when DockerOverSsh was deleted; they wrap docker
commands that run on an outer host. The tests use a stub OuterHostInterface
that records issued commands and returns canned ``CommandResult``s, which
keeps these unit tests fast and free of any real SSH/Docker dependency.
"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import HostId
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import build_image_on_outer
from imbue.mngr_vps.container_setup import build_ssh_transport_for_outer
from imbue.mngr_vps.container_setup import check_directory_exists_on_outer
from imbue.mngr_vps.container_setup import check_file_exists_on_outer
from imbue.mngr_vps.container_setup import commit_container
from imbue.mngr_vps.container_setup import create_bind_volume_on_outer
from imbue.mngr_vps.container_setup import delete_btrfs_subvolume_on_outer
from imbue.mngr_vps.container_setup import docker_inspect_running
from imbue.mngr_vps.container_setup import ensure_depot_token_available
from imbue.mngr_vps.container_setup import exec_in_container
from imbue.mngr_vps.container_setup import get_outer_free_disk_gb
from imbue.mngr_vps.container_setup import install_btrfs_progs_on_outer
from imbue.mngr_vps.container_setup import is_btrfs_progs_installed_on_outer
from imbue.mngr_vps.container_setup import is_fstab_entry_present_on_outer
from imbue.mngr_vps.container_setup import is_path_mounted_on_outer
from imbue.mngr_vps.container_setup import is_retryable_rsync_error
from imbue.mngr_vps.container_setup import is_running_container_state
from imbue.mngr_vps.container_setup import prepare_btrfs_on_outer
from imbue.mngr_vps.container_setup import pull_image
from imbue.mngr_vps.container_setup import redact_secret_env
from imbue.mngr_vps.container_setup import remove_container
from imbue.mngr_vps.container_setup import remove_volume
from imbue.mngr_vps.container_setup import run_container
from imbue.mngr_vps.container_setup import run_docker
from imbue.mngr_vps.container_setup import seed_host_volume_layout_on_outer
from imbue.mngr_vps.container_setup import start_container
from imbue.mngr_vps.container_setup import stop_container
from imbue.mngr_vps.container_setup import translate_outer_concurrency_errors
from imbue.mngr_vps.docker_realizer import _read_host_id_label_from_vps
from imbue.mngr_vps.errors import ContainerSetupError
from imbue.mngr_vps.errors import VpsProvisioningError


class _Recorded(MutableModel):
    """One recorded execute_idempotent_command invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")


class _StubOuter(MutableModel):
    """Stub outer host satisfying the subset of OuterHostInterface used by these helpers.

    Records each ``execute_idempotent_command`` call and returns canned
    ``CommandResult``s from a preloaded queue (or a default success result).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responses: list[CommandResult] = Field(
        default_factory=list,
        description="FIFO of responses to return; default-success when empty",
    )
    recorded: list[_Recorded] = Field(default_factory=list, description="Each call recorded in order")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(_Recorded(command=command, timeout_seconds=timeout_seconds))
        if self.responses:
            return self.responses.pop(0)
        return CommandResult(stdout="", stderr="", success=True)

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(_Recorded(command=command, timeout_seconds=timeout_seconds))
        result = self.responses.pop(0) if self.responses else CommandResult(stdout="", stderr="", success=True)
        for line in result.stdout.splitlines():
            on_line(line)
        for line in result.stderr.splitlines():
            on_line(line)
        return result


def _outer(*responses: CommandResult) -> OuterHostInterface:
    """Build a stub outer host typed as ``OuterHostInterface`` for the helpers under test.

    The helpers only ever call ``execute_idempotent_command`` /
    ``execute_streaming_command``, so the stub doesn't need to implement the
    rest of the interface. ``cast`` is used because the stub is
    structurally-but-not-nominally an OuterHostInterface (the interface has many
    other abstract methods that aren't exercised here).
    """
    return cast(OuterHostInterface, _StubOuter(responses=list(responses)))


def _stub(outer: OuterHostInterface) -> _StubOuter:
    """Recover the underlying ``_StubOuter`` so tests can introspect ``recorded``."""
    return cast(_StubOuter, outer)


# =============================================================================
# Lightweight string helpers
# =============================================================================


def test_redact_secret_env_replaces_depot_token() -> None:
    redacted = redact_secret_env("DEPOT_TOKEN=abc123 docker build .")
    assert "abc123" not in redacted
    assert "DEPOT_TOKEN=<redacted>" in redacted


def test_redact_secret_env_passes_through_when_no_secret() -> None:
    cmd = "docker build -t my-image ."
    assert redact_secret_env(cmd) == cmd


def test_is_retryable_rsync_error_matches_known_patterns() -> None:
    assert is_retryable_rsync_error("rsync: write error: Broken pipe")
    assert is_retryable_rsync_error("ssh: connect to host 1.2.3.4 port 22: Connection refused")
    assert is_retryable_rsync_error("client_loop: send disconnect: Broken pipe")


def test_is_retryable_rsync_error_returns_false_for_other_errors() -> None:
    assert not is_retryable_rsync_error("unexpected EOF in tar header")


# =============================================================================
# docker_inspect_running
# =============================================================================


def test_is_running_container_state() -> None:
    """Only the exact ``running`` state denotes a running container -- one rule for both paths."""
    assert is_running_container_state("running") is True
    assert is_running_container_state("exited") is False
    assert is_running_container_state("paused") is False
    assert is_running_container_state(None) is False
    assert is_running_container_state("") is False


def test_docker_inspect_running_returns_true_when_running() -> None:
    outer = _outer(CommandResult(stdout="running\n", stderr="", success=True))
    assert docker_inspect_running(outer, "my-container") is True
    assert ".State.Status" in _stub(outer).recorded[0].command
    assert "my-container" in _stub(outer).recorded[0].command


def test_docker_inspect_running_returns_false_when_not_running() -> None:
    outer = _outer(CommandResult(stdout="exited\n", stderr="", success=True))
    assert docker_inspect_running(outer, "my-container") is False


def test_docker_inspect_running_returns_false_when_command_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="no such container", success=False))
    assert docker_inspect_running(outer, "missing-container") is False


# =============================================================================
# check_file_exists_on_outer
# =============================================================================


def test_check_file_exists_returns_true_when_test_succeeds() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert check_file_exists_on_outer(outer, Path("/tmp/some-file")) is True
    assert _stub(outer).recorded[0].command.startswith("test -f")


def test_check_file_exists_returns_false_when_test_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    assert check_file_exists_on_outer(outer, Path("/tmp/missing")) is False


# =============================================================================
# exec_in_container / run_docker
# =============================================================================


def test_exec_in_container_runs_docker_exec_with_quoted_command() -> None:
    outer = _outer(CommandResult(stdout="hello\n", stderr="", success=True))
    output = exec_in_container(outer, "my-container", "echo hello")
    assert output == "hello\n"
    cmd = _stub(outer).recorded[0].command
    assert "docker exec" in cmd
    assert "my-container" in cmd
    # Inner command must be properly shell-escaped (single-quoted)
    assert "'echo hello'" in cmd


def test_exec_in_container_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="permission denied", success=False))
    with pytest.raises(MngrError, match="docker exec"):
        exec_in_container(outer, "c1", "rm /etc/foo")


def test_run_docker_quotes_each_arg_separately() -> None:
    outer = _outer(CommandResult(stdout="ok\n", stderr="", success=True))
    run_docker(outer, ["volume", "inspect", "my-vol"])
    assert _stub(outer).recorded[0].command == "docker volume inspect my-vol"


def test_run_docker_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(MngrError, match="docker"):
        run_docker(outer, ["volume", "inspect", "missing-vol"])


# =============================================================================
# Container lifecycle helpers
# =============================================================================


def test_commit_container_returns_stripped_image_id() -> None:
    outer = _outer(CommandResult(stdout="sha256:abc123\n", stderr="", success=True))
    image_id = commit_container(outer, "my-container", "my-image:v1")
    assert image_id == "sha256:abc123"
    assert _stub(outer).recorded[0].command == "docker commit my-container my-image:v1"


def test_stop_container_includes_timeout_arg() -> None:
    outer = _outer()
    stop_container(outer, "my-container", timeout_seconds=5)
    assert _stub(outer).recorded[0].command == "docker stop -t 5 my-container"


def test_start_container_runs_docker_start_in_one_round_trip() -> None:
    outer = _outer()
    start_container(outer, "my-container")
    # A single `docker start` round-trip, shell-quoting the container name.
    recorded = _stub(outer).recorded
    assert len(recorded) == 1
    assert recorded[0].command == "docker start my-container"


def test_start_container_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(MngrError, match="docker start c1 failed: boom"):
        start_container(outer, "c1")


def test_remove_container_without_force() -> None:
    outer = _outer()
    remove_container(outer, "my-container", force=False)
    assert _stub(outer).recorded[0].command == "docker rm my-container"


def test_remove_container_with_force() -> None:
    outer = _outer()
    remove_container(outer, "my-container", force=True)
    assert _stub(outer).recorded[0].command == "docker rm -f my-container"


def test_remove_volume_uses_docker_volume_rm_force() -> None:
    outer = _outer()
    remove_volume(outer, "my-vol")
    assert _stub(outer).recorded[0].command == "docker volume rm -f my-vol"


def test_pull_image_uses_docker_pull_with_timeout() -> None:
    outer = _outer()
    pull_image(outer, "alpine:latest", timeout_seconds=120.0)
    assert _stub(outer).recorded[0].command == "docker pull alpine:latest"
    assert _stub(outer).recorded[0].timeout_seconds == 120.0


# =============================================================================
# run_container
# =============================================================================


def test_run_container_returns_stripped_container_id() -> None:
    outer = _outer(CommandResult(stdout="abc123def\n", stderr="", success=True))
    container_id = run_container(
        outer,
        image="alpine:latest",
        name="test-container",
        port_mappings={},
        volumes=[],
        labels={},
        extra_args=[],
        entrypoint_cmd="sleep 10",
    )
    assert container_id == "abc123def"


def test_run_container_command_includes_all_pieces() -> None:
    outer = _outer(CommandResult(stdout="cid\n", stderr="", success=True))
    run_container(
        outer,
        image="my-image:tag",
        name="my-container",
        port_mappings={"127.0.0.1:8080": "80"},
        volumes=["/host/data:/data:rw"],
        labels={"com.imbue.mngr.host-id": "host-abc"},
        extra_args=["--restart", "always"],
        entrypoint_cmd="echo hi",
    )
    cmd = _stub(outer).recorded[0].command
    assert cmd.startswith("docker run -d --name my-container")
    assert "-p 127.0.0.1:8080:80" in cmd
    assert "-v /host/data:/data:rw" in cmd
    assert "--label com.imbue.mngr.host-id=host-abc" in cmd
    assert "--restart always" in cmd
    assert "--entrypoint sh my-image:tag -c 'echo hi'" in cmd


# =============================================================================
# build_image_on_outer
# =============================================================================


def test_build_image_on_outer_with_docker_builder_streams_output() -> None:
    outer = _outer(CommandResult(stdout="step 1/2: FROM alpine\nstep 2/2: RUN ls\n", stderr="", success=True))
    received: list[str] = []
    tag = build_image_on_outer(
        outer,
        tag="my-image:v1",
        build_context_path="/tmp/build",
        docker_build_args=["--file=Dockerfile"],
        timeout_seconds=300.0,
        on_output=received.append,
        builder=DockerBuilder.DOCKER,
    )
    assert tag == "my-image:v1"
    assert "step 1/2" in received[0]
    cmd = _stub(outer).recorded[0].command
    assert cmd.startswith("docker build -t my-image:v1")
    assert "--file=Dockerfile" in cmd


def test_build_image_on_outer_raises_on_build_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="error: failed to fetch base image", success=False))
    with pytest.raises(MngrError, match="Remote docker build failed"):
        build_image_on_outer(
            outer,
            tag="bad-image",
            build_context_path="/tmp/build",
            docker_build_args=[],
            timeout_seconds=60.0,
            on_output=None,
            builder=DockerBuilder.DOCKER,
        )


def test_ensure_depot_token_available_raises_for_depot_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPOT_TOKEN", raising=False)
    with pytest.raises(MngrError, match="DEPOT_TOKEN"):
        ensure_depot_token_available(DockerBuilder.DEPOT)


def test_ensure_depot_token_available_raises_for_depot_with_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPOT_TOKEN", "")
    with pytest.raises(MngrError, match="DEPOT_TOKEN"):
        ensure_depot_token_available(DockerBuilder.DEPOT)


def test_ensure_depot_token_available_passes_for_depot_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPOT_TOKEN", "tok-123")
    ensure_depot_token_available(DockerBuilder.DEPOT)


def test_ensure_depot_token_available_is_noop_for_docker_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    # The DOCKER builder never needs a token, even when DEPOT_TOKEN is absent.
    monkeypatch.delenv("DEPOT_TOKEN", raising=False)
    ensure_depot_token_available(DockerBuilder.DOCKER)


def test_build_image_on_outer_with_depot_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPOT_TOKEN", raising=False)
    outer = _outer()
    with pytest.raises(MngrError, match="DEPOT_TOKEN"):
        build_image_on_outer(
            outer,
            tag="my-image",
            build_context_path="/tmp/build",
            docker_build_args=[],
            timeout_seconds=60.0,
            on_output=None,
            builder=DockerBuilder.DEPOT,
        )


def test_build_image_on_outer_with_depot_uses_depot_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPOT_TOKEN", "my-secret-token")
    monkeypatch.delenv("DEPOT_PROJECT_ID", raising=False)
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    tag = build_image_on_outer(
        outer,
        tag="depot-image",
        build_context_path="/tmp/build",
        docker_build_args=[],
        timeout_seconds=60.0,
        on_output=None,
        builder=DockerBuilder.DEPOT,
    )
    assert tag == "depot-image"
    cmd = _stub(outer).recorded[0].command
    # depot build runs with --load so the image lands on the daemon, invoked via
    # the resolved $DEPOT_BIN rather than a bare `depot`.
    assert '"$DEPOT_BIN" build --load -t depot-image' in cmd
    # Resolution prefers a depot already on PATH, falling back to the installer's
    # off-PATH default ($HOME/.depot/bin/depot); the install check is idempotent
    # against whichever path was resolved.
    assert 'command -v depot || echo "$HOME/.depot/bin/depot"' in cmd
    assert 'test -x "$DEPOT_BIN"' in cmd
    # Secret must NOT be inlined into the command string -- it goes via env.
    assert "my-secret-token" not in cmd


# =============================================================================
# _read_host_id_label_from_vps
# =============================================================================


def test_read_host_id_label_returns_host_id_when_container_has_label() -> None:
    """Success path: VPS hosts one mngr container, label parses to a HostId."""
    expected = HostId.generate()
    outer = _outer(CommandResult(stdout=f"{expected}\n", stderr="", success=True))
    result = _read_host_id_label_from_vps(outer)
    assert result == expected
    cmd = _stub(outer).recorded[0].command
    # Filters by the host-id label and inspects the resulting container ids.
    assert "docker ps -a -q" in cmd
    assert f"label={LABEL_HOST_ID}" in cmd
    assert "docker inspect --format" in cmd


def test_read_host_id_label_returns_none_when_no_mngr_container() -> None:
    """No mngr container on the VPS yet (e.g. concurrent create) -- return None, not raise."""
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert _read_host_id_label_from_vps(outer) is None


def test_read_host_id_label_skips_blank_lines() -> None:
    """xargs with no input can produce a trailing newline; whitespace-only lines must be skipped."""
    outer = _outer(CommandResult(stdout="\n   \n", stderr="", success=True))
    assert _read_host_id_label_from_vps(outer) is None


def test_read_host_id_label_raises_on_malformed_label() -> None:
    """A label value that isn't a valid HostId must surface as MngrError, not crash discovery."""
    outer = _outer(CommandResult(stdout="not-a-valid-host-id\n", stderr="", success=True))
    with pytest.raises(MngrError, match="malformed"):
        _read_host_id_label_from_vps(outer)


def test_read_host_id_label_raises_when_docker_ps_fails() -> None:
    """A non-zero exit from the docker ps pipeline must raise MngrError."""
    outer = _outer(CommandResult(stdout="", stderr="docker daemon not running", success=False))
    with pytest.raises(MngrError, match="Failed to list mngr containers"):
        _read_host_id_label_from_vps(outer)


# =============================================================================
# Btrfs helpers (probe + mutate primitives used by _prepare_btrfs_on_outer)
# =============================================================================


def test_is_btrfs_progs_installed_returns_true_when_command_v_succeeds() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert is_btrfs_progs_installed_on_outer(outer) is True
    assert "command -v mkfs.btrfs" in _stub(outer).recorded[0].command


def test_is_btrfs_progs_installed_returns_false_when_command_v_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="not found", success=False))
    assert is_btrfs_progs_installed_on_outer(outer) is False


def test_install_btrfs_progs_runs_apt_get_update_then_install() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    install_btrfs_progs_on_outer(outer)
    cmd = _stub(outer).recorded[0].command
    assert "apt-get update" in cmd
    assert "apt-get install -y btrfs-progs" in cmd
    assert "DEBIAN_FRONTEND=noninteractive" in cmd


def test_install_btrfs_progs_raises_on_apt_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="E: Unable to locate package btrfs-progs", success=False))
    with pytest.raises(VpsProvisioningError, match="btrfs-progs"):
        install_btrfs_progs_on_outer(outer)


def test_get_outer_free_disk_gb_parses_df_output() -> None:
    # ``df --output=avail -B 1`` prints "Avail\n<bytes>\n"; tail -n 1 leaves the bytes.
    # 37 GiB + 500 MiB of free bytes must floor-divide to 37 (NOT round up to 38).
    free_bytes = 37 * (1024**3) + 500 * (1024**2)
    outer = _outer(CommandResult(stdout=f"     {free_bytes}\n", stderr="", success=True))
    free = get_outer_free_disk_gb(outer, Path("/"))
    assert free == 37
    cmd = _stub(outer).recorded[0].command
    assert "df --output=avail -B 1 " in cmd
    assert "tail -n 1" in cmd


def test_get_outer_free_disk_gb_uses_pipefail_so_df_failure_is_surfaced() -> None:
    # Regression: without ``set -o pipefail`` the pipeline's exit status is
    # whatever ``tail -n 1`` returned (zero), masking a failing ``df`` and
    # routing the failure into the wrong VpsProvisioningError branch.
    outer = _outer(CommandResult(stdout="0\n", stderr="", success=True))
    get_outer_free_disk_gb(outer, Path("/"))
    cmd = _stub(outer).recorded[0].command
    assert "set -o pipefail" in cmd
    # The pipeline must run under bash so ``set -o pipefail`` is honored
    # (dash, the default ``/bin/sh`` on Debian, does not support it).
    assert cmd.startswith("bash -c ")


def test_get_outer_free_disk_gb_floors_to_whole_gib() -> None:
    # 1 GiB - 1 byte free must report 0, not 1, so the caller's
    # ``free_gb - reserved_gb`` math never over-allocates.
    free_bytes = (1024**3) - 1
    outer = _outer(CommandResult(stdout=f"{free_bytes}\n", stderr="", success=True))
    assert get_outer_free_disk_gb(outer, Path("/")) == 0


def test_get_outer_free_disk_gb_raises_on_shell_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="df: /: No such file", success=False))
    with pytest.raises(VpsProvisioningError, match="free disk space"):
        get_outer_free_disk_gb(outer, Path("/"))


def test_get_outer_free_disk_gb_raises_on_unparseable_output() -> None:
    outer = _outer(CommandResult(stdout="totally not an integer\n", stderr="", success=True))
    with pytest.raises(VpsProvisioningError, match="Could not parse"):
        get_outer_free_disk_gb(outer, Path("/"))


def test_is_path_mounted_returns_true_when_mountpoint_q_succeeds() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert is_path_mounted_on_outer(outer, Path("/mngr-btrfs")) is True
    assert "mountpoint -q" in _stub(outer).recorded[0].command


def test_is_path_mounted_returns_false_when_mountpoint_q_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    assert is_path_mounted_on_outer(outer, Path("/mngr-btrfs")) is False


def test_is_fstab_entry_present_returns_true_when_grep_succeeds() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert is_fstab_entry_present_on_outer(outer, Path("/var/lib/mngr-btrfs.img")) is True
    cmd = _stub(outer).recorded[0].command
    # grep -qE for the path anchored at line start and followed by whitespace;
    # re.escape escapes both the dot and the hyphen in the path.
    assert "grep -qE" in cmd
    assert "/var/lib/mngr\\-btrfs\\.img[[:space:]]" in cmd
    assert "/etc/fstab" in cmd


def test_is_fstab_entry_present_returns_false_when_grep_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    assert is_fstab_entry_present_on_outer(outer, Path("/var/lib/mngr-btrfs.img")) is False


def test_check_directory_exists_uses_test_dash_d() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert check_directory_exists_on_outer(outer, Path("/some/dir")) is True
    assert _stub(outer).recorded[0].command == "test -d /some/dir"


def test_check_directory_exists_returns_false_when_missing() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    assert check_directory_exists_on_outer(outer, Path("/missing")) is False


def test_create_bind_volume_runs_docker_volume_create_with_bind_opts() -> None:
    outer = _outer(CommandResult(stdout="my-vol\n", stderr="", success=True))
    create_bind_volume_on_outer(outer, volume_name="my-vol", device_path=Path("/mngr-btrfs/abcd"))
    cmd = _stub(outer).recorded[0].command
    assert "docker volume create" in cmd
    assert "--driver local" in cmd
    assert "--opt type=none" in cmd
    assert "--opt device=/mngr-btrfs/abcd" in cmd
    assert "--opt o=bind" in cmd
    assert "my-vol" in cmd


def test_create_bind_volume_raises_on_docker_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="volume already exists", success=False))
    with pytest.raises(MngrError, match="docker"):
        create_bind_volume_on_outer(outer, volume_name="my-vol", device_path=Path("/mngr-btrfs/abcd"))


def test_seed_host_volume_layout_mkdirs_host_dir_and_agents() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    seed_host_volume_layout_on_outer(outer, Path("/mngr-btrfs/abcd"))
    cmd = _stub(outer).recorded[0].command
    assert cmd.startswith("mkdir -p")
    assert "/mngr-btrfs/abcd/host_dir" in cmd
    assert "/mngr-btrfs/abcd/agents" in cmd


def test_seed_host_volume_layout_raises_on_mkdir_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="permission denied", success=False))
    with pytest.raises(MngrError, match="seed host volume"):
        seed_host_volume_layout_on_outer(outer, Path("/mngr-btrfs/abcd"))


def test_delete_btrfs_subvolume_is_noop_when_path_missing() -> None:
    # test -d returns failure -> the helper short-circuits without running the delete.
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    delete_btrfs_subvolume_on_outer(outer, Path("/mngr-btrfs/missing"))
    # Only one command was issued (the existence check); the delete was skipped.
    assert len(_stub(outer).recorded) == 1
    assert _stub(outer).recorded[0].command.startswith("test -d")


def test_delete_btrfs_subvolume_runs_btrfs_delete_when_path_exists() -> None:
    # Two responses in order: the test -d probe, then the btrfs subvolume delete.
    outer = _outer(
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="", success=True),
    )
    delete_btrfs_subvolume_on_outer(outer, Path("/mngr-btrfs/abcd"))
    assert len(_stub(outer).recorded) == 2
    assert "btrfs subvolume delete" in _stub(outer).recorded[1].command
    assert "/mngr-btrfs/abcd" in _stub(outer).recorded[1].command


def test_delete_btrfs_subvolume_raises_on_delete_failure() -> None:
    # First response: test -d succeeds. Second response: btrfs subvolume delete fails.
    outer = _outer(
        CommandResult(stdout="", stderr="", success=True),
        CommandResult(stdout="", stderr="ERROR: Could not destroy subvolume", success=False),
    )
    with pytest.raises(MngrError, match="btrfs subvolume delete"):
        delete_btrfs_subvolume_on_outer(outer, Path("/mngr-btrfs/abcd"))


# =============================================================================
# prepare_btrfs_on_outer (full setup pipeline)
#
# A scripted outer that picks a CommandResult per probe by matching substrings
# in the command lets each test exercise a specific "state" of the VPS
# (everything missing, everything already present, etc.) and assert on the
# resulting sequence of mutating commands.
# =============================================================================


class _ScriptedOuter(MutableModel):
    """Outer that returns canned responses keyed by substring of the issued command.

    For each ``execute_idempotent_command`` call, the first key in ``script``
    that appears in the command string supplies the response. Anything not
    matching falls back to a default success (so calls we don't care about
    sequencing for don't need a dedicated entry).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[tuple[str, CommandResult]] = Field(default_factory=list)
    recorded: list[str] = Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(command)
        for substring, response in self.script:
            if substring in command:
                return response
        return CommandResult(stdout="", stderr="", success=True)


def _scripted(script: list[tuple[str, CommandResult]]) -> OuterHostInterface:
    return cast(OuterHostInterface, _ScriptedOuter(script=script))


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", success=True)


def _fail(stderr: str = "boom") -> CommandResult:
    return CommandResult(stdout="", stderr=stderr, success=False)


_TEST_HOST_ID = HostId.generate()
_TEST_HOST_HEX = _TEST_HOST_ID.get_uuid().hex
_TEST_MOUNT_PATH = Path("/mngr-btrfs")
_TEST_LOOP_FILE = Path("/var/lib/mngr-btrfs.img")
_TEST_RESERVED_GB = 20


def _fresh_vps_script() -> list[tuple[str, CommandResult]]:
    # Every probe reports "missing/not-yet" so each gated mutating step runs.
    # Used by both fresh-vps tests below to share the canonical layout.
    # df --output=avail -B 1 returns bytes; 100 GiB worth keeps the arithmetic
    # in the assertions (free 100 - reserved 20 = 80 GiB loop file) easy to read.
    return [
        ("command -v mkfs.btrfs", _fail()),
        ("test -f /var/lib/mngr-btrfs.img", _fail()),
        ("df --output=avail", _ok(f"{100 * (1024**3)}\n")),
        ("mountpoint -q", _fail()),
        ("grep -qE", _fail()),
        (f"test -d /mngr-btrfs/{_TEST_HOST_HEX}", _fail()),
    ]


def test_prepare_btrfs_on_outer_returns_per_host_subvolume_path() -> None:
    outer = _scripted(_fresh_vps_script())
    result = prepare_btrfs_on_outer(
        outer,
        host_id=_TEST_HOST_ID,
        btrfs_mount_path=_TEST_MOUNT_PATH,
        loop_file_path=_TEST_LOOP_FILE,
        outer_disk_reserved_gb=_TEST_RESERVED_GB,
    )
    assert result == _TEST_MOUNT_PATH / _TEST_HOST_HEX


def test_prepare_btrfs_on_outer_runs_every_mutating_step_on_fresh_vps() -> None:
    outer = _scripted(_fresh_vps_script())
    prepare_btrfs_on_outer(
        outer,
        host_id=_TEST_HOST_ID,
        btrfs_mount_path=_TEST_MOUNT_PATH,
        loop_file_path=_TEST_LOOP_FILE,
        outer_disk_reserved_gb=_TEST_RESERVED_GB,
    )
    recorded = cast(_ScriptedOuter, outer).recorded
    joined = "\n".join(recorded)
    # Every mutating step must appear. Loop file size = 100GB free - 20GB reserved = 80GB.
    assert "apt-get install -y btrfs-progs" in joined
    assert "fallocate -l 80G /var/lib/mngr-btrfs.img" in joined
    assert "mkfs.btrfs /var/lib/mngr-btrfs.img" in joined
    assert "mount -o loop /var/lib/mngr-btrfs.img /mngr-btrfs" in joined
    # The fstab line goes through shlex.quote so it ends up single-quoted as one arg.
    assert "echo '/var/lib/mngr-btrfs.img  /mngr-btrfs  btrfs  loop,defaults  0 0' >> /etc/fstab" in joined
    assert f"btrfs subvolume create /mngr-btrfs/{_TEST_HOST_HEX}" in joined


def test_prepare_btrfs_on_outer_is_idempotent_when_everything_in_place() -> None:
    # All probes succeed -> every step is skipped, no mutating commands issued.
    outer = _scripted(
        [
            ("command -v mkfs.btrfs", _ok()),
            ("test -f /var/lib/mngr-btrfs.img", _ok()),
            ("mountpoint -q", _ok()),
            ("grep -qE", _ok()),
            (f"test -d /mngr-btrfs/{_TEST_HOST_HEX}", _ok()),
        ]
    )
    prepare_btrfs_on_outer(
        outer,
        host_id=_TEST_HOST_ID,
        btrfs_mount_path=_TEST_MOUNT_PATH,
        loop_file_path=_TEST_LOOP_FILE,
        outer_disk_reserved_gb=_TEST_RESERVED_GB,
    )
    recorded = cast(_ScriptedOuter, outer).recorded
    # No mutating command was issued -- only probes ran. We assert this by
    # listing the probe substrings and ensuring every recorded command starts
    # with one of them (rather than substring-searching for ``mkfs.btrfs``
    # which is also part of the ``command -v mkfs.btrfs`` probe).
    probe_prefixes = (
        "command -v",
        "test -f",
        "mkdir -p",
        "mountpoint -q",
        "grep -qE",
        "test -d",
    )
    for cmd in recorded:
        assert cmd.startswith(probe_prefixes), f"unexpected mutating command issued: {cmd!r}"


def test_prepare_btrfs_on_outer_raises_when_free_space_below_reserve() -> None:
    # Loop file missing forces the free-space check, which sees only 15 GiB < 20 GiB reserved.
    # mountpoint -q fails: a fresh VPS is not yet mounted (so the pre-mounted-btrfs
    # short-circuit does not apply and the loop-file path runs).
    outer = _scripted(
        [
            ("command -v mkfs.btrfs", _ok()),
            ("test -f /var/lib/mngr-btrfs.img", _fail()),
            ("mountpoint -q", _fail()),
            ("df --output=avail", _ok(f"{15 * (1024**3)}\n")),
        ]
    )
    with pytest.raises(VpsProvisioningError, match="Insufficient free space"):
        prepare_btrfs_on_outer(
            outer,
            host_id=_TEST_HOST_ID,
            btrfs_mount_path=_TEST_MOUNT_PATH,
            loop_file_path=_TEST_LOOP_FILE,
            outer_disk_reserved_gb=_TEST_RESERVED_GB,
        )


def test_prepare_btrfs_on_outer_raises_when_free_space_equal_to_reserve() -> None:
    # Boundary: free == reserved means loop_file_size would be 0; reject.
    # mountpoint -q fails: a fresh VPS is not yet mounted (so the pre-mounted-btrfs
    # short-circuit does not apply and the loop-file path runs).
    outer = _scripted(
        [
            ("command -v mkfs.btrfs", _ok()),
            ("test -f /var/lib/mngr-btrfs.img", _fail()),
            ("mountpoint -q", _fail()),
            ("df --output=avail", _ok(f"{20 * (1024**3)}\n")),
        ]
    )
    with pytest.raises(VpsProvisioningError, match="Insufficient free space"):
        prepare_btrfs_on_outer(
            outer,
            host_id=_TEST_HOST_ID,
            btrfs_mount_path=_TEST_MOUNT_PATH,
            loop_file_path=_TEST_LOOP_FILE,
            outer_disk_reserved_gb=_TEST_RESERVED_GB,
        )


def test_prepare_btrfs_on_outer_skips_free_space_check_when_loop_file_present() -> None:
    """Re-runs on an already-allocated VPS must not fail just because docker images filled the reserve."""
    # No "df" entry in the script: if df is called the script falls back to
    # default-success with empty stdout, which would crash the int-parse and
    # fail the test loudly.
    outer = _scripted(
        [
            ("command -v mkfs.btrfs", _ok()),
            ("test -f /var/lib/mngr-btrfs.img", _ok()),
            ("mountpoint -q", _ok()),
            ("grep -qE", _ok()),
            (f"test -d /mngr-btrfs/{_TEST_HOST_HEX}", _ok()),
        ]
    )
    prepare_btrfs_on_outer(
        outer,
        host_id=_TEST_HOST_ID,
        btrfs_mount_path=_TEST_MOUNT_PATH,
        loop_file_path=_TEST_LOOP_FILE,
        outer_disk_reserved_gb=_TEST_RESERVED_GB,
    )
    joined = "\n".join(cast(_ScriptedOuter, outer).recorded)
    assert "df --output=avail" not in joined


def test_prepare_btrfs_on_outer_skips_loop_when_btrfs_already_mounted() -> None:
    """Slice case: btrfs is already mounted (lima data disk) and no loop file exists.

    The loop-file allocation/mkfs/mount/fstab must be skipped entirely; only the
    per-host subvolume is ensured.
    """
    outer = _scripted(
        [
            ("mountpoint -q", _ok()),
            ("test -f /var/lib/mngr-btrfs.img", _fail()),
            ("command -v mkfs.btrfs", _ok()),
            (f"test -d /mngr-btrfs/{_TEST_HOST_HEX}", _fail()),
        ]
    )
    result = prepare_btrfs_on_outer(
        outer,
        host_id=_TEST_HOST_ID,
        btrfs_mount_path=_TEST_MOUNT_PATH,
        loop_file_path=_TEST_LOOP_FILE,
        outer_disk_reserved_gb=_TEST_RESERVED_GB,
    )
    assert result == _TEST_MOUNT_PATH / _TEST_HOST_HEX
    joined = "\n".join(cast(_ScriptedOuter, outer).recorded)
    # No loop-file machinery ran...
    assert "df --output=avail" not in joined
    assert "fallocate" not in joined
    assert "mount -o loop" not in joined
    assert "/etc/fstab" not in joined
    # ...but the per-host subvolume was created.
    assert f"btrfs subvolume create /mngr-btrfs/{_TEST_HOST_HEX}" in joined


# =========================================================================
# build_ssh_transport_for_outer
# =========================================================================


class _SshTransportOuter(MutableModel):
    """Minimal outer exposing only what build_ssh_transport_for_outer reads:
    get_ssh_connection_info() and connector.host.data."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    info: tuple[str, str, int, Path] | None = Field(description="(user, hostname, port, key_path) or None")
    known_hosts_file: str = Field(default="", description="value for host.data['ssh_known_hosts_file']")

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        return self.info

    @property
    def connector(self) -> Any:
        return SimpleNamespace(host=SimpleNamespace(data={"ssh_known_hosts_file": self.known_hosts_file}))


def test_build_ssh_transport_passes_the_ssh_port() -> None:
    # Regression: the lima docker-mode outer is reached via a Lima-forwarded
    # port on 127.0.0.1, so the rsync ssh transport must pass -p <port>;
    # without it ssh hits 127.0.0.1:22 and strict host-key checking fails.
    outer = _SshTransportOuter(info=("root", "127.0.0.1", 38519, Path("/k/key")), known_hosts_file="/k/known_hosts")
    ssh_cmd, user, hostname, port, key = build_ssh_transport_for_outer(cast(OuterHostInterface, outer))
    assert "-p 38519" in ssh_cmd
    assert "-o UserKnownHostsFile=/k/known_hosts" in ssh_cmd
    assert "-o StrictHostKeyChecking=yes" in ssh_cmd
    assert (user, hostname, port) == ("root", "127.0.0.1", 38519)


def test_build_ssh_transport_raises_for_local_outer() -> None:
    outer = _SshTransportOuter(info=None)
    with pytest.raises(MngrError):
        build_ssh_transport_for_outer(cast(OuterHostInterface, outer))


def test_translate_outer_concurrency_errors_wraps_concurrency_exception_group() -> None:
    # Regression for the leaked-lima-VM bug: a build/upload failure inside a
    # ConcurrencyGroup surfaces as ConcurrencyExceptionGroup, which is NOT a
    # MngrError and so escapes provider `except MngrError` cleanup clauses. The
    # boundary must convert it into a ContainerSetupError (a MngrError subclass).
    inner = MngrError("Upload failed: host key verification failed")
    with pytest.raises(ContainerSetupError) as exc_info:
        with translate_outer_concurrency_errors("upload the build context to the host"):
            raise ConcurrencyExceptionGroup("group", [inner], main_exception=inner)
    assert isinstance(exc_info.value, MngrError)
    assert "upload the build context to the host" in str(exc_info.value)
    assert "host key verification failed" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None


def test_translate_outer_concurrency_errors_wraps_process_timeout_error() -> None:
    with pytest.raises(ContainerSetupError) as exc_info:
        with translate_outer_concurrency_errors("build the image"):
            raise ProcessTimeoutError(command=("docker", "build"), stdout="", stderr="")
    assert isinstance(exc_info.value, MngrError)
    assert "build the image" in str(exc_info.value)


def test_translate_outer_concurrency_errors_passes_mngr_error_through_unwrapped() -> None:
    # A plain MngrError raised directly (not inside a ConcurrencyGroup) must not
    # be double-wrapped -- callers already handle MngrError.
    sentinel = MngrError("already a mngr error")
    with pytest.raises(MngrError) as exc_info:
        with translate_outer_concurrency_errors("do a thing"):
            raise sentinel
    assert exc_info.value is sentinel
    assert not isinstance(exc_info.value, ContainerSetupError)


def test_translate_outer_concurrency_errors_is_noop_on_success() -> None:
    results: list[int] = []
    with translate_outer_concurrency_errors("do a thing"):
        results.append(1)
    assert results == [1]
