"""Unit tests for the imbue_cloud provider instance helpers."""

import shutil
import subprocess
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.data_types import LeaseAttributes
from imbue.mngr_imbue_cloud.data_types import LeasedHostInfo
from imbue.mngr_imbue_cloud.errors import FastPathUnavailableError
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.hosts.host import ImbueCloudHost
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.providers.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.providers.instance import _resolve_fast_path_attributes
from imbue.mngr_vps.container_setup import RUNNING_CONTAINER_STATE


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
# Restart routing + re-bootstrap: a stopped leased container must (1) resolve
# via get_host to an OFFLINE host so ensure_host_started routes ``mngr start``
# through start_host, and (2) have start_host relaunch the container's sshd over
# the outer root SSH, not just ``docker start``. The container filesystem (the
# per-host authorized key and the served host key) survives a docker stop/start,
# so only sshd -- a process launched via ``docker exec``, never the entrypoint --
# must be relaunched. Without (1), start_host is never reached; without (2), the
# container comes back with no sshd. Either way a stopped leased mind is left
# unrecoverable.
# =============================================================================


_RESTART_CONTAINER_ID = "container-xyz"


class _StubOuter(MutableModel):
    """Records the docker commands issued on the outer and returns canned results.

    Only ``execute_idempotent_command`` is exercised by the get_host probe and
    start_host (container-id lookup, ``docker inspect`` running-state probe,
    ``docker start``, and the ``docker exec`` sshd-relaunch). Following the
    sibling vps_docker provider tests, it implements just that method and is
    handed to the provider via ``cast(OuterHostInterface, ...)`` rather than
    subclassing the (large) ``OuterHostInterface``.
    """

    container_running: bool = True
    recorded_commands: list[str] = Field(default_factory=list)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded_commands.append(command)
        # The container-id lookup (``docker ps -aq --filter label=...``) resolves
        # to a concrete container so the caller proceeds.
        if command.startswith("docker ps "):
            return CommandResult(stdout=f"{_RESTART_CONTAINER_ID}\n", stderr="", success=True)
        # ``docker inspect --format '{{.State.Status}}'`` drives get_host's
        # online/offline decision (via ``is_running_container_state``), so the
        # canned output must be a container status string, not a boolean.
        if command.startswith("docker inspect"):
            status = RUNNING_CONTAINER_STATE if self.container_running else "exited"
            return CommandResult(stdout=f"{status}\n", stderr="", success=True)
        return CommandResult(stdout="", stderr="", success=True)


class _FakeImbueCloudProvider(ImbueCloudProvider):
    """Drives the real get_host / start_host logic against a canned outer.

    Overrides only the boundaries that would otherwise do real I/O: the lease
    cache, the outer-SSH connection, the on-disk keypair location, the
    sshd-readiness wait (a real network round-trip to the container), and the
    final host construction (pyinfra wiring). Everything in between -- the
    container lookup, the ``docker inspect`` running-state probe, ``docker
    start`` and the sshd relaunch -- runs for real against ``_outer``. Mirrors
    the sibling vps_docker tests.
    """

    _lease: LeasedHostInfo | None = None
    _outer: _StubOuter | None = None
    _keypair_dir: Path = Path("/tmp/fake-imbue-cloud-keypair")
    _built: ImbueCloudHost | None = None
    _waited_for: list[str] = []

    def _list_leased_hosts_cached(self) -> list[LeasedHostInfo]:
        return [self._lease] if self._lease is not None else []

    @contextmanager
    def outer_host_for(self, host_id: HostId) -> Iterator[OuterHostInterface | None]:
        yield cast(OuterHostInterface, self._outer)

    def _host_keypair_paths(self, host_id: HostId) -> tuple[Path, Path]:
        return self._keypair_dir / "ssh_key", self._keypair_dir / "ssh_key.pub"

    def _wait_for_container_sshd(self, leased: LeasedHostInfo) -> None:
        self._waited_for.append(leased.vps_address)

    def _build_host_object(self, lease: LeasedHostInfo, *, adopt_pre_baked_agent: bool = True) -> ImbueCloudHost:
        assert self._built is not None
        return self._built


def _make_lease(host_id: HostId) -> LeasedHostInfo:
    return LeasedHostInfo(
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


def _make_provider(
    lease: LeasedHostInfo,
    outer: _StubOuter,
    keypair_dir: Path,
    built: ImbueCloudHost,
    mngr_ctx: MngrContext,
) -> _FakeImbueCloudProvider:
    return _FakeImbueCloudProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        mngr_ctx=mngr_ctx,
        _lease=lease,
        _outer=outer,
        _keypair_dir=keypair_dir,
        _built=built,
        _waited_for=[],
    )


def _index_of(commands: list[str], substring: str) -> int:
    for index, command in enumerate(commands):
        if substring in command:
            return index
    raise AssertionError(f"no recorded command contains {substring!r}; recorded={commands}")


def test_get_host_returns_offline_host_when_container_stopped(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A stopped leased container must resolve to an OFFLINE host.

    This is the load-bearing routing fix: ``ensure_host_started`` only calls
    ``start_host`` when ``get_host`` returns a non-online host. The previous
    implementation returned an online ``Host`` unconditionally, so ``mngr
    start`` skipped ``start_host`` and SSHed straight into the dead container,
    leaving a stopped leased mind unrecoverable.
    """
    host_id = HostId.generate()
    lease = _make_lease(host_id)
    # The private key must be on disk so get_host actually probes the outer
    # (a missing key short-circuits to "assume running").
    (tmp_path / "ssh_key").write_text("private-key")
    outer = _StubOuter(container_running=False)
    provider = _make_provider(lease, outer, tmp_path, ImbueCloudHost.model_construct(), temp_mngr_ctx)

    host = provider.get_host(host_id)

    # Not an online Host -> ensure_host_started routes through start_host.
    assert not isinstance(host, Host)
    assert isinstance(host, OfflineHost)
    # The decision was made by actually probing the container's running state.
    assert any(command.startswith("docker inspect") for command in outer.recorded_commands)


def test_get_host_returns_online_host_when_container_running(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A running leased container resolves to an online Host (so no needless restart)."""
    host_id = HostId.generate()
    lease = _make_lease(host_id)
    (tmp_path / "ssh_key").write_text("private-key")
    outer = _StubOuter(container_running=True)
    built = ImbueCloudHost.model_construct()
    provider = _make_provider(lease, outer, tmp_path, built, temp_mngr_ctx)

    host = provider.get_host(host_id)

    assert isinstance(host, Host)
    assert host is built


def test_start_host_rebootstraps_container_ssh(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """start_host must ``docker start`` the container, relaunch its sshd, wait, then return the host.

    Regression test: a bare ``docker start`` is not enough because the
    in-container sshd is launched via ``docker exec`` (not the entrypoint), so a
    restarted leased container comes back with no sshd and the subsequent
    ``mngr start`` SSH fails, leaving the mind unrecoverable. The container
    filesystem (the per-host authorized key and the served host key) survives the
    stop/start, so neither an authorized-keys re-seed nor a host-key re-scan is
    needed -- only the sshd process must be relaunched.
    """
    host_id = HostId.generate()
    lease = _make_lease(host_id)
    outer = _StubOuter()
    built = ImbueCloudHost.model_construct()
    provider = _make_provider(lease, outer, tmp_path, built, temp_mngr_ctx)

    result = provider.start_host(host_id)

    commands = outer.recorded_commands
    start_index = _index_of(commands, f"docker start {_RESTART_CONTAINER_ID}")
    sshd_index = _index_of(commands, "/usr/sbin/sshd -D")

    # The container is started before its sshd is relaunched.
    assert start_index < sshd_index
    # We wait for sshd to come back, but do not re-seed authorized_keys -- it
    # persists in the container filesystem across a docker stop/start.
    assert provider._waited_for == [lease.vps_address]
    assert not any("authorized_keys" in command for command in commands)
    # The returned host is the rebuilt host object (start_host completed).
    assert result is built


# =============================================================================
# _list_leased_hosts_cached -- discovery-time error narrowing. A transport-level
# failure reaching the connector (flaky wifi / connector down) must surface as
# ProviderUnavailableError so recovery UIs can tell "the provider is unreachable,
# don't bother restarting" apart from auth/account problems, which keep their own
# types and fall through to the generic "can't reach your workspace" handling.
# =============================================================================


class _ListHostsClient:
    """Stub connector client whose ``list_hosts`` raises a preset exception."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def list_hosts(self, access_token: SecretStr) -> list[LeasedHostInfo]:
        raise self._error


class _DiscoveryProvider(ImbueCloudProvider):
    """Provider stub with the account/token resolution short-circuited.

    Isolates ``_list_leased_hosts_cached`` so a test can drive only the
    connector call's failure mode without standing up sessions on disk.
    """

    def _require_account(self, override: str | None = None) -> ImbueCloudAccount:
        return ImbueCloudAccount("user@example.com")

    def _get_access_token(self, account: ImbueCloudAccount) -> SecretStr:
        return SecretStr("token")


def _make_discovery_provider(list_hosts_error: Exception) -> _DiscoveryProvider:
    return _DiscoveryProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        client=_ListHostsClient(list_hosts_error),
        _leased_hosts_cache=None,
    )


def test_list_leased_hosts_maps_transport_failure_to_provider_unavailable() -> None:
    """A connection-level httpx failure becomes ProviderUnavailableError (the retry-not-restart signal)."""
    provider = _make_discovery_provider(httpx.ConnectError("Connection refused"))

    with pytest.raises(ProviderUnavailableError) as exc_info:
        provider._list_leased_hosts_cached()

    # The provider name is attributed (so mngr's errors[] carries it) and the
    # curated help text does NOT tell a cloud user to start Docker.
    assert exc_info.value.provider_name == ProviderInstanceName("imbue-cloud-test")
    assert "docker" not in (exc_info.value.user_help_text or "").lower()


def test_list_leased_hosts_preserves_auth_error() -> None:
    """An auth failure keeps its own type -- it is NOT laundered into ProviderUnavailableError."""
    provider = _make_discovery_provider(ImbueCloudAuthError("Unauthenticated (401)"))

    with pytest.raises(ImbueCloudAuthError):
        provider._list_leased_hosts_cached()


def test_ensure_host_key_pinned_does_not_clobber_a_recorded_key(temp_mngr_ctx: MngrContext) -> None:
    """A slow-path rebuilt container key (authoritatively recorded) must survive a later
    add-if-absent ensure from the connector's stale initial key."""
    provider = ImbueCloudProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"), mngr_ctx=temp_mngr_ctx
    )
    host_id = HostId.generate()
    provider._record_host_key(host_id, "203.0.113.7", 2222, "ssh-ed25519 AAAArebuiltkey")
    known_hosts_path = provider._ensure_host_key_pinned(host_id, "203.0.113.7", 2222, "ssh-ed25519 AAAAinitialkey")
    contents = known_hosts_path.read_text()
    assert "AAAArebuiltkey" in contents
    assert "AAAAinitialkey" not in contents


def test_ensure_host_key_pinned_records_connector_key_on_a_fresh_host(temp_mngr_ctx: MngrContext) -> None:
    """On a machine with no prior known_hosts entry, the connector-provided key is pinned (no scan)."""
    provider = ImbueCloudProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"), mngr_ctx=temp_mngr_ctx
    )
    host_id = HostId.generate()
    known_hosts_path = provider._ensure_host_key_pinned(host_id, "203.0.113.8", 2222, "ssh-ed25519 AAAAconnectorkey")
    assert "AAAAconnectorkey" in known_hosts_path.read_text()


def test_ensure_host_key_pinned_is_a_noop_when_key_is_none(temp_mngr_ctx: MngrContext) -> None:
    """A None key (connector too old) leaves known_hosts empty -- never trust-on-first-use."""
    provider = ImbueCloudProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"), mngr_ctx=temp_mngr_ctx
    )
    host_id = HostId.generate()
    known_hosts_path = provider._ensure_host_key_pinned(host_id, "203.0.113.9", 2222, None)
    assert known_hosts_path.read_text() == ""


def test_ensure_host_key_pinned_pins_outer_key_when_only_container_entry_exists(temp_mngr_ctx: MngrContext) -> None:
    """The outer (:22, bare-host pattern) key must still be pinned when a container
    ([host]:2222) entry is already present -- the bare host is a substring of the
    bracketed container line, so a substring check would wrongly skip it."""
    provider = ImbueCloudProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"), mngr_ctx=temp_mngr_ctx
    )
    host_id = HostId.generate()
    provider._ensure_host_key_pinned(host_id, "203.0.113.10", 2222, "ssh-ed25519 AAAAcontainerkey")
    known_hosts_path = provider._ensure_host_key_pinned(host_id, "203.0.113.10", 22, "ssh-ed25519 AAAAouterkey")
    contents = known_hosts_path.read_text()
    assert "AAAAcontainerkey" in contents
    assert "AAAAouterkey" in contents


class _FastPathGuardProvider(ImbueCloudProvider):
    """Reaches the ``fast_mode=require`` start-arg guard without real account/lease I/O."""

    _did_reach_fast_path: bool = False

    def _require_account(self, override: str | None = None) -> ImbueCloudAccount:
        return ImbueCloudAccount("tester@imbue.com")

    def _get_access_token(self, account: ImbueCloudAccount) -> SecretStr:
        return SecretStr("fake-token")

    def _create_host_fast_path(
        self,
        *,
        name: HostName,
        attributes: LeaseAttributes,
        token: SecretStr,
        region: str | None,
    ) -> Host:
        self._did_reach_fast_path = True
        return cast(Host, OfflineHost.model_construct())


# Minimal build args that select the fast (adopt) path with a valid repo identity.
_FAST_PATH_BUILD_ARGS: tuple[str, ...] = (
    "repo_url=https://github.com/imbue-ai/forever-claude-template.git",
    "repo_branch_or_tag=minds-v0.3.2",
    "fast_mode=require",
)


def _make_fast_path_guard_provider(mngr_ctx: MngrContext) -> _FastPathGuardProvider:
    return _FastPathGuardProvider.model_construct(
        name=ProviderInstanceName("imbue-cloud-test"),
        mngr_ctx=mngr_ctx,
        _did_reach_fast_path=False,
    )


def test_fast_path_allows_start_args_the_baked_container_already_carries(temp_mngr_ctx: MngrContext) -> None:
    """fast_mode=require must accept the pool_host template's docker run flags.

    The pre-baked container is already created with these, so requesting them on
    the adopt path is consistent rather than a conflict -- this is what keeps the
    fast and slow paths accepting the same start args.
    """
    provider = _make_fast_path_guard_provider(temp_mngr_ctx)
    host = provider.create_host(
        HostName("mind-test"),
        start_args=["--restart=unless-stopped", "--workdir=/", "--security-opt=no-new-privileges"],
        build_args=list(_FAST_PATH_BUILD_ARGS),
    )
    assert provider._did_reach_fast_path
    assert isinstance(host, OfflineHost)


def test_fast_path_rejects_start_args_the_baked_container_cannot_honor(temp_mngr_ctx: MngrContext) -> None:
    """A start arg outside the adoptable set still fails (the adopted container
    cannot apply it without a rebuild), and the error names only that arg."""
    provider = _make_fast_path_guard_provider(temp_mngr_ctx)
    with pytest.raises(MngrError) as exc_info:
        provider.create_host(
            HostName("mind-test"),
            start_args=["--restart=unless-stopped", "--privileged"],
            build_args=list(_FAST_PATH_BUILD_ARGS),
        )
    message = str(exc_info.value)
    assert "--privileged" in message
    assert "--restart=unless-stopped" not in message
    assert not provider._did_reach_fast_path


def test_fast_path_rejects_image_swap_and_names_only_the_image(temp_mngr_ctx: MngrContext) -> None:
    """An --image swap cannot be adopted, and with no offending start args the
    message names only the image (not an empty start-args list)."""
    provider = _make_fast_path_guard_provider(temp_mngr_ctx)
    with pytest.raises(MngrError) as exc_info:
        provider.create_host(
            HostName("mind-test"),
            image=ImageReference("ghcr.io/example/custom:latest"),
            start_args=["--restart=unless-stopped"],
            build_args=list(_FAST_PATH_BUILD_ARGS),
        )
    message = str(exc_info.value)
    assert "ghcr.io/example/custom:latest" in message
    assert "start args" not in message
    assert not provider._did_reach_fast_path
