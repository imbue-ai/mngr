"""Unit tests for the module-level outer-host docker helpers in instance.py.

These helpers were extracted when DockerOverSsh was deleted; they wrap docker
commands that run on an outer host. The tests use a stub OuterHostInterface
that records issued commands and returns canned ``CommandResult``s, which
keeps these unit tests fast and free of any real SSH/Docker dependency.
"""

from collections.abc import Callable
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr_vps_docker.instance import _build_image_on_outer
from imbue.mngr_vps_docker.instance import _check_file_exists_on_outer
from imbue.mngr_vps_docker.instance import _commit_container
from imbue.mngr_vps_docker.instance import _create_volume
from imbue.mngr_vps_docker.instance import _docker_inspect_running
from imbue.mngr_vps_docker.instance import _exec_in_container
from imbue.mngr_vps_docker.instance import _is_retryable_rsync_error
from imbue.mngr_vps_docker.instance import _pull_image
from imbue.mngr_vps_docker.instance import _redact_secret_env
from imbue.mngr_vps_docker.instance import _remove_container
from imbue.mngr_vps_docker.instance import _remove_volume
from imbue.mngr_vps_docker.instance import _run_container
from imbue.mngr_vps_docker.instance import _run_docker
from imbue.mngr_vps_docker.instance import _start_container
from imbue.mngr_vps_docker.instance import _stop_container


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
    redacted = _redact_secret_env("DEPOT_TOKEN=abc123 docker build .")
    assert "abc123" not in redacted
    assert "DEPOT_TOKEN=<redacted>" in redacted


def test_redact_secret_env_passes_through_when_no_secret() -> None:
    cmd = "docker build -t my-image ."
    assert _redact_secret_env(cmd) == cmd


def test_is_retryable_rsync_error_matches_known_patterns() -> None:
    assert _is_retryable_rsync_error("rsync: write error: Broken pipe")
    assert _is_retryable_rsync_error("ssh: connect to host 1.2.3.4 port 22: Connection refused")
    assert _is_retryable_rsync_error("client_loop: send disconnect: Broken pipe")


def test_is_retryable_rsync_error_returns_false_for_other_errors() -> None:
    assert not _is_retryable_rsync_error("unexpected EOF in tar header")


# =============================================================================
# _docker_inspect_running
# =============================================================================


def test_docker_inspect_running_returns_true_when_running() -> None:
    outer = _outer(CommandResult(stdout="true\n", stderr="", success=True))
    assert _docker_inspect_running(outer, "my-container") is True
    assert "docker inspect --format" in _stub(outer).recorded[0].command
    assert "my-container" in _stub(outer).recorded[0].command


def test_docker_inspect_running_returns_false_when_not_running() -> None:
    outer = _outer(CommandResult(stdout="false\n", stderr="", success=True))
    assert _docker_inspect_running(outer, "my-container") is False


def test_docker_inspect_running_returns_false_when_command_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="no such container", success=False))
    assert _docker_inspect_running(outer, "missing-container") is False


# =============================================================================
# _check_file_exists_on_outer
# =============================================================================


def test_check_file_exists_returns_true_when_test_succeeds() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    assert _check_file_exists_on_outer(outer, "/tmp/some-file") is True
    assert _stub(outer).recorded[0].command.startswith("test -f")


def test_check_file_exists_returns_false_when_test_fails() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=False))
    assert _check_file_exists_on_outer(outer, "/tmp/missing") is False


# =============================================================================
# _exec_in_container / _run_docker
# =============================================================================


def test_exec_in_container_runs_docker_exec_with_quoted_command() -> None:
    outer = _outer(CommandResult(stdout="hello\n", stderr="", success=True))
    output = _exec_in_container(outer, "my-container", "echo hello")
    assert output == "hello\n"
    cmd = _stub(outer).recorded[0].command
    assert "docker exec" in cmd
    assert "my-container" in cmd
    # Inner command must be properly shell-escaped (single-quoted)
    assert "'echo hello'" in cmd


def test_exec_in_container_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="permission denied", success=False))
    with pytest.raises(MngrError, match="docker exec"):
        _exec_in_container(outer, "c1", "rm /etc/foo")


def test_run_docker_quotes_each_arg_separately() -> None:
    outer = _outer(CommandResult(stdout="ok\n", stderr="", success=True))
    _run_docker(outer, ["volume", "inspect", "my-vol"])
    assert _stub(outer).recorded[0].command == "docker volume inspect my-vol"


def test_run_docker_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(MngrError, match="docker"):
        _run_docker(outer, ["volume", "inspect", "missing-vol"])


# =============================================================================
# Container lifecycle helpers
# =============================================================================


def test_commit_container_returns_stripped_image_id() -> None:
    outer = _outer(CommandResult(stdout="sha256:abc123\n", stderr="", success=True))
    image_id = _commit_container(outer, "my-container", "my-image:v1")
    assert image_id == "sha256:abc123"
    assert _stub(outer).recorded[0].command == "docker commit my-container my-image:v1"


def test_stop_container_includes_timeout_arg() -> None:
    outer = _outer()
    _stop_container(outer, "my-container", timeout_seconds=5)
    assert _stub(outer).recorded[0].command == "docker stop -t 5 my-container"


def test_start_container_uses_docker_start() -> None:
    outer = _outer()
    _start_container(outer, "my-container")
    assert _stub(outer).recorded[0].command == "docker start my-container"


def test_remove_container_without_force() -> None:
    outer = _outer()
    _remove_container(outer, "my-container", force=False)
    assert _stub(outer).recorded[0].command == "docker rm my-container"


def test_remove_container_with_force() -> None:
    outer = _outer()
    _remove_container(outer, "my-container", force=True)
    assert _stub(outer).recorded[0].command == "docker rm -f my-container"


def test_create_volume_uses_docker_volume_create() -> None:
    outer = _outer()
    _create_volume(outer, "my-vol")
    assert _stub(outer).recorded[0].command == "docker volume create my-vol"


def test_remove_volume_uses_docker_volume_rm_force() -> None:
    outer = _outer()
    _remove_volume(outer, "my-vol")
    assert _stub(outer).recorded[0].command == "docker volume rm -f my-vol"


def test_pull_image_uses_docker_pull_with_timeout() -> None:
    outer = _outer()
    _pull_image(outer, "alpine:latest", timeout_seconds=120.0)
    assert _stub(outer).recorded[0].command == "docker pull alpine:latest"
    assert _stub(outer).recorded[0].timeout_seconds == 120.0


# =============================================================================
# _run_container
# =============================================================================


def test_run_container_returns_stripped_container_id() -> None:
    outer = _outer(CommandResult(stdout="abc123def\n", stderr="", success=True))
    container_id = _run_container(
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
    _run_container(
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
# _build_image_on_outer
# =============================================================================


def test_build_image_on_outer_with_docker_builder_streams_output() -> None:
    outer = _outer(CommandResult(stdout="step 1/2: FROM alpine\nstep 2/2: RUN ls\n", stderr="", success=True))
    received: list[str] = []
    tag = _build_image_on_outer(
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
        _build_image_on_outer(
            outer,
            tag="bad-image",
            build_context_path="/tmp/build",
            docker_build_args=[],
            timeout_seconds=60.0,
            on_output=None,
            builder=DockerBuilder.DOCKER,
        )


def test_build_image_on_outer_with_depot_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPOT_TOKEN", raising=False)
    outer = _outer()
    with pytest.raises(MngrError, match="DEPOT_TOKEN"):
        _build_image_on_outer(
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
    tag = _build_image_on_outer(
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
    # Depot install + depot build, with --load (so the image lands on the daemon)
    assert "depot build --load -t depot-image" in cmd
    # Secret must NOT be inlined into the command string -- it goes via env.
    assert "my-secret-token" not in cmd
