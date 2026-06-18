"""Tests for the offline/tag-mirror VPS provider subsystem."""

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
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.offline_host import OfflineHost
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
from imbue.mngr_vps.instance_offline import AGENT_TAG_FIELDS
from imbue.mngr_vps.instance_offline import AGENT_TAG_PREFIX
from imbue.mngr_vps.instance_offline import OfflineCapableVpsProvider
from imbue.mngr_vps.instance_offline import TagHostStateStore
from imbue.mngr_vps.instance_offline import TagMirrorVpsProvider
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
    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        raise AssertionError("not exercised by this test")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        raise AssertionError("not exercised by this test")

    def _offline_discovered_host_from_instance(self, instance: Mapping[str, Any]) -> DiscoveredHost | None:
        raise AssertionError("not exercised by this test")

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        raise AssertionError("not exercised by this test")

    def _persisted_agent_dicts_from_instance(self, instance: Mapping[str, Any]) -> list[dict]:
        raise AssertionError("not exercised by this test")

    def _offline_host_from_instance(self, host_id: HostId, instance: Mapping[str, Any]) -> OfflineHost:
        raise AssertionError("not exercised by this test")

    def _mirror_agent_record(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        raise AssertionError("not exercised by this test")

    def _remove_mirrored_agent_record(self, host_id: HostId, agent_id: str) -> None:
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


# =========================================================================
# Shared TagHostStateStore: tag-removal delegation + key building
# =========================================================================


class _RecordingTagProvider(TagMirrorVpsProvider):
    """``TagMirrorVpsProvider`` stub that records the tag-removal calls the store makes.

    Only the two methods ``TagHostStateStore`` reaches are implemented; the stub is
    built via ``model_construct`` so the unrelated provider abstractmethods need no
    bodies for these store-level tests.
    """

    _instance_by_host: dict[HostId, dict[str, Any] | None] = PrivateAttr(default_factory=dict)
    _removed: list[tuple[str, list[str]]] = PrivateAttr(default_factory=list)

    def _find_instance_for_host(self, host_id: HostId) -> dict[str, Any] | None:
        return self._instance_by_host.get(host_id)

    def _remove_instance_tags(self, instance: Mapping[str, Any], keys: Sequence[str]) -> None:
        self._removed.append((str(instance["id"]), list(keys)))

    # -- abstract hooks the store-level tests do not exercise --------------------
    def _state_bucket(self) -> None:
        return None

    def _bucket_error_type(self) -> type[MngrError]:
        return MngrError

    def _bucket_label(self) -> str:
        return "test bucket"

    def _host_name_key(self) -> str:
        return "Name"

    def _is_instance_offline(self, instance: Mapping[str, Any]) -> bool:
        raise AssertionError("not exercised by this test")

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        raise AssertionError("not exercised by this test")

    def _pause_cloud_instance(self, instance_id: VpsInstanceId) -> None:
        raise AssertionError("not exercised by this test")

    def _persist_agent_to_tags(self, host_id: HostId, agent_id: str, agent_data: Mapping[str, object]) -> None:
        raise AssertionError("not exercised by this test")

    def _resume_cloud_instance(self, instance_id: VpsInstanceId) -> str:
        raise AssertionError("not exercised by this test")


def test_tag_host_state_store_remove_agent_record_builds_per_field_keys() -> None:
    """remove_agent_record resolves the instance and removes one tag per agent field."""
    host_id = HostId.generate()
    provider = _RecordingTagProvider.model_construct()
    provider._instance_by_host[host_id] = {"id": "i-abc"}
    store = TagHostStateStore.model_construct(provider=provider)

    store.remove_agent_record(host_id, "agent-7")

    expected_keys = [f"{AGENT_TAG_PREFIX}agent-7-{field}" for field in AGENT_TAG_FIELDS]
    assert provider._removed == [("i-abc", expected_keys)]


def test_tag_host_state_store_remove_agent_record_is_noop_when_instance_gone() -> None:
    """remove_agent_record makes no tag-removal call when the host's instance is absent."""
    host_id = HostId.generate()
    provider = _RecordingTagProvider.model_construct()
    provider._instance_by_host[host_id] = None
    store = TagHostStateStore.model_construct(provider=provider)

    store.remove_agent_record(host_id, "agent-7")

    assert provider._removed == []


def test_tag_host_state_store_host_record_writes_are_noops() -> None:
    """The instance's own tags carry the host record, so the host-record store ops are no-ops."""
    store: HostStateStore = TagHostStateStore.model_construct(provider=_RecordingTagProvider.model_construct())
    # Neither call resolves an instance or removes tags; they simply return.
    store.persist_host_record(cast(VpsHostRecord, object()))
    store.delete_host_state(HostId.generate())
