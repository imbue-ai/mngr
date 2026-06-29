"""Tests for the offline VPS provider subsystem (external HostStateStore mirror)."""

import threading
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import PrivateAttr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.interfaces.volume_test import InMemoryVolume
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.host_state_store import BucketHostStateStore
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_state_store import NullHostDirBackend
from imbue.mngr_vps.host_state_store import StateBucket
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.host_store_test import _LocalFakeOuter
from imbue.mngr_vps.host_store_test import _make_local_connector
from imbue.mngr_vps.instance_offline import BucketHostDirBackend
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.instance_offline import _HOST_DIR_UPLOAD_CONCURRENCY
from imbue.mngr_vps.instance_offline import _write_files_concurrently
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient

# =========================================================================
# Shared cloud stop/start lifecycle: resume mirrors the record externally
# =========================================================================


class _MirrorCalled(Exception):
    """Raised by the test's mirror spy to capture the resumed record and stop
    ``start_host`` right after the external mirror -- before the heavily
    I/O-bound ``super().start_host`` placement restart, which is out of scope."""


class _FakeRealizer:
    """Minimal ``HostRealizer`` stand-in: only the two methods the resume path
    reaches before the mirror are implemented (the rest are never called here)."""

    def __init__(self, store: VpsHostStore) -> None:
        self._store = store

    def open_host_store(self, outer: OuterHostInterface, host_id: HostId) -> VpsHostStore:
        return self._store

    def host_dir_path_on_outer(self, host_id: HostId) -> Path:
        return Path("/srv") / host_id.get_uuid().hex / "host_dir"


class _CapturingHostStore:
    """``VpsHostStore`` stand-in that serves a seeded record and captures the write."""

    def __init__(self, record: VpsHostRecord) -> None:
        self._record = record
        self.written: VpsHostRecord | None = None

    def read_host_record(self) -> VpsHostRecord | None:
        return self._record

    def write_host_record(self, host_record: VpsHostRecord) -> None:
        self.written = host_record


class _ResumeMirrorProvider(OfflineCapableVpsProvider):
    """Concrete ``OfflineCapableVpsProvider`` that stubs the resume I/O boundary so
    a test can assert the *shared* ``start_host`` mirrors the resumed record."""

    _mirrored_record: VpsHostRecord | None = PrivateAttr(default=None)

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        return "10.0.0.9"

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        return {"id": "i-resume"}

    def _rebind_known_hosts_pre_connect(self, host_id: HostId, new_ip: str) -> None:
        pass

    def _rebind_known_hosts(self, record: VpsHostRecord, new_ip: str) -> None:
        pass

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        pass

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        yield _LocalFakeOuter(id=HostId.generate(), connector=_make_local_connector())

    def _persist_host_record_externally(self, record: VpsHostRecord) -> None:
        self._mirrored_record = record
        raise _MirrorCalled

    # -- abstract hooks not exercised by the resume path under test --------------
    @property
    def _state_store(self) -> HostStateStore:
        raise AssertionError("not exercised by this test")

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        raise AssertionError("not exercised by this test")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        raise AssertionError("not exercised by this test")

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        raise AssertionError("not exercised by this test")

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        raise AssertionError("not exercised by this test")


def test_offline_resume_mirrors_record_with_cleared_stop_reason(temp_mngr_ctx: MngrContext) -> None:
    """The shared ``OfflineCapableVpsProvider.start_host`` mirrors the resumed record externally.

    Regression guard for the bug where a per-provider ``start_host`` wrote the
    resumed record on-volume but skipped ``_persist_host_record_externally``,
    leaving the external (bucket) view reporting the just-resumed host as STOPPED.
    The shared base now does the on-volume write *and* the external mirror in one
    place; here we assert that on resume it clears ``stop_reason``, rewrites
    ``vps_ip``, and mirrors that same record. Every provider inherits this, so the
    mirror can no longer be dropped from one provider's copy.
    """
    host_id = HostId.generate()
    config = VpsHostConfig(vps_instance_id=VpsInstanceId("i-resume"), region="r", plan="p")
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="resumed-host",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    store = _CapturingHostStore(VpsHostRecord(certified_host_data=certified, vps_ip=None, config=config))
    provider = _ResumeMirrorProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        vps_client=ExternallyManagedVpsClient(),
    )
    provider._realizer_cache = {IsolationMode.CONTAINER: cast(HostRealizer, _FakeRealizer(cast(VpsHostStore, store)))}

    with pytest.raises(_MirrorCalled):
        provider.start_host(host_id)

    # The resumed record was written on-volume and then mirrored externally -- both
    # with stop_reason cleared and the fresh vps_ip.
    assert store.written is not None
    assert store.written.certified_host_data.stop_reason is None
    assert store.written.vps_ip == "10.0.0.9"
    assert provider._mirrored_record is store.written


class _HostKeyWaitProvider(_ResumeMirrorProvider):
    """Records the resume readiness-wait sequence so a test can assert the resume
    path waits for the *expected* host key (not just any sshd) before connecting."""

    _calls: list[str] = PrivateAttr(default_factory=list)
    _expected_key_arg: str | None = PrivateAttr(default=None)

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        self._calls.append("wait_sshd")

    def _wait_for_expected_host_key(self, vps_ip: str, expected_host_public_key: str, timeout_seconds: float) -> None:
        self._calls.append("wait_host_key")
        self._expected_key_arg = expected_host_public_key

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        self._calls.append("open_outer")
        yield _LocalFakeOuter(id=HostId.generate(), connector=_make_local_connector())


def test_offline_resume_waits_for_expected_host_key_before_connecting(temp_mngr_ctx: MngrContext) -> None:
    """Resume must poll for mngr's *exact* VPS host key (port 22) before the strict connect.

    Regression guard for the GCP-bare resume failure: GCP's GCE startup-script
    re-runs on every boot and ``systemctl restart ssh``s partway through, so a
    plain ``wait_for_sshd`` (any-key handshake) can return while sshd is about to
    be restarted, and the strict-checked connect then hits a refused/mismatched
    port 22. The shared base now mirrors create's host-key wait on resume -- after
    ``_wait_for_sshd_on_vps`` and before ``_make_outer_for_vps_ip`` -- using the VPS
    host public key (port 22's key, which is the bare agent endpoint). Cloud-init
    backends inherit the no-op default, so they return on the first poll.
    """
    host_id = HostId.generate()
    config = VpsHostConfig(vps_instance_id=VpsInstanceId("i-resume"), region="r", plan="p")
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="resumed-host",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    store = _CapturingHostStore(VpsHostRecord(certified_host_data=certified, vps_ip=None, config=config))
    provider = _HostKeyWaitProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        vps_client=ExternallyManagedVpsClient(),
    )
    provider._realizer_cache = {IsolationMode.CONTAINER: cast(HostRealizer, _FakeRealizer(cast(VpsHostStore, store)))}
    # Materialize this host's per-host VPS host key, as create would have, so resume
    # has a key to wait for (resume reads the per-host key for this host_id).
    expected_vps_host_key = provider._get_vps_host_keypair(host_id)[1]

    with pytest.raises(_MirrorCalled):
        provider.start_host(host_id)

    # The host-key wait happens, after the sshd wait and before the strict connect.
    assert provider._calls == ["wait_sshd", "wait_host_key", "open_outer"]
    # It waits for mngr's locally-held VPS host public key (the key sshd serves on
    # port 22 -- the bare agent endpoint), not a record/account-sourced value.
    assert provider._expected_key_arg == expected_vps_host_key


# =========================================================================
# Instance-marker realizer selection (discover/connect a bare host with the
# right realizer before any on-host store is opened)
# =========================================================================


class _MarkerProvider(OfflineCapableVpsProvider):
    """Offline provider whose cached instance listing is supplied directly, so a
    test can assert the realizer is selected from the instance ``mngr-isolation``
    marker (no SSH) -- the path that makes a bare host discoverable."""

    _instances: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    @property
    def _state_store(self) -> HostStateStore:
        raise AssertionError("not exercised by this test")

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        raise AssertionError("not exercised by this test")

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        raise AssertionError("not exercised by this test")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        raise AssertionError("not exercised by this test")

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        raise AssertionError("not exercised by this test")

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        raise AssertionError("not exercised by this test")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        return self._instances


def _marker_provider(temp_mngr_ctx: MngrContext, instances: list[dict[str, Any]]) -> _MarkerProvider:
    provider = _MarkerProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        # The provider config defaults to CONTAINER isolation; the whole point is
        # that an existing bare host is still reached via the bare realizer.
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        mngr_ctx=temp_mngr_ctx,
        vps_client=ExternallyManagedVpsClient(),
    )
    provider._instances = instances
    return provider


def test_realizer_for_vps_ip_picks_bare_from_isolation_none_marker(temp_mngr_ctx: MngrContext) -> None:
    """Discovery probes a bare host's VPS with the BARE realizer when its instance
    carries ``mngr-isolation=none`` -- even though the provider config defaults to
    CONTAINER.

    This is the core fix for the connect/discovery bug: ``_read_records_from_vps``
    has only the IP (not yet the record), so it resolves the realizer from the
    instance's marker via ``_realizer_for_vps_ip``. Without this a bare host probed
    by the default container realizer finds no container and is invisible. Asserting
    the realizer type AND its port-22 endpoint captures the exact wrong behavior.
    """
    instances = [{"id": "i-bare", "main_ip": "10.0.0.5", "tags": [f"{ISOLATION_TAG_KEY}=none"]}]
    provider = _marker_provider(temp_mngr_ctx, instances)
    # The create-time default realizer is unchanged (still the container one).
    assert isinstance(provider._realizer, DockerRealizer)
    realizer = provider._realizer_for_vps_ip("10.0.0.5")
    assert isinstance(realizer, BareRealizer)
    assert realizer.agent_endpoint("10.0.0.5").port == 22


def test_realizer_for_vps_ip_picks_container_from_isolation_container_marker(temp_mngr_ctx: MngrContext) -> None:
    """An instance marked ``mngr-isolation=container`` probes with the container realizer."""
    instances = [{"id": "i-ctr", "main_ip": "10.0.0.6", "tags": [f"{ISOLATION_TAG_KEY}=container"]}]
    provider = _marker_provider(temp_mngr_ctx, instances)
    realizer = provider._realizer_for_vps_ip("10.0.0.6")
    assert isinstance(realizer, DockerRealizer)


def test_realizer_for_vps_ip_defaults_untagged_instance_to_container(temp_mngr_ctx: MngrContext) -> None:
    """A pre-marker instance (no ``mngr-isolation`` tag) defaults to the container realizer.

    Backward-compat guard: hosts created before the marker existed were all
    container placements, so an absent marker preserves the prior behavior.
    """
    instances = [{"id": "i-old", "main_ip": "10.0.0.7", "tags": ["mngr-host-id=abc"]}]
    provider = _marker_provider(temp_mngr_ctx, instances)
    realizer = provider._realizer_for_vps_ip("10.0.0.7")
    assert isinstance(realizer, DockerRealizer)


def test_realizer_for_vps_ip_falls_back_to_create_time_realizer_for_unknown_ip(temp_mngr_ctx: MngrContext) -> None:
    """An IP not in the listing (e.g. a just-created host) uses the create-time realizer."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    assert provider._realizer_for_vps_ip("10.0.0.99") is provider._realizer


def test_realizer_for_vps_ip_raises_mngr_error_on_corrupt_marker(temp_mngr_ctx: MngrContext) -> None:
    """A corrupt ``mngr-isolation`` marker raises an ``MngrError`` (not a bare ``ValueError``).

    The marker is an account-writable tag, so a corrupt value is a realistic input.
    Discovery's per-VPS error isolation only catches ``MngrError``; if marker parsing
    leaked a bare ``ValueError`` it would escape that handling and abort the whole
    discovery sweep, dropping every other VPS's hosts. Asserting ``MngrError`` here
    pins the failure into the family that per-VPS degradation handles.
    """
    instances = [{"id": "i-bad", "main_ip": "10.0.0.8", "tags": [f"{ISOLATION_TAG_KEY}=gvisor"]}]
    provider = _marker_provider(temp_mngr_ctx, instances)
    with pytest.raises(MngrError):
        provider._realizer_for_vps_ip("10.0.0.8")


# =========================================================================
# Post-finalize idle-watcher invariant (_on_host_finalized)
# =========================================================================


class _FinalizeProvider(_MarkerProvider):
    """Drives ``_on_host_finalized``: the host-record presence is injected and the
    idle-watcher install is recorded rather than run (so no SSH happens)."""

    _record: VpsHostRecord | None = PrivateAttr(default=None)
    _watcher_installed: bool = PrivateAttr(default=False)

    def _find_host_record(self, host: HostId | HostName) -> VpsHostRecord | None:
        return self._record

    def _install_idle_watcher(self, *, host_id: HostId, vps_ip: str) -> None:
        self._watcher_installed = True


def _finalize_provider(temp_mngr_ctx: MngrContext, isolation: IsolationMode) -> _FinalizeProvider:
    return _FinalizeProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test"), isolation=isolation),
        mngr_ctx=temp_mngr_ctx,
        vps_client=ExternallyManagedVpsClient(),
    )


def _record_with_config(host_id: HostId) -> VpsHostRecord:
    return VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="finalize-host",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ),
        vps_ip="10.0.0.1",
        config=VpsHostConfig(vps_instance_id=VpsInstanceId("i-finalize"), region="r", plan="p"),
    )


def test_on_host_finalized_raises_when_record_missing_blocks_watcher(temp_mngr_ctx: MngrContext) -> None:
    """A missing host record at post-finalize, when the idle watcher is due (a container
    placement that cannot stop its own host), is a broken invariant -- the record was
    just made durable. ``_on_host_finalized`` must raise (failing create, whose cleanup
    tears the VPS back down) rather than silently skip the watcher and ship a host that
    can never auto-stop on idle.
    """
    provider = _finalize_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    assert not provider._realizer.idle_shutdown_stops_host
    provider._record = None
    with pytest.raises(HostCreationError):
        provider._on_host_finalized(host_id=HostId.generate(), vps_ip="10.0.0.1")
    assert not provider._watcher_installed


def test_on_host_finalized_installs_watcher_when_record_present(temp_mngr_ctx: MngrContext) -> None:
    """With the record durable, finalize proceeds to install the watcher (no raise)."""
    provider = _finalize_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    host_id = HostId.generate()
    provider._record = _record_with_config(host_id)
    provider._on_host_finalized(host_id=host_id, vps_ip="10.0.0.1")
    assert provider._watcher_installed


def test_on_host_finalized_skips_record_check_for_self_stopping_placement(temp_mngr_ctx: MngrContext) -> None:
    """A bare placement self-stops on idle, so no host-side watcher is installed and the
    record invariant does not apply -- a missing record there must not fail finalize.
    """
    provider = _finalize_provider(temp_mngr_ctx, IsolationMode.NONE)
    assert provider._realizer.idle_shutdown_stops_host
    provider._record = None
    provider._on_host_finalized(host_id=HostId.generate(), vps_ip="10.0.0.1")
    assert not provider._watcher_installed


# =========================================================================
# rename_host re-mirrors the cheap host-name identity (_remirror_host_name)
# =========================================================================


class _RenameProvider(_MarkerProvider):
    """Captures the ``_remirror_host_name`` call ``rename_host`` makes, and stubs
    ``get_host`` so the rename path needs no reachable host."""

    _record: VpsHostRecord | None = PrivateAttr(default=None)
    _remirror_calls: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def _find_host_record(self, host: HostId | HostName) -> VpsHostRecord | None:
        return self._record

    def _remirror_host_name(self, host_record: VpsHostRecord, name: HostName) -> None:
        self._remirror_calls.append((host_record.certified_host_data.host_id, str(name)))

    def get_host(self, host: HostId | HostName) -> HostInterface:
        return cast(HostInterface, object())


def test_rename_host_remirrors_updated_name(temp_mngr_ctx: MngrContext) -> None:
    """rename_host re-stamps the cheap host-name identity (via _remirror_host_name) with
    the NEW name, so a renamed-then-stopped host stops listing under its old name. A
    stopped record (vps_ip=None) is used so the rename needs no reachable volume.
    """
    provider = _RenameProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        mngr_ctx=temp_mngr_ctx,
        vps_client=ExternallyManagedVpsClient(),
    )
    host_id = HostId.generate()
    provider._record = VpsHostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="oldname",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            stop_reason=HostState.STOPPED.value,
        ),
        vps_ip=None,
        config=VpsHostConfig(vps_instance_id=VpsInstanceId("i-rename"), region="r", plan="p"),
    )
    provider.rename_host(host_id, HostName("newname"))
    assert provider._remirror_calls == [(str(host_id), "newname")]


# =========================================================================
# Shared tag-based offline discovery default (_offline_discovered_host_from_instance)
# =========================================================================


class _TagDiscoveryProvider(_MarkerProvider):
    """Exercises the base ``_offline_discovered_host_from_instance`` over a custom name-tag key."""

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        return OfflineCapableVpsProvider._offline_discovered_host_from_instance(self, instance)

    def _host_name_tag_key(self) -> str:
        return "DisplayName"


def test_offline_discovered_host_from_instance_default_uses_name_tag_hook(temp_mngr_ctx: MngrContext) -> None:
    """The shared default reads the host id and the ``_host_name_tag_key()`` name tag (mngr- stripped)."""
    provider = _TagDiscoveryProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        mngr_ctx=temp_mngr_ctx,
        vps_client=ExternallyManagedVpsClient(),
    )
    host_id = HostId.generate()
    instance = {"id": "i-1", "tags": [f"mngr-host-id={host_id}", "DisplayName=mngr-myhost"]}
    discovered = provider._offline_discovered_host_from_instance(instance)
    assert discovered is not None
    assert discovered.host_id == host_id
    assert str(discovered.host_name) == "myhost"
    assert discovered.host_state == HostState.STOPPED


def test_offline_discovered_host_from_instance_default_returns_none_without_host_id(
    temp_mngr_ctx: MngrContext,
) -> None:
    """An instance with no ``mngr-host-id`` tag is not a mngr host (returns None)."""
    provider = _TagDiscoveryProvider(
        name=ProviderInstanceName("offline-test"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        config=VpsProviderConfig(backend=ProviderBackendName("offline-test")),
        mngr_ctx=temp_mngr_ctx,
        vps_client=ExternallyManagedVpsClient(),
    )
    assert provider._offline_discovered_host_from_instance({"id": "i-2", "tags": ["DisplayName=mngr-x"]}) is None


# =========================================================================
# Shared bucket-store / host_dir-backend selection helpers
# =========================================================================


class _FakeStateBucket:
    """``StateBucket`` Protocol stand-in (structurally satisfies it; no method is exercised here)."""

    def write_host_record_json(self, host_id: HostId, record_json: str) -> None:
        raise AssertionError("not exercised")

    def read_host_record_json(self, host_id: HostId) -> str | None:
        raise AssertionError("not exercised")

    def write_agent_record(self, host_id: HostId, agent_id: str, data: Mapping[str, object]) -> None:
        raise AssertionError("not exercised")

    def list_agent_records(self, host_id: HostId) -> list[dict]:
        raise AssertionError("not exercised")

    def remove_agent_record(self, host_id: HostId, agent_id: str) -> None:
        raise AssertionError("not exercised")

    def delete_host_state(self, host_id: HostId) -> None:
        raise AssertionError("not exercised")

    def host_dir_prefix_has_objects(self, host_id: HostId) -> bool:
        raise AssertionError("not exercised")

    def volume_for_host(self, host_id: HostId) -> Volume:
        raise AssertionError("not exercised")


def test_select_bucket_store_builds_bucket_store_when_present(temp_mngr_ctx: MngrContext) -> None:
    """``_select_bucket_store`` wraps a present bucket in a ``BucketHostStateStore`` with the label."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    bucket = cast(StateBucket, _FakeStateBucket())
    store = provider._select_bucket_store(bucket, store_label="Test state bucket", prepare_command="mngr test prepare")
    assert isinstance(store, BucketHostStateStore)
    assert store.bucket is bucket
    assert store.bucket_label == "Test state bucket"


def test_select_bucket_store_raises_actionable_error_when_absent(temp_mngr_ctx: MngrContext) -> None:
    """A ``None`` bucket raises the actionable prepare-pointer error (the bucket is required)."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    with pytest.raises(MngrError, match="mngr test prepare"):
        provider._select_bucket_store(None, store_label="Test state bucket", prepare_command="mngr test prepare")


def test_select_bucket_host_dir_backend_is_bucket_backed_when_enabled_and_present(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Enabled + a present bucket selects the bucket-backed host_dir backend bound to that bucket."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    bucket = cast(StateBucket, _FakeStateBucket())
    backend = provider._select_bucket_host_dir_backend(bucket, enabled=True)
    assert isinstance(backend, BucketHostDirBackend)
    assert backend.bucket is bucket
    assert backend.provider is provider


def test_select_bucket_host_dir_backend_is_null_when_disabled(temp_mngr_ctx: MngrContext) -> None:
    """The feature flag off selects the no-op backend even when a bucket is present."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    bucket = cast(StateBucket, _FakeStateBucket())
    assert isinstance(provider._select_bucket_host_dir_backend(bucket, enabled=False), NullHostDirBackend)


def test_select_bucket_host_dir_backend_is_null_when_no_bucket(temp_mngr_ctx: MngrContext) -> None:
    """No bucket selects the no-op backend even when the feature flag is on."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    assert isinstance(provider._select_bucket_host_dir_backend(None, enabled=True), NullHostDirBackend)


# =========================================================================
# Shared known_hosts add helper (resume rebind)
# =========================================================================


def test_add_known_hosts_for_ip_adds_both_endpoints(temp_mngr_ctx: MngrContext) -> None:
    """Both keys present: the VPS (port 22) and container (config port) endpoints are added."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    provider._add_known_hosts_for_ip(
        "10.0.0.5", vps_public_key="ssh-ed25519 AAAAVPS", container_public_key="ssh-ed25519 AAAACTR"
    )
    vps_lines = provider._vps_known_hosts_path().read_text()
    container_lines = provider._container_known_hosts_path().read_text()
    assert "10.0.0.5 ssh-ed25519 AAAAVPS" in vps_lines
    assert f"[10.0.0.5]:{provider.config.container_ssh_port} ssh-ed25519 AAAACTR" in container_lines


def test_add_known_hosts_for_ip_skips_endpoint_with_absent_key(temp_mngr_ctx: MngrContext) -> None:
    """An absent (None) key skips that endpoint and leaves its known_hosts file untouched."""
    provider = _marker_provider(temp_mngr_ctx, instances=[])
    provider._add_known_hosts_for_ip("10.0.0.6", vps_public_key="ssh-ed25519 AAAAVPS", container_public_key=None)
    assert "10.0.0.6 ssh-ed25519 AAAAVPS" in provider._vps_known_hosts_path().read_text()
    assert not provider._container_known_hosts_path().exists()


# =========================================================================
# Concurrent host_dir capture upload (_write_files_concurrently)
# =========================================================================


def test_write_files_concurrently_overlaps_writers_and_persists_all_files() -> None:
    """All files land regardless of chunking, and the per-file writes genuinely overlap."""
    file_count = _HOST_DIR_UPLOAD_CONCURRENCY * 3 + 1
    files = {f"projects/repo/.git/objects/{i:04d}": f"obj-{i}".encode() for i in range(file_count)}
    worker_count = min(_HOST_DIR_UPLOAD_CONCURRENCY, file_count)
    lock = threading.Lock()
    written: dict[str, bytes] = {}
    # Every worker must rendezvous at the barrier before any proceeds, so the upload
    # completes only if the workers truly run concurrently; a serialized
    # implementation would block the first worker forever and time out here.
    barrier = threading.Barrier(worker_count, timeout=30)

    class _BarrierVolume(InMemoryVolume):
        def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
            barrier.wait()
            with lock:
                written.update(file_contents_by_path)

    _write_files_concurrently(_BarrierVolume(), files)

    assert written == files


def test_write_files_concurrently_empty_is_a_noop() -> None:
    """An empty mapping writes nothing and spawns no workers."""
    volume = InMemoryVolume()
    _write_files_concurrently(volume, {})
    assert volume.files == {}


def test_write_files_concurrently_surfaces_worker_failure() -> None:
    """A failure in any worker propagates out of the concurrent upload."""
    files: dict[str, bytes] = {f"f{i}": b"x" for i in range(_HOST_DIR_UPLOAD_CONCURRENCY * 2)}
    files["dir/boom"] = b"x"

    class _FailingVolume(InMemoryVolume):
        def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
            if any("boom" in path for path in file_contents_by_path):
                raise MngrError("upload failed")
            self.files.update(file_contents_by_path)

    with pytest.raises(MngrError, match="upload failed"):
        _write_files_concurrently(_FailingVolume(), files)
