"""Unit tests for the imbue_cloud provider instance helpers."""

import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.errors import FastPathUnavailableError
from imbue.mngr_imbue_cloud.hosts.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.providers.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.providers.instance import _resolve_fast_path_attributes


def test_resolve_fast_path_attributes_canonicalizes_remote_url_and_keeps_branch() -> None:
    resolved = _resolve_fast_path_attributes(
        LeaseAttributes(
            repo_url="git@github.com:imbue-ai/forever-claude-template.git",
            repo_branch_or_tag="v0.3.0",
            cpus=4,
        )
    )
    assert resolved.repo_url == "github.com/imbue-ai/forever-claude-template"
    assert resolved.repo_branch_or_tag == "v0.3.0"
    # Non-identity attributes are preserved.
    assert resolved.cpus == 4


@pytest.mark.parametrize(
    "attributes",
    [
        LeaseAttributes(repo_branch_or_tag="v0.3.0"),
        LeaseAttributes(repo_url="https://github.com/imbue-ai/forever-claude-template"),
        LeaseAttributes(),
    ],
)
def test_resolve_fast_path_attributes_requires_both_repo_and_branch(attributes: LeaseAttributes) -> None:
    with pytest.raises(FastPathUnavailableError):
        _resolve_fast_path_attributes(attributes)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolve_fast_path_attributes_errors_on_local_path_without_origin(tmp_path: Path) -> None:
    repo_dir = tmp_path / "no_origin"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    with pytest.raises(FastPathUnavailableError):
        _resolve_fast_path_attributes(LeaseAttributes(repo_url=str(repo_dir), repo_branch_or_tag="main"))


class _StubImbueCloudProvider(ImbueCloudProvider):
    """Test stub that supplies a tmp keypair path so we don't hit real disk paths."""

    _stub_keypair_dir: Path = Path("/tmp/stub-imbue-cloud-keypair")

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        return self._stub_keypair_dir / "ssh_key", self._stub_keypair_dir / "ssh_key.pub"


def test_build_offline_details_from_lease_preserves_host_and_failure_reason(tmp_path: Path) -> None:
    """When outer SSH is unreachable, the lease-only fallback must keep the host visible.

    Regression test for the branch's stated fix: even in the worst-case
    "no SSH at all" path, ``mngr list`` should still emit a HostDetails
    row with the SSH target populated (so the user can see what we tried
    to reach) and ``failure_reason`` carrying the underlying error.
    """
    provider_name = ProviderInstanceName("imbue-cloud-test")
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    lease = LeasedHostInfo(
        host_db_id=LeaseDbId("lease-db-id"),
        vps_address="203.0.113.42",
        ssh_port=22,
        ssh_user="user1",
        container_ssh_port=2222,
        agent_id=str(agent_id),
        host_id=str(host_id),
        host_name=str(host_id),
        attributes={},
        leased_at="2025-01-01T00:00:00Z",
    )
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(str(host_id)),
        provider_name=provider_name,
        host_state=HostState.CRASHED,
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(str(agent_id)),
        provider_name=provider_name,
    )
    failure_message = "outer SSH unreachable: connect to host 203.0.113.42 port 22: Connection timed out"
    provider = _StubImbueCloudProvider.model_construct(
        name=provider_name,
        _stub_keypair_dir=tmp_path,
    )

    host_details, agent_details_list = provider._build_offline_details_from_lease(
        host_ref=host_ref,
        agent_refs=[agent_ref],
        lease=lease,
        failure_message=failure_message,
        offline_field_generators={},
    )

    # The host is NOT dropped from the listing -- this is the primary contract.
    assert host_details.id == host_id
    # SSH info is populated from the lease so the user can see what we tried
    # to connect to.
    assert host_details.ssh is not None
    assert host_details.ssh.user == lease.ssh_user
    assert host_details.ssh.host == lease.vps_address
    assert host_details.ssh.port == lease.container_ssh_port
    # State defaults to CRASHED in the lease-only fallback (we have no
    # outer-SSH-derived state to be more specific).
    assert host_details.state == HostState.CRASHED
    # ``failure_reason`` carries the underlying error.
    assert host_details.failure_reason == failure_message
    # One agent_details per agent_ref, all attached to the offline host.
    assert len(agent_details_list) == 1
    assert agent_details_list[0].id == agent_id
    assert agent_details_list[0].host == host_details


# =============================================================================
# _release_lease_on_failure -- the reliability invariant that a failure after a
# successful lease releases the host back to the pool exactly once (so failed
# fast/slow-path builds never leak a paid lease), while a success releases
# nothing and lets the wrapped result/exception flow through untouched.
# =============================================================================


class _RecordingReleaseClient:
    """Stub connector client that records release_host calls (and reports success)."""

    def __init__(self) -> None:
        self.release_calls: list[str] = []

    def release_host(self, access_token: SecretStr, host_db_id: str) -> bool:
        self.release_calls.append(host_db_id)
        return True


class _ReleaseGuardProvider(ImbueCloudProvider):
    """Provider stub that records local-state cleanup instead of touching disk."""

    _cleanup_calls: list[HostId] = []

    def _cleanup_local_host_state(self, host_id: HostId) -> None:
        self._cleanup_calls.append(host_id)


def _make_release_guard_provider() -> tuple[_ReleaseGuardProvider, _RecordingReleaseClient]:
    client = _RecordingReleaseClient()
    provider = _ReleaseGuardProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        client=client,
        _cleanup_calls=[],
    )
    return provider, client


def test_release_lease_on_failure_releases_once_and_propagates() -> None:
    """A failure inside the guard releases the lease exactly once and re-raises the original error."""
    provider, client = _make_release_guard_provider()
    host_id = HostId.generate()
    original_error = RuntimeError("rebuild blew up")

    with pytest.raises(RuntimeError) as exc_info:
        with provider._release_lease_on_failure(SecretStr("tok"), "lease-db-id", host_id, "slow-path rebuild"):
            raise original_error

    # The ORIGINAL exception must propagate untouched (the guard uses a
    # success flag + finally, not except, so it never swallows or wraps it).
    assert exc_info.value is original_error
    # Exactly one release, against the lease's host_db_id.
    assert client.release_calls == ["lease-db-id"]
    # Local host state is cleaned up so a retry starts from a clean slate.
    assert provider._cleanup_calls == [host_id]


def test_release_lease_on_failure_does_not_release_on_success() -> None:
    """A clean exit must NOT release the lease -- the host was successfully adopted/rebuilt."""
    provider, client = _make_release_guard_provider()
    host_id = HostId.generate()

    with provider._release_lease_on_failure(SecretStr("tok"), "lease-db-id", host_id, "fast-path setup"):
        pass

    assert client.release_calls == []
    assert provider._cleanup_calls == []


# =============================================================================
# start_host -- bug C regression: a restart must re-bootstrap the container's
# SSH (relaunch sshd, re-seed the per-host authorized key, then reconcile the
# served host key) over the outer root SSH, not just ``docker start``. Without
# this, a stopped leased mind comes back with no sshd and is unrecoverable.
# =============================================================================


_RESTART_CONTAINER_ID = "container-xyz"


class _RecordingOuter(OuterHostInterface):
    """OuterHostInterface stand-in that records the docker commands start_host issues.

    ``execute_idempotent_command`` answers the container-id lookup with a fixed
    id and reports success for everything else (``docker start`` and the
    ``docker exec`` sshd-restart / authorized_keys re-seed), so start_host runs
    its real orchestration against an in-memory recorder. The remaining
    abstract methods raise so an unexpected path fails loudly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    recorded_commands: list[str] = Field(default_factory=list)

    @property
    def is_local(self) -> bool:
        return False

    def get_name(self) -> str:
        return "recording-outer"

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)
        # The container-id lookup (``docker ps -aq --filter label=...``) must
        # resolve to a concrete container so start_host proceeds.
        if command.startswith("docker ps "):
            return CommandResult(stdout=f"{_RESTART_CONTAINER_ID}\n", stderr="", success=True)
        return CommandResult(stdout="", stderr="", success=True)

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError("_RecordingOuter.execute_stateful_command not used by start_host")

    def execute_streaming_command(
        self,
        command: str,
        on_line: Callable[[str], None],
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise NotImplementedError("_RecordingOuter.execute_streaming_command not used by start_host")

    def read_file(self, path: Path) -> bytes:
        raise NotImplementedError("_RecordingOuter.read_file not used by start_host")

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        raise NotImplementedError("_RecordingOuter.read_text_file not used by start_host")

    def get_file_mtime(self, path: Path) -> datetime | None:
        raise NotImplementedError("_RecordingOuter.get_file_mtime not used by start_host")

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        raise NotImplementedError("_RecordingOuter.list_directory not used by start_host")

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        raise NotImplementedError("_RecordingOuter.write_file not used by start_host")

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        raise NotImplementedError("_RecordingOuter.write_text_file not used by start_host")

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        return None


class _RestartStubProvider(ImbueCloudProvider):
    """Provider stub that drives the real start_host against a recording outer.

    The outer-SSH connection, the host-key re-scan, and the sshd-readiness wait
    all do real network I/O, so they are replaced with recorders; everything
    start_host does in between (container lookup, ``docker start``, sshd
    relaunch, authorized_keys re-seed) runs for real against ``_outer``.
    """

    _lease: LeasedHostInfo | None = None
    _outer: OuterHostInterface | None = None
    _keypair_dir: Path = Path("/tmp/restart-stub-keypair")
    _built: ImbueCloudHost | None = None
    _waited_for: list[str] = []
    _rescanned: list[tuple[str, int]] = []

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        yield self._outer

    def _find_leased(self, host_id: HostId) -> LeasedHostInfo | None:
        return self._lease

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        return self._keypair_dir / "ssh_key", self._keypair_dir / "ssh_key.pub"

    def _wait_for_container_sshd(self, leased: LeasedHostInfo) -> None:
        self._waited_for.append(leased.vps_address)

    def _scan_and_record_container_host_key(self, host_id: HostId, vps_address: str, container_ssh_port: int) -> Path:
        self._rescanned.append((vps_address, container_ssh_port))
        return self._keypair_dir / "known_hosts"

    def _build_host_object(self, lease: LeasedHostInfo, *, adopt_pre_baked_agent: bool = True) -> ImbueCloudHost:
        assert self._built is not None
        return self._built


def _index_of(commands: list[str], substring: str) -> int:
    for index, command in enumerate(commands):
        if substring in command:
            return index
    raise AssertionError(f"no recorded command contains {substring!r}; recorded={commands}")


def test_start_host_rebootstraps_container_ssh(tmp_path: Path) -> None:
    """start_host must relaunch sshd, re-seed the authorized key, wait, then re-scan the host key.

    Regression test for bug C: the previous implementation did a bare
    ``docker start`` and returned, so a restarted leased container came back
    with no sshd (it is launched via ``docker exec``, not the entrypoint) and
    the subsequent ``mngr start`` SSH failed, leaving the mind unrecoverable.
    """
    host_id = HostId.generate()
    lease = LeasedHostInfo(
        host_db_id=LeaseDbId("lease-db-id"),
        vps_address="203.0.113.42",
        ssh_port=22,
        ssh_user="root",
        container_ssh_port=2222,
        agent_id=str(AgentId.generate()),
        host_id=str(host_id),
        host_name=str(host_id),
        attributes={},
        leased_at="2025-01-01T00:00:00Z",
    )
    # The per-host public key must be on disk for the authorized_keys re-seed.
    public_key = "ssh-ed25519 AAAApublic per-host-key"
    (tmp_path / "ssh_key.pub").write_text(public_key + "\n")

    outer = _RecordingOuter.model_construct(recorded_commands=[])
    built_host = ImbueCloudHost.model_construct()
    provider = _RestartStubProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        _lease=lease,
        _outer=outer,
        _keypair_dir=tmp_path,
        _built=built_host,
        _waited_for=[],
        _rescanned=[],
    )

    result = provider.start_host(host_id)

    commands = outer.recorded_commands
    start_index = _index_of(commands, f"docker start {_RESTART_CONTAINER_ID}")
    sshd_index = _index_of(commands, "/usr/sbin/sshd -D")
    authorized_keys_index = _index_of(commands, "authorized_keys")

    # The container is started before sshd is relaunched, and the per-host key is
    # re-seeded -- carrying the actual public key from disk.
    assert start_index < sshd_index
    assert public_key in commands[authorized_keys_index]
    # sshd is relaunched and the key re-seeded BEFORE we wait for / re-scan sshd.
    assert provider._waited_for == [lease.vps_address]
    assert provider._rescanned == [(lease.vps_address, lease.container_ssh_port)]
    # The returned host is the rebuilt host object (start_host completed).
    assert result is built_host
