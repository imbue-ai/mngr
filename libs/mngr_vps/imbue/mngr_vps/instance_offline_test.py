"""Tests for the offline VPS provider subsystem (external HostStateStore mirror)."""

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
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.host_state_store import HostStateStore
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.host_store_test import _LocalFakeOuter
from imbue.mngr_vps.host_store_test import _make_local_connector
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.interfaces import HostRealizer
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

    def _rebind_known_hosts_pre_connect(self, new_ip: str) -> None:
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
    provider._realizer_cache = cast(HostRealizer, _FakeRealizer(cast(VpsHostStore, store)))

    with pytest.raises(_MirrorCalled):
        provider.start_host(host_id)

    # The resumed record was written on-volume and then mirrored externally -- both
    # with stop_reason cleared and the fresh vps_ip.
    assert store.written is not None
    assert store.written.certified_host_data.stop_reason is None
    assert store.written.vps_ip == "10.0.0.9"
    assert provider._mirrored_record is store.written
