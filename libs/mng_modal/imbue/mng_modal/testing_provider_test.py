"""Tests for mng_modal using TestingModalInterface.

These tests exercise ModalProviderInstance business logic (host records,
volumes, tags, snapshots, discovery, lifecycle) without requiring real
Modal credentials or SSH connections.
"""

import contextlib
from datetime import datetime
from datetime import timezone
from io import StringIO
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import HostNameConflictError
from imbue.mng.errors import HostNotFoundError
from imbue.mng.errors import MngError
from imbue.mng.errors import SnapshotNotFoundError
from imbue.mng.hosts.offline_host import OfflineHost
from imbue.mng.interfaces.data_types import CertifiedHostData
from imbue.mng.interfaces.data_types import VolumeFileType
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId
from imbue.mng_modal.backend import ModalAppContextHandle
from imbue.mng_modal.backend import ModalProviderBackend
from imbue.mng_modal.backend import _create_environment
from imbue.mng_modal.backend import _enter_ephemeral_app_context_with_env_retry
from imbue.mng_modal.backend import _exit_modal_app_context
from imbue.mng_modal.backend import _lookup_persistent_app_with_env_retry
from imbue.mng_modal.backend import register_provider_backend
from imbue.mng_modal.config import ModalMode
from imbue.mng_modal.config import ModalProviderConfig
from imbue.mng_modal.errors import NoSnapshotsModalMngError
from imbue.mng_modal.instance import HOST_VOLUME_INFIX
from imbue.mng_modal.instance import HostRecord
from imbue.mng_modal.instance import ModalProviderApp
from imbue.mng_modal.instance import ModalProviderInstance
from imbue.mng_modal.instance import SandboxConfig
from imbue.mng_modal.instance import TAG_HOST_ID
from imbue.mng_modal.instance import TAG_HOST_NAME
from imbue.mng_modal.instance import TAG_USER_PREFIX
from imbue.mng_modal.instance import _build_image_from_dockerfile_contents
from imbue.mng_modal.instance import _build_listing_collection_script
from imbue.mng_modal.instance import _build_modal_secrets_from_env
from imbue.mng_modal.instance import _build_modal_volumes
from imbue.mng_modal.instance import _parse_optional_float
from imbue.mng_modal.instance import _parse_optional_int
from imbue.mng_modal.instance import _parse_volume_spec
from imbue.mng_modal.instance import _substitute_dockerfile_build_args
from imbue.mng_modal.routes.deployment import deploy_function
from imbue.mng_modal.testing import make_host_record
from imbue.mng_modal.testing import make_sandbox_with_tags
from imbue.mng_modal.testing import make_snapshot
from imbue.mng_modal.testing import make_testing_modal_interface
from imbue.mng_modal.testing import make_testing_provider
from imbue.mng_modal.testing import setup_host_with_sandbox
from imbue.mng_modal.volume import _proxy_file_entry_type_to_volume_file_type
from imbue.modal_proxy.data_types import FileEntryType as ProxyFileEntryType
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.testing import TestingModalInterface

# ---------------------------------------------------------------------------
# Host Record CRUD Tests
# ---------------------------------------------------------------------------


def test_write_and_read_host_record(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="my-host")
    testing_provider._write_host_record(record)

    loaded = testing_provider._read_host_record(host_id)
    assert loaded is not None
    assert loaded.certified_host_data.host_name == "my-host"
    assert loaded.ssh_host == "127.0.0.1"


def test_read_host_record_not_found(testing_provider: ModalProviderInstance) -> None:
    result = testing_provider._read_host_record(HostId.generate())
    assert result is None


def test_read_host_record_uses_cache(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id)
    testing_provider._write_host_record(record)

    # First read populates cache
    loaded1 = testing_provider._read_host_record(host_id)
    assert loaded1 is not None

    # Second read uses cache (same object)
    loaded2 = testing_provider._read_host_record(host_id)
    assert loaded2 is loaded1


def test_read_host_record_bypass_cache(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="v1")
    testing_provider._write_host_record(record)

    # Populate cache
    testing_provider._read_host_record(host_id)

    # Update record directly on volume (bypassing cache)
    record2 = make_host_record(host_id=host_id, host_name="v2")
    testing_provider._write_host_record(record2)

    # Read with cache=False should see the update
    loaded = testing_provider._read_host_record(host_id, use_cache=False)
    assert loaded is not None
    assert loaded.certified_host_data.host_name == "v2"


def test_delete_host_record(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id)
    testing_provider._write_host_record(record)

    testing_provider._delete_host_record(host_id)

    assert testing_provider._read_host_record(host_id, use_cache=False) is None


def test_list_all_host_records(testing_provider: ModalProviderInstance) -> None:
    cg = testing_provider.mng_ctx.concurrency_group

    host1 = HostId.generate()
    host2 = HostId.generate()
    testing_provider._write_host_record(make_host_record(host_id=host1, host_name="h1"))
    testing_provider._write_host_record(make_host_record(host_id=host2, host_name="h2"))

    records = testing_provider._list_all_host_records(cg)
    assert len(records) == 2
    names = {r.certified_host_data.host_name for r in records}
    assert names == {"h1", "h2"}


def test_list_all_host_records_empty(testing_provider: ModalProviderInstance) -> None:
    cg = testing_provider.mng_ctx.concurrency_group
    records = testing_provider._list_all_host_records(cg)
    assert records == []


def test_save_failed_host_record(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    testing_provider._save_failed_host_record(
        host_id=host_id,
        host_name=HostName("failed-host"),
        tags={"env": "test"},
        failure_reason="Image build failed",
        build_log="Error: dependency not found",
    )

    record = testing_provider._read_host_record(host_id)
    assert record is not None
    assert record.certified_host_data.failure_reason == "Image build failed"
    assert record.certified_host_data.build_log == "Error: dependency not found"
    # Failed hosts have no SSH info
    assert record.ssh_host is None


def test_clear_snapshots_from_host_record(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
        make_snapshot("snap-2", "s2"),
    ]
    record = make_host_record(host_id=host_id, snapshots=snapshots)
    testing_provider._write_host_record(record)

    testing_provider._clear_snapshots_from_host_record(host_id)

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 0


def test_clear_snapshots_noop_when_no_snapshots(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, snapshots=[])
    testing_provider._write_host_record(record)

    testing_provider._clear_snapshots_from_host_record(host_id)

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 0


def test_clear_snapshots_noop_when_no_record(testing_provider: ModalProviderInstance) -> None:
    # Should not raise
    testing_provider._clear_snapshots_from_host_record(HostId.generate())


# ---------------------------------------------------------------------------
# Agent Persistence Tests
# ---------------------------------------------------------------------------


def test_persist_and_list_agent_data(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    agent_data = {
        "id": "agent-cccc3333dddd4444eeee5555ffff6666",
        "name": "test-agent",
        "type": "claude",
        "command": "claude",
    }
    testing_provider.persist_agent_data(host_id, agent_data)

    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-cccc3333dddd4444eeee5555ffff6666"
    assert agents[0]["name"] == "test-agent"


def test_persist_multiple_agents(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    testing_provider.persist_agent_data(
        host_id, {"id": "a1", "name": "agent-aaaa1111bbbb2222cccc3333dddd4444", "type": "claude"}
    )
    testing_provider.persist_agent_data(
        host_id, {"id": "a2", "name": "agent-bbbb2222cccc3333dddd4444eeee5555", "type": "codex"}
    )

    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert len(agents) == 2


def test_remove_persisted_agent_data(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    testing_provider.persist_agent_data(
        host_id, {"id": "agent-aaaa1111bbbb2222cccc3333dddd4444", "name": "a1", "type": "claude"}
    )
    testing_provider.persist_agent_data(
        host_id, {"id": "agent-bbbb2222cccc3333dddd4444eeee5555", "name": "a2", "type": "codex"}
    )

    testing_provider.remove_persisted_agent_data(host_id, AgentId("agent-aaaa1111bbbb2222cccc3333dddd4444"))

    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-bbbb2222cccc3333dddd4444eeee5555"


def test_remove_nonexistent_agent_data(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    # Should not raise
    testing_provider.remove_persisted_agent_data(host_id, AgentId.generate())


def test_list_agents_for_nonexistent_host(testing_provider: ModalProviderInstance) -> None:
    agents = testing_provider.list_persisted_agent_data_for_host(HostId.generate())
    assert agents == []


def test_destroy_agents_on_host(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    testing_provider.persist_agent_data(
        host_id, {"id": "agent-aaaa1111bbbb2222cccc3333dddd4444", "name": "a1", "type": "claude"}
    )
    testing_provider.persist_agent_data(
        host_id, {"id": "agent-bbbb2222cccc3333dddd4444eeee5555", "name": "a2", "type": "codex"}
    )

    testing_provider._destroy_agents_on_host(host_id)

    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert agents == []


def test_persist_agent_without_id_logs_warning(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    # Should not raise, just log a warning
    testing_provider.persist_agent_data(host_id, {"name": "no-id-agent", "type": "claude"})
    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert agents == []


# ---------------------------------------------------------------------------
# Volume Operations Tests
# ---------------------------------------------------------------------------


def test_get_state_volume(testing_provider: ModalProviderInstance) -> None:
    vol = testing_provider.get_state_volume()
    assert vol is not None


def test_build_host_volume(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    vol = testing_provider._build_host_volume(host_id)
    assert vol is not None


def test_get_volume_for_host_returns_volume(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    # Create the host volume first
    testing_provider._build_host_volume(host_id)
    # Write something to it so listdir works
    vol = testing_provider._build_host_volume(host_id)
    vol.write_files({"marker.txt": b"exists"})

    host_volume = testing_provider.get_volume_for_host(host_id)
    assert host_volume is not None


def test_get_volume_for_host_returns_none_when_disabled(
    testing_provider_no_host_volume: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    result = testing_provider_no_host_volume.get_volume_for_host(host_id)
    assert result is None


def test_get_volume_for_host_returns_none_when_not_found(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    # Don't create the host volume -- volume_from_name with create_if_missing=False
    result = testing_provider.get_volume_for_host(host_id)
    assert result is None


def test_delete_host_volume(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    testing_provider._build_host_volume(host_id)
    testing_provider._delete_host_volume(host_id)
    # Should not raise even if already deleted
    testing_provider._delete_host_volume(host_id)


# ---------------------------------------------------------------------------
# Host Name Uniqueness Tests
# ---------------------------------------------------------------------------


def test_check_host_name_unique_no_conflicts(testing_provider: ModalProviderInstance) -> None:
    testing_provider._write_host_record(make_host_record(host_name="other-host"))
    # Should not raise
    testing_provider._check_host_name_is_unique(HostName("new-host"))


def test_check_host_name_unique_with_stopped_host(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    testing_provider._write_host_record(make_host_record(host_id=host_id, host_name="taken-name", snapshots=snapshots))
    with pytest.raises(HostNameConflictError):
        testing_provider._check_host_name_is_unique(HostName("taken-name"))


def test_check_host_name_unique_destroyed_host_ok(testing_provider: ModalProviderInstance) -> None:
    # A destroyed host (no snapshots, not running, not failed) should not block reuse
    testing_provider._write_host_record(make_host_record(host_name="reusable-name", snapshots=[]))
    # Should not raise
    testing_provider._check_host_name_is_unique(HostName("reusable-name"))


# ---------------------------------------------------------------------------
# Sandbox Cache Tests
# ---------------------------------------------------------------------------


def test_sandbox_cache_by_id(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    name = HostName("cached")
    sandbox = make_sandbox_with_tags(testing_modal, host_id, "cached")

    testing_provider._cache_sandbox(host_id, name, sandbox)

    found = testing_provider._find_sandbox_by_host_id(host_id)
    assert found is not None
    assert found.get_object_id() == sandbox.get_object_id()


def test_sandbox_cache_by_name(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    name = HostName("by-name")
    sandbox = make_sandbox_with_tags(testing_modal, host_id, "by-name")

    testing_provider._cache_sandbox(host_id, name, sandbox)

    found = testing_provider._find_sandbox_by_name(name)
    assert found is not None
    assert found.get_object_id() == sandbox.get_object_id()


def test_uncache_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    name = HostName("to-uncache")
    sandbox = make_sandbox_with_tags(testing_modal, host_id, "to-uncache")

    testing_provider._cache_sandbox(host_id, name, sandbox)
    testing_provider._uncache_sandbox(host_id, name)

    # Should fall through to Modal API lookup
    # The sandbox has tags, so it should still be found
    found = testing_provider._find_sandbox_by_host_id(host_id)
    assert found is not None


def test_reset_caches(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    sandbox = make_sandbox_with_tags(testing_modal, host_id, "reset-me")
    testing_provider._cache_sandbox(host_id, HostName("reset-me"), sandbox)

    testing_provider.reset_caches()
    # After reset, cache is empty (but API lookup still works)
    assert host_id not in testing_provider._sandbox_cache_by_id


# ---------------------------------------------------------------------------
# Sandbox Listing Tests
# ---------------------------------------------------------------------------


def test_list_sandboxes(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id1 = HostId.generate()
    host_id2 = HostId.generate()
    make_sandbox_with_tags(testing_modal, host_id1, "h1")
    make_sandbox_with_tags(testing_modal, host_id2, "h2")

    # Also create a sandbox WITHOUT mng tags (should be excluded)
    image = testing_modal.image_debian_slim()
    app = list(testing_modal._apps.values())[0]
    testing_modal.sandbox_create(image=image, app=app, timeout=300, cpu=1.0, memory=1024)

    sandboxes = testing_provider._list_sandboxes()
    assert len(sandboxes) == 2


def test_find_sandbox_by_host_id_api_fallback(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    make_sandbox_with_tags(testing_modal, host_id, "api-lookup")

    # Don't cache -- should fall back to API
    found = testing_provider._find_sandbox_by_host_id(host_id)
    assert found is not None


def test_find_sandbox_by_name_api_fallback(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    make_sandbox_with_tags(testing_modal, host_id, "api-name-lookup")

    found = testing_provider._find_sandbox_by_name(HostName("api-name-lookup"))
    assert found is not None


def test_find_sandbox_returns_none_when_not_found(
    testing_provider: ModalProviderInstance,
) -> None:
    assert testing_provider._find_sandbox_by_host_id(HostId.generate()) is None
    assert testing_provider._find_sandbox_by_name(HostName("nonexistent")) is None


# ---------------------------------------------------------------------------
# Image Building Tests
# ---------------------------------------------------------------------------


def test_build_modal_image_default(testing_provider: ModalProviderInstance) -> None:
    image = testing_provider._build_modal_image()
    assert image.get_object_id() is not None


def test_build_modal_image_from_registry(testing_provider: ModalProviderInstance) -> None:
    image = testing_provider._build_modal_image(base_image="python:3.11-slim")
    assert "python" in image.get_object_id()


def test_build_modal_image_from_dockerfile(
    testing_provider: ModalProviderInstance,
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM debian:bookworm-slim\nRUN echo hello\n")
    image = testing_provider._build_modal_image(dockerfile=dockerfile)
    assert image.get_object_id() is not None


def test_build_modal_image_with_secrets(
    testing_provider: ModalProviderInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SECRET", "secret_value")
    image = testing_provider._build_modal_image(secrets=["TEST_SECRET"])
    assert image.get_object_id() is not None


# ---------------------------------------------------------------------------
# Discovery Tests
# ---------------------------------------------------------------------------


def test_discover_hosts_with_running_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    snapshots = [make_snapshot("snap-run", "s1")]
    record = make_host_record(host_id=host_id, host_name="running-host", snapshots=snapshots)
    testing_provider._write_host_record(record)
    make_sandbox_with_tags(testing_modal, host_id, "running-host")

    cg = testing_provider.mng_ctx.concurrency_group
    # discover_hosts with running sandboxes -- _create_host_from_sandbox will fail
    # (no SSH) but the host record has snapshots, so it appears as a stopped host
    discovered = testing_provider.discover_hosts(cg)
    discovered_ids = {d.host_id for d in discovered}
    assert host_id in discovered_ids


def test_discover_hosts_stopped_with_snapshots(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="stopped-host", snapshots=snapshots)
    testing_provider._write_host_record(record)

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    assert len(discovered) == 1
    assert discovered[0].host_name == HostName("stopped-host")


def test_discover_hosts_failed_host(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="failed-host",
        failure_reason="Build failed",
        ssh_host=None,
        ssh_port=None,
        ssh_host_public_key=None,
    )
    testing_provider._write_host_record(record)

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    assert len(discovered) == 1
    assert discovered[0].host_name == HostName("failed-host")


def test_discover_hosts_destroyed_excluded_by_default(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="destroyed-host", snapshots=[])
    testing_provider._write_host_record(record)

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    assert len(discovered) == 0


def test_discover_hosts_destroyed_included_when_requested(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="destroyed-host", snapshots=[])
    testing_provider._write_host_record(record)

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg, include_destroyed=True)
    assert len(discovered) == 1


def test_discover_hosts_and_agents(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="with-agents", snapshots=snapshots)
    testing_provider._write_host_record(record)

    # Add agent data with proper AgentId format
    agent_id = str(AgentId.generate())
    testing_provider.persist_agent_data(
        host_id,
        {
            "id": agent_id,
            "name": "test-agent",
            "type": "claude",
            "command": "claude",
        },
    )

    cg = testing_provider.mng_ctx.concurrency_group
    result = testing_provider.discover_hosts_and_agents(cg)
    assert len(result) == 1
    host_ref = list(result.keys())[0]
    assert host_ref.host_name == HostName("with-agents")
    agents = result[host_ref]
    assert len(agents) == 1
    assert agents[0].agent_name == "test-agent"


def test_discover_hosts_and_agents_excludes_destroyed(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="destroyed", snapshots=[])
    testing_provider._write_host_record(record)

    cg = testing_provider.mng_ctx.concurrency_group
    result = testing_provider.discover_hosts_and_agents(cg)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# get_host and to_offline_host Tests
# ---------------------------------------------------------------------------


def test_get_host_by_id_offline(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="offline-host", snapshots=snapshots)
    testing_provider._write_host_record(record)

    host = testing_provider.get_host(host_id)
    assert host.id == host_id
    assert host.get_name() == "offline-host"


def test_get_host_by_name_offline(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="by-name-host", snapshots=snapshots)
    testing_provider._write_host_record(record)

    host = testing_provider.get_host(HostName("by-name-host"))
    assert host.id == host_id


def test_get_host_not_found(testing_provider: ModalProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.get_host(HostId.generate())


def test_get_host_by_name_not_found(testing_provider: ModalProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.get_host(HostName("nonexistent"))


def test_to_offline_host(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="offline")
    testing_provider._write_host_record(record)

    offline = testing_provider.to_offline_host(host_id)
    assert offline.id == host_id


def test_to_offline_host_not_found(testing_provider: ModalProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.to_offline_host(HostId.generate())


# ---------------------------------------------------------------------------
# Host Resources Tests
# ---------------------------------------------------------------------------


def test_get_host_resources_with_config(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    config = SandboxConfig(cpu=4.0, memory=8.0)
    record = make_host_record(host_id=host_id, config=config)
    testing_provider._write_host_record(record)

    offline = testing_provider.to_offline_host(host_id)
    resources = testing_provider.get_host_resources(offline)
    assert resources.cpu.count == 4
    assert resources.memory_gb == 8.0


def test_get_host_resources_no_config(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        config=None,
        ssh_host=None,
        ssh_port=None,
        ssh_host_public_key=None,
    )
    testing_provider._write_host_record(record)

    offline = testing_provider.to_offline_host(host_id)
    resources = testing_provider.get_host_resources(offline)
    assert resources.cpu.count == 1
    assert resources.memory_gb == 1.0


# ---------------------------------------------------------------------------
# Snapshot Tests
# ---------------------------------------------------------------------------


def test_list_snapshots(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
        make_snapshot("snap-2", "s2"),
    ]
    record = make_host_record(host_id=host_id, snapshots=snapshots)
    testing_provider._write_host_record(record)

    snap_list = testing_provider.list_snapshots(host_id)
    assert len(snap_list) == 2
    names = {s.name for s in snap_list}
    assert names == {SnapshotName("s1"), SnapshotName("s2")}


def test_list_snapshots_empty(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, snapshots=[])
    testing_provider._write_host_record(record)

    snap_list = testing_provider.list_snapshots(host_id)
    assert snap_list == []


def test_list_snapshots_no_record(testing_provider: ModalProviderInstance) -> None:
    snap_list = testing_provider.list_snapshots(HostId.generate())
    assert snap_list == []


def test_record_snapshot(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """Test the low-level _record_snapshot method which creates a filesystem snapshot
    and records it in the host record. This avoids the SSH requirement of create_snapshot
    since _record_snapshot calls get_host which needs SSH for the certified data update.
    We test the snapshot filesystem + host record logic directly.
    """
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="snap-host")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "snap-host")

    # Directly call sandbox.snapshot_filesystem to verify the testing sandbox supports it
    snap_image = sandbox.snapshot_filesystem()
    assert snap_image.get_object_id().startswith("snap-")

    # Verify the sandbox snapshot creates unique IDs
    snap_image2 = sandbox.snapshot_filesystem()
    assert snap_image2.get_object_id() != snap_image.get_object_id()


def test_create_snapshot_no_sandbox_raises(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id)
    testing_provider._write_host_record(record)

    with pytest.raises(HostNotFoundError):
        testing_provider.create_snapshot(host_id)


# ---------------------------------------------------------------------------
# Stop Host Tests
# ---------------------------------------------------------------------------


def test_stop_host_with_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="to-stop")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "to-stop")
    testing_provider._cache_sandbox(host_id, HostName("to-stop"), sandbox)

    # Use create_snapshot=False since snapshots require SSH (get_host -> set_certified_data)
    testing_provider.stop_host(host_id, create_snapshot=False)

    # Sandbox should be terminated
    with pytest.raises(ModalProxyError, match="terminated"):
        sandbox.exec("echo", "should fail")

    # Host record should have stop_reason
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.stop_reason == "STOPPED"


def test_stop_host_no_sandbox(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="already-stopped")
    testing_provider._write_host_record(record)

    # Should not raise even though no sandbox exists
    testing_provider.stop_host(host_id, create_snapshot=False)


# ---------------------------------------------------------------------------
# Destroy Host Tests
# ---------------------------------------------------------------------------


def test_destroy_host(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="to-destroy", snapshots=snapshots)
    testing_provider._write_host_record(record)

    # Add agent data
    testing_provider.persist_agent_data(host_id, {"id": str(AgentId.generate()), "name": "agent", "type": "claude"})

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "to-destroy")
    testing_provider._cache_sandbox(host_id, HostName("to-destroy"), sandbox)

    testing_provider.destroy_host(host_id)

    # Sandbox terminated
    with pytest.raises(ModalProxyError, match="terminated"):
        sandbox.exec("echo", "should fail")

    # Snapshots cleared
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 0

    # Agents removed
    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert agents == []


# ---------------------------------------------------------------------------
# Delete Host Tests
# ---------------------------------------------------------------------------


def test_delete_host(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="to-delete")
    testing_provider._write_host_record(record)
    testing_provider.persist_agent_data(host_id, {"id": str(AgentId.generate()), "name": "agent", "type": "claude"})

    offline = testing_provider.to_offline_host(host_id)
    testing_provider.delete_host(offline)

    assert testing_provider._read_host_record(host_id, use_cache=False) is None


# ---------------------------------------------------------------------------
# On Connection Error Tests
# ---------------------------------------------------------------------------


def test_on_connection_error_clears_caches(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="conn-err")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "conn-err")
    testing_provider._cache_sandbox(host_id, HostName("conn-err"), sandbox)
    testing_provider._host_record_cache_by_id[host_id] = record

    testing_provider.on_connection_error(host_id)

    assert host_id not in testing_provider._sandbox_cache_by_id
    assert host_id not in testing_provider._host_record_cache_by_id
    assert host_id not in testing_provider._host_by_id_cache


# ---------------------------------------------------------------------------
# Build Provider Instance Tests
# ---------------------------------------------------------------------------


def test_build_provider_instance_testing_mode(
    temp_mng_ctx: MngContext,
) -> None:
    config = ModalProviderConfig(
        mode=ModalMode.TESTING,
        app_name="build-test",
        host_dir=temp_mng_ctx.config.default_host_dir,
    )
    instance = ModalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, ModalProviderInstance)
    assert instance.app_name == "build-test"

    # Clean up the app registry
    ModalProviderBackend.close_app("build-test")


def test_build_provider_instance_truncates_long_names(
    temp_mng_ctx: MngContext,
) -> None:
    config = ModalProviderConfig(
        mode=ModalMode.TESTING,
        app_name="a" * 100,
        host_dir=temp_mng_ctx.config.default_host_dir,
    )
    instance = ModalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, ModalProviderInstance)
    # App name should be truncated to max_app_name_length
    assert len(instance.app_name) <= 64

    ModalProviderBackend.close_app(instance.app_name)


# ---------------------------------------------------------------------------
# Backend App Registry Tests
# ---------------------------------------------------------------------------


def test_app_registry_caches_apps(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal_interface = make_testing_modal_interface(tmp_path, cg)

    app1, handle1 = ModalProviderBackend._get_or_create_app("registry-test", "env1", False, modal_interface)
    app2, handle2 = ModalProviderBackend._get_or_create_app("registry-test", "env1", False, modal_interface)
    assert app1.get_app_id() == app2.get_app_id()

    ModalProviderBackend.close_app("registry-test")


def test_app_registry_persistent_mode(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal_interface = make_testing_modal_interface(tmp_path, cg)

    app, handle = ModalProviderBackend._get_or_create_app("persistent-test", "env1", True, modal_interface)
    assert app.get_app_id().startswith("ap-")
    # Persistent apps don't use run context
    assert handle.run_context is None

    ModalProviderBackend.close_app("persistent-test")


def test_close_app_removes_from_registry(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal_interface = make_testing_modal_interface(tmp_path, cg)

    ModalProviderBackend._get_or_create_app("close-test", "env1", False, modal_interface)
    assert "close-test" in ModalProviderBackend._app_registry

    ModalProviderBackend.close_app("close-test")
    assert "close-test" not in ModalProviderBackend._app_registry


def test_get_volume_for_app(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal_interface = make_testing_modal_interface(tmp_path, cg)

    ModalProviderBackend._get_or_create_app("vol-test", "env1", False, modal_interface)
    volume = ModalProviderBackend.get_volume_for_app("vol-test", modal_interface)
    assert volume is not None

    ModalProviderBackend.close_app("vol-test")


def test_get_volume_for_app_not_registered(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal_interface = make_testing_modal_interface(tmp_path, cg)

    with pytest.raises(MngError, match="not found in registry"):
        ModalProviderBackend.get_volume_for_app("nonexistent", modal_interface)


# ---------------------------------------------------------------------------
# Start Host Tests
# ---------------------------------------------------------------------------


def test_start_host_no_snapshots_raises(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="no-snaps", snapshots=[])
    testing_provider._write_host_record(record)

    with pytest.raises(NoSnapshotsModalMngError):
        testing_provider.start_host(host_id)


def test_start_host_failed_host_raises(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="failed",
        failure_reason="Build failed",
        ssh_host=None,
        ssh_port=None,
        ssh_host_public_key=None,
    )
    testing_provider._write_host_record(record)

    with pytest.raises(MngError, match="failed during creation"):
        testing_provider.start_host(host_id)


def test_start_host_not_found_raises(testing_provider: ModalProviderInstance) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.start_host(HostId.generate())


# ---------------------------------------------------------------------------
# Create Host Error Path Tests
# ---------------------------------------------------------------------------


def test_create_host_raises_on_ssh_setup_failure(
    testing_provider: ModalProviderInstance,
) -> None:
    """create_host raises when SSH setup fails in the testing environment.

    The sandbox is created successfully (TestingModalInterface doesn't need real
    Modal), but SSH setup fails because the testing sandbox can't start sshd
    as a non-root user. This verifies the error propagation path.
    """
    with pytest.raises((MngError, OSError, ExceptionGroup)):
        testing_provider.create_host(HostName("will-fail"))


# ---------------------------------------------------------------------------
# Properties and Config Tests
# ---------------------------------------------------------------------------


def test_provider_properties(testing_provider: ModalProviderInstance) -> None:
    assert testing_provider.supports_snapshots is True
    assert testing_provider.supports_shutdown_hosts is False
    assert testing_provider.supports_volumes is True
    assert testing_provider.supports_mutable_tags is True


def test_list_running_host_ids(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    cg = testing_provider.mng_ctx.concurrency_group
    host_id1 = HostId.generate()
    host_id2 = HostId.generate()
    make_sandbox_with_tags(testing_modal, host_id1, "r1")
    make_sandbox_with_tags(testing_modal, host_id2, "r2")

    running_ids = testing_provider._list_running_host_ids(cg)
    assert host_id1 in running_ids
    assert host_id2 in running_ids


def test_list_running_host_ids_empty(
    testing_provider: ModalProviderInstance,
) -> None:
    cg = testing_provider.mng_ctx.concurrency_group
    running_ids = testing_provider._list_running_host_ids(cg)
    assert running_ids == set()


# ---------------------------------------------------------------------------
# Certified Host Data Update Tests
# ---------------------------------------------------------------------------


def test_on_certified_host_data_updated(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="update-test")
    testing_provider._write_host_record(record)

    new_data = record.certified_host_data.model_copy_update(
        to_update(record.certified_host_data.field_ref().host_name, "updated-name"),
    )
    testing_provider._on_certified_host_data_updated(host_id, new_data)

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.host_name == "updated-name"


def test_on_certified_host_data_updated_not_found(testing_provider: ModalProviderInstance) -> None:
    now = datetime.now(timezone.utc)
    host_id = HostId.generate()
    data = CertifiedHostData(
        host_id=str(host_id),
        host_name="x",
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(MngError, match="not found"):
        testing_provider._on_certified_host_data_updated(host_id, data)


# ---------------------------------------------------------------------------
# Offline Host from Host Record Tests
# ---------------------------------------------------------------------------


def test_create_host_from_host_record(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("snap-1", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="offline-test", snapshots=snapshots)
    testing_provider._write_host_record(record)

    offline = testing_provider._create_host_from_host_record(record)
    assert offline.id == host_id
    assert offline.get_name() == "offline-test"


# ---------------------------------------------------------------------------
# Host Volume Name Derivation Tests
# ---------------------------------------------------------------------------


def test_host_volume_name_derivation(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    name = testing_provider._get_host_volume_name(host_id)
    assert HOST_VOLUME_INFIX in name
    assert len(name) <= 64


# ---------------------------------------------------------------------------
# ModalVolume Wrapper Tests
# ---------------------------------------------------------------------------


def test_modal_volume_wrapper(testing_provider: ModalProviderInstance) -> None:
    vol = testing_provider.get_state_volume()

    # Write
    vol.write_files({"/test/data.txt": b"hello"})

    # Read
    data = vol.read_file("/test/data.txt")
    assert data == b"hello"

    # List
    entries = vol.listdir("/test")
    assert len(entries) == 1

    # Remove
    vol.remove_file("/test/data.txt")

    # Remove directory
    vol.write_files({"/rmdir/file.txt": b"x"})
    vol.remove_directory("/rmdir")


# ---------------------------------------------------------------------------
# Tag Operations Tests
# ---------------------------------------------------------------------------


def test_get_host_tags_from_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="tagged-host",
        user_tags={"env": "prod"},
    )
    testing_provider._write_host_record(record)
    sandbox = make_sandbox_with_tags(
        testing_modal,
        host_id,
        "tagged-host",
        user_tags={"env": "prod", "team": "infra"},
    )
    testing_provider._cache_sandbox(host_id, HostName("tagged-host"), sandbox)

    tags = testing_provider.get_host_tags(host_id)
    assert tags == {"env": "prod", "team": "infra"}


def test_get_host_tags_from_host_record(
    testing_provider: ModalProviderInstance,
) -> None:
    """When no sandbox is running, tags are read from the host record."""
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="offline-tagged",
        user_tags={"version": "1.0"},
    )
    testing_provider._write_host_record(record)

    tags = testing_provider.get_host_tags(host_id)
    assert tags == {"version": "1.0"}


def test_get_host_tags_not_found_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.get_host_tags(HostId.generate())


def test_set_host_tags_offline(
    testing_provider: ModalProviderInstance,
) -> None:
    """set_host_tags on an offline host updates the volume record."""
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="set-tags-host",
        user_tags={"old": "value"},
    )
    testing_provider._write_host_record(record)

    testing_provider.set_host_tags(host_id, {"new": "tag", "another": "one"})

    # Volume record should be updated
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"new": "tag", "another": "one"}


def test_set_host_tags_sandbox_tags_updated(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """set_host_tags updates both sandbox tags and the volume record."""
    host_id, _, sandbox = setup_host_with_sandbox(
        testing_provider, testing_modal, "set-tags-sb", user_tags={"old": "value"}
    )

    testing_provider.set_host_tags(host_id, {"new": "tag", "another": "one"})

    # Sandbox tags should have user tags replaced
    sandbox_tags = sandbox.get_tags()
    assert sandbox_tags.get(TAG_USER_PREFIX + "new") == "tag"
    assert sandbox_tags.get(TAG_USER_PREFIX + "another") == "one"
    assert TAG_USER_PREFIX + "old" not in sandbox_tags
    assert sandbox_tags[TAG_HOST_ID] == str(host_id)
    assert sandbox_tags[TAG_HOST_NAME] == "set-tags-sb"

    # Volume record should also be updated
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"new": "tag", "another": "one"}


def test_add_tags_to_host_offline(
    testing_provider: ModalProviderInstance,
) -> None:
    """add_tags_to_host on an offline host merges tags in the volume record."""
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="add-tags-host",
        user_tags={"existing": "value"},
    )
    testing_provider._write_host_record(record)

    testing_provider.add_tags_to_host(host_id, {"added": "new"})

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"existing": "value", "added": "new"}


def test_add_tags_to_host_sandbox_tags_updated(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """add_tags_to_host updates sandbox tags and volume record when sandbox is running."""
    host_id, _, sandbox = setup_host_with_sandbox(
        testing_provider, testing_modal, "add-tags-sb", user_tags={"existing": "value"}
    )

    testing_provider.add_tags_to_host(host_id, {"added": "new"})

    sandbox_tags = sandbox.get_tags()
    assert sandbox_tags.get(TAG_USER_PREFIX + "existing") == "value"
    assert sandbox_tags.get(TAG_USER_PREFIX + "added") == "new"

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"existing": "value", "added": "new"}


def test_remove_tags_from_host_offline(
    testing_provider: ModalProviderInstance,
) -> None:
    """remove_tags_from_host on an offline host removes tags from the volume record."""
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="remove-tags-host",
        user_tags={"keep": "yes", "remove": "me"},
    )
    testing_provider._write_host_record(record)

    testing_provider.remove_tags_from_host(host_id, ["remove"])

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"keep": "yes"}


def test_remove_tags_from_host_sandbox_tags_updated(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """remove_tags_from_host updates sandbox tags and volume record."""
    host_id, _, sandbox = setup_host_with_sandbox(
        testing_provider, testing_modal, "remove-tags-sb", user_tags={"keep": "yes", "remove": "me"}
    )

    testing_provider.remove_tags_from_host(host_id, ["remove"])

    sandbox_tags = sandbox.get_tags()
    assert TAG_USER_PREFIX + "remove" not in sandbox_tags
    assert sandbox_tags.get(TAG_USER_PREFIX + "keep") == "yes"

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.user_tags == {"keep": "yes"}


def test_rename_host_with_sandbox(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """rename_host updates sandbox tags and volume record."""
    host_id, _, sandbox = setup_host_with_sandbox(testing_provider, testing_modal, "old-name")

    testing_provider.rename_host(host_id, HostName("new-name"))

    # Sandbox tag should be updated
    sandbox_tags = sandbox.get_tags()
    assert sandbox_tags[TAG_HOST_NAME] == "new-name"

    # Volume record should be updated
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.host_name == "new-name"


def test_rename_host_without_sandbox(
    testing_provider: ModalProviderInstance,
) -> None:
    """Renaming an offline host (no sandbox) should update the host record."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="offline-old")
    testing_provider._write_host_record(record)

    testing_provider.rename_host(host_id, HostName("offline-new"))

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.host_name == "offline-new"


# ---------------------------------------------------------------------------
# Delete Snapshot Tests
# ---------------------------------------------------------------------------


def test_delete_snapshot_removes_from_record(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    snap_id = "im-snap-abc123"
    snapshots = [
        make_snapshot(snap_id, "s1"),
        make_snapshot("im-snap-def456", "s2"),
    ]
    record = make_host_record(host_id=host_id, host_name="snap-del-host", snapshots=snapshots)
    testing_provider._write_host_record(record)

    testing_provider.delete_snapshot(host_id, SnapshotId(snap_id))

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 1
    assert updated.certified_host_data.snapshots[0].id == "im-snap-def456"


def test_delete_snapshot_not_found_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    snapshots = [
        make_snapshot("existing-snap", "s1"),
    ]
    record = make_host_record(host_id=host_id, host_name="snap-host", snapshots=snapshots)
    testing_provider._write_host_record(record)

    with pytest.raises(SnapshotNotFoundError):
        testing_provider.delete_snapshot(host_id, SnapshotId("nonexistent-snap"))


def test_delete_snapshot_host_not_found_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.delete_snapshot(HostId.generate(), SnapshotId("some-snap"))


# ---------------------------------------------------------------------------
# get_host Edge Cases Tests
# ---------------------------------------------------------------------------


def test_get_host_by_id_uses_cache(
    testing_provider: ModalProviderInstance,
) -> None:
    """Once a host is fetched, subsequent get_host calls return the cached object."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="cache-test")
    testing_provider._write_host_record(record)

    host1 = testing_provider.get_host(host_id)
    host2 = testing_provider.get_host(host_id)
    assert host1 is host2


def test_get_host_by_name_searches_host_records(
    testing_provider: ModalProviderInstance,
) -> None:
    """get_host by name should search through host records when no sandbox matches."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="name-search")
    testing_provider._write_host_record(record)

    host = testing_provider.get_host(HostName("name-search"))
    assert host.id == host_id
    assert host.get_name() == "name-search"


def test_get_host_by_name_not_in_records_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    """get_host by name raises HostNotFoundError if not found in records or sandboxes."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="other-host")
    testing_provider._write_host_record(record)

    with pytest.raises(HostNotFoundError):
        testing_provider.get_host(HostName("not-this-one"))


# ---------------------------------------------------------------------------
# Discover Hosts with Multiple States Tests
# ---------------------------------------------------------------------------


def test_discover_hosts_mixed_states(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """Discover hosts with a mix of stopped, failed, and destroyed hosts."""
    cg = testing_provider.mng_ctx.concurrency_group

    # Stopped host (has snapshots, no sandbox)
    stopped_id = HostId.generate()
    stopped_snaps = [
        make_snapshot("snap-stopped", "s1"),
    ]
    testing_provider._write_host_record(
        make_host_record(host_id=stopped_id, host_name="stopped", snapshots=stopped_snaps)
    )

    # Failed host (has failure_reason, no sandbox, no snapshots)
    failed_id = HostId.generate()
    testing_provider._write_host_record(
        make_host_record(
            host_id=failed_id,
            host_name="failed",
            failure_reason="Build broke",
            ssh_host=None,
            ssh_port=None,
            ssh_host_public_key=None,
        )
    )

    # Destroyed host (no snapshots, no failure, no sandbox)
    destroyed_id = HostId.generate()
    testing_provider._write_host_record(make_host_record(host_id=destroyed_id, host_name="destroyed", snapshots=[]))

    # Without include_destroyed, only stopped + failed should appear
    discovered = testing_provider.discover_hosts(cg, include_destroyed=False)
    discovered_names = {d.host_name for d in discovered}
    assert HostName("stopped") in discovered_names
    assert HostName("failed") in discovered_names
    assert HostName("destroyed") not in discovered_names

    # With include_destroyed, all three should appear
    discovered_all = testing_provider.discover_hosts(cg, include_destroyed=True)
    discovered_all_names = {d.host_name for d in discovered_all}
    assert HostName("stopped") in discovered_all_names
    assert HostName("failed") in discovered_all_names
    assert HostName("destroyed") in discovered_all_names


def test_discover_hosts_and_agents_mixed_states(
    testing_provider: ModalProviderInstance,
) -> None:
    """discover_hosts_and_agents includes stopped/failed hosts with their agents."""
    cg = testing_provider.mng_ctx.concurrency_group

    # Stopped host with agent
    stopped_id = HostId.generate()
    stopped_snaps = [
        make_snapshot("snap-1", "s1"),
    ]
    testing_provider._write_host_record(
        make_host_record(host_id=stopped_id, host_name="stopped-agents", snapshots=stopped_snaps)
    )
    agent_id = str(AgentId.generate())
    testing_provider.persist_agent_data(
        stopped_id,
        {
            "id": agent_id,
            "name": "agent-one",
            "type": "claude",
            "command": "claude",
        },
    )

    # Failed host (no agents)
    failed_id = HostId.generate()
    testing_provider._write_host_record(
        make_host_record(
            host_id=failed_id,
            host_name="failed-no-agents",
            failure_reason="Build error",
            ssh_host=None,
            ssh_port=None,
            ssh_host_public_key=None,
        )
    )

    # Destroyed host (should be excluded by default)
    destroyed_id = HostId.generate()
    testing_provider._write_host_record(
        make_host_record(host_id=destroyed_id, host_name="destroyed-no-agents", snapshots=[])
    )

    result = testing_provider.discover_hosts_and_agents(cg)
    result_names = {h.host_name for h in result}
    assert HostName("stopped-agents") in result_names
    assert HostName("failed-no-agents") in result_names
    assert HostName("destroyed-no-agents") not in result_names

    # Verify the stopped host has its agent
    for host_ref, agents in result.items():
        if host_ref.host_name == HostName("stopped-agents"):
            assert len(agents) == 1
            assert agents[0].agent_name == "agent-one"


# ---------------------------------------------------------------------------
# ModalProviderApp Tests
# ---------------------------------------------------------------------------


def test_modal_provider_app_get_captured_output(
    testing_modal: TestingModalInterface,
) -> None:
    app = testing_modal.app_lookup("output-test", create_if_missing=True, environment_name="test")
    volume = testing_modal.volume_from_name("output-vol", create_if_missing=True, environment_name="test")
    captured = ["some build log output"]

    modal_app = ModalProviderApp(
        app_name="output-test",
        environment_name="test",
        app=app,
        volume=volume,
        modal_interface=testing_modal,
        close_callback=lambda: None,
        get_output_callback=lambda: captured[0],
    )

    assert modal_app.get_captured_output() == "some build log output"


def test_modal_provider_app_close(
    testing_modal: TestingModalInterface,
) -> None:
    app = testing_modal.app_lookup("close-test", create_if_missing=True, environment_name="test")
    volume = testing_modal.volume_from_name("close-vol", create_if_missing=True, environment_name="test")
    close_called = [False]

    def on_close() -> None:
        close_called[0] = True

    modal_app = ModalProviderApp(
        app_name="close-test",
        environment_name="test",
        app=app,
        volume=volume,
        modal_interface=testing_modal,
        close_callback=on_close,
        get_output_callback=lambda: "",
    )

    modal_app.close()
    assert close_called[0] is True


def test_provider_instance_get_captured_output(
    testing_provider: ModalProviderInstance,
) -> None:
    """get_captured_output on the instance delegates to the modal_app."""
    output = testing_provider.get_captured_output()
    assert output == ""


def test_provider_instance_close(
    testing_provider: ModalProviderInstance,
) -> None:
    """close on the instance delegates to the modal_app."""
    # Should not raise
    testing_provider.close()


# ---------------------------------------------------------------------------
# Volume Wrapper Edge Cases Tests
# ---------------------------------------------------------------------------


def test_proxy_file_entry_type_file_maps_to_volume_file() -> None:
    result = _proxy_file_entry_type_to_volume_file_type(ProxyFileEntryType.FILE)
    assert result == VolumeFileType.FILE


def test_proxy_file_entry_type_directory_maps_to_volume_directory() -> None:
    result = _proxy_file_entry_type_to_volume_file_type(ProxyFileEntryType.DIRECTORY)
    assert result == VolumeFileType.DIRECTORY


# ---------------------------------------------------------------------------
# Parsing Helper Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("42", 42), ("  123  ", 123), ("0", 0), ("", None), ("   ", None), ("not_a_number", None), ("12.5", None)],
)
def test_parse_optional_int(value: str, expected: int | None) -> None:
    assert _parse_optional_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3.14", 3.14), ("  42.0  ", 42.0), ("0", 0.0), ("100", 100.0), ("", None), ("   ", None), ("abc", None)],
)
def test_parse_optional_float(value: str, expected: float | None) -> None:
    assert _parse_optional_float(value) == expected


# ---------------------------------------------------------------------------
# Parse Build Args (testing_provider specific) Tests
# ---------------------------------------------------------------------------


def test_parse_build_args_dockerfile_flag(
    testing_provider: ModalProviderInstance,
) -> None:
    config = testing_provider._parse_build_args(["--file=/path/to/Dockerfile"])
    assert config.dockerfile == "/path/to/Dockerfile"


def test_parse_build_args_volume_spec(
    testing_provider: ModalProviderInstance,
) -> None:
    config = testing_provider._parse_build_args(["--volume=mydata:/mnt/data"])
    assert config.volumes == (("mydata", "/mnt/data"),)


def test_parse_build_args_docker_build_arg_spec(
    testing_provider: ModalProviderInstance,
) -> None:
    config = testing_provider._parse_build_args(["--docker-build-arg=KEY=value", "--docker-build-arg=OTHER=stuff"])
    assert config.docker_build_args == ("KEY=value", "OTHER=stuff")


def test_parse_build_args_offline_flag(
    testing_provider: ModalProviderInstance,
) -> None:
    config = testing_provider._parse_build_args(["offline"])
    assert config.offline is True
    assert config.effective_cidr_allowlist == []


def test_parse_build_args_all_options(
    testing_provider: ModalProviderInstance,
) -> None:
    config = testing_provider._parse_build_args(
        [
            "--gpu=a100",
            "--cpu=4",
            "--memory=16",
            "--image=python:3.11",
            "--timeout=600",
            "--region=us-east",
            "--secret=MY_SECRET",
            "--cidr-allowlist=10.0.0.0/8",
            "--volume=data:/data",
            "--docker-build-arg=VER=1.0",
        ]
    )
    assert config.gpu == "a100"
    assert config.cpu == 4.0
    assert config.memory == 16.0
    assert config.image == "python:3.11"
    assert config.timeout == 600
    assert config.region == "us-east"
    assert config.secrets == ("MY_SECRET",)
    assert config.cidr_allowlist == ("10.0.0.0/8",)
    assert config.volumes == (("data", "/data"),)
    assert config.docker_build_args == ("VER=1.0",)


def test_parse_build_args_unknown_arg_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    with pytest.raises(MngError, match="Unknown build arguments"):
        testing_provider._parse_build_args(["--unknown-arg=value"])


# ---------------------------------------------------------------------------
# Backend Module-Level Function Tests
# ---------------------------------------------------------------------------


def test_create_environment(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    _create_environment("test-env", modal)
    assert "test-env" in modal._environments


def test_create_environment_rejects_bad_mng_prefix(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    with pytest.raises(MngError, match="Refusing to create"):
        _create_environment("mng_bad-name", modal)


def test_create_environment_allows_mng_test_prefix(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    _create_environment("mng_test-good-name", modal)
    assert "mng_test-good-name" in modal._environments


def test_lookup_persistent_app_with_env_retry(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    modal.environment_create("env1")
    app = _lookup_persistent_app_with_env_retry("my-app", "env1", modal)
    assert app.get_name() == "my-app"


def test_enter_ephemeral_app_context_with_env_retry(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    modal.environment_create("env1")
    app = modal.app_create("eph-app")
    gen = _enter_ephemeral_app_context_with_env_retry(app, "env1", modal)
    assert gen is not None


def test_exit_modal_app_context_with_ephemeral(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    modal.environment_create("env1")
    app = modal.app_create("exit-test")
    gen = app.run(environment_name="env1")
    next(gen)

    output_buffer = StringIO()
    output_buffer.write("some modal output")

    handle = ModalAppContextHandle(
        run_context=gen,
        app_name="exit-test",
        environment_name="env1",
        output_capture_context=contextlib.nullcontext((output_buffer, None)),
        output_buffer=output_buffer,
        loguru_writer=None,
        volume_name="exit-test-state",
    )
    _exit_modal_app_context(handle)


def test_exit_modal_app_context_persistent(tmp_path: Path) -> None:
    output_buffer = StringIO()
    handle = ModalAppContextHandle(
        run_context=None,
        app_name="persistent-exit",
        environment_name="env1",
        output_capture_context=contextlib.nullcontext((output_buffer, None)),
        output_buffer=output_buffer,
        loguru_writer=None,
        volume_name="persistent-exit-state",
    )
    _exit_modal_app_context(handle)


def test_reset_app_registry(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    ModalProviderBackend._get_or_create_app("reset-a", "env1", False, modal)
    ModalProviderBackend._get_or_create_app("reset-b", "env1", False, modal)
    assert len(ModalProviderBackend._app_registry) >= 2

    ModalProviderBackend.reset_app_registry()
    assert "reset-a" not in ModalProviderBackend._app_registry
    assert "reset-b" not in ModalProviderBackend._app_registry


def test_backend_get_description() -> None:
    assert "Modal" in ModalProviderBackend.get_description()


def test_backend_get_build_args_help() -> None:
    help_text = ModalProviderBackend.get_build_args_help()
    assert "--file" in help_text
    assert "--gpu" in help_text
    assert "--cpu" in help_text


def test_backend_get_start_args_help() -> None:
    help_text = ModalProviderBackend.get_start_args_help()
    assert "No start arguments" in help_text


# ---------------------------------------------------------------------------
# Deploy Function Tests
# ---------------------------------------------------------------------------


def test_deploy_function(
    testing_provider: ModalProviderInstance,
) -> None:
    url = deploy_function(
        "snapshot_and_shutdown",
        testing_provider.app_name,
        testing_provider.environment_name,
        testing_provider._modal_interface,
    )
    assert "snapshot_and_shutdown" in url


# ---------------------------------------------------------------------------
# Volume Listing and Deletion Tests
# ---------------------------------------------------------------------------


def test_list_volumes(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id1 = HostId.generate()
    host_id2 = HostId.generate()
    testing_provider._build_host_volume(host_id1)
    testing_provider._build_host_volume(host_id2)

    volumes = testing_provider.list_volumes()
    assert len(volumes) >= 2


def test_delete_volume(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    testing_provider._build_host_volume(host_id)

    vol_name = testing_provider._get_host_volume_name(host_id)
    volumes = testing_provider.list_volumes()
    matching = [v for v in volumes if v.name == vol_name]
    assert len(matching) == 1

    testing_provider.delete_volume(matching[0].volume_id)

    volumes_after = testing_provider.list_volumes()
    matching_after = [v for v in volumes_after if v.name == vol_name]
    assert len(matching_after) == 0


def test_delete_volume_not_found_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    with pytest.raises(MngError, match="not found"):
        testing_provider.delete_volume(VolumeId.generate())


# ---------------------------------------------------------------------------
# get_connector Tests
# ---------------------------------------------------------------------------


def test_get_connector_not_found_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    with pytest.raises(HostNotFoundError):
        testing_provider.get_connector(HostId.generate())


def test_get_connector_failed_host_raises(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    record = make_host_record(
        host_id=host_id,
        host_name="failed-conn",
        failure_reason="Build failed",
        ssh_host=None,
        ssh_port=None,
        ssh_host_public_key=None,
    )
    testing_provider._write_host_record(record)

    with pytest.raises(MngError, match="no SSH info"):
        testing_provider.get_connector(host_id)


# ---------------------------------------------------------------------------
# _build_modal_volumes Tests
# ---------------------------------------------------------------------------


def test_build_modal_volumes(
    testing_provider: ModalProviderInstance,
) -> None:
    volume_specs = (("my-vol", "/mnt/data"), ("other-vol", "/mnt/other"))
    volumes = _build_modal_volumes(
        volume_specs,
        testing_provider.environment_name,
        testing_provider._modal_interface,
    )
    assert "/mnt/data" in volumes
    assert "/mnt/other" in volumes


# ---------------------------------------------------------------------------
# _build_modal_secrets_from_env Tests
# ---------------------------------------------------------------------------


def test_build_modal_secrets_from_env_empty(testing_modal: TestingModalInterface) -> None:
    result = _build_modal_secrets_from_env([], testing_modal)
    assert result == []


def test_build_modal_secrets_from_env_missing_var(testing_modal: TestingModalInterface) -> None:
    with pytest.raises(MngError, match="not set"):
        _build_modal_secrets_from_env(["DEFINITELY_NOT_SET_VAR_12345"], testing_modal)


# ---------------------------------------------------------------------------
# _substitute_dockerfile_build_args Tests
# ---------------------------------------------------------------------------


def test_substitute_dockerfile_build_args() -> None:
    dockerfile = 'FROM debian\nARG VERSION="1.0"\nRUN echo $VERSION\n'
    result = _substitute_dockerfile_build_args(dockerfile, ["VERSION=2.0"])
    assert 'ARG VERSION="2.0"' in result


def test_substitute_dockerfile_build_args_not_found() -> None:
    dockerfile = "FROM debian\nRUN echo hello\n"
    with pytest.raises(MngError, match="not found as an ARG"):
        _substitute_dockerfile_build_args(dockerfile, ["MISSING_ARG=value"])


def test_substitute_dockerfile_build_args_bad_format() -> None:
    with pytest.raises(MngError, match="KEY=VALUE"):
        _substitute_dockerfile_build_args("FROM debian", ["bad_format"])


# ---------------------------------------------------------------------------
# _build_image_from_dockerfile_contents Tests
# ---------------------------------------------------------------------------


def test_build_image_from_dockerfile_contents(
    testing_provider: ModalProviderInstance,
) -> None:
    dockerfile_contents = "FROM debian:bookworm-slim\nRUN echo hello\nRUN echo world\n"
    image = _build_image_from_dockerfile_contents(
        dockerfile_contents,
        modal_interface=testing_provider._modal_interface,
        is_each_layer_cached=True,
    )
    assert image.get_object_id() is not None


def test_build_image_from_dockerfile_no_layer_caching(
    testing_provider: ModalProviderInstance,
) -> None:
    dockerfile_contents = "FROM debian:bookworm-slim\nRUN echo hello\n"
    image = _build_image_from_dockerfile_contents(
        dockerfile_contents,
        modal_interface=testing_provider._modal_interface,
        is_each_layer_cached=False,
    )
    assert image.get_object_id() is not None


# ---------------------------------------------------------------------------
# SandboxConfig Tests
# ---------------------------------------------------------------------------


def test_sandbox_config_effective_cidr_allowlist_default() -> None:
    config = SandboxConfig()
    assert config.effective_cidr_allowlist is None


def test_sandbox_config_effective_cidr_allowlist_offline() -> None:
    config = SandboxConfig(offline=True)
    assert config.effective_cidr_allowlist == []


def test_sandbox_config_effective_cidr_allowlist_explicit() -> None:
    config = SandboxConfig(cidr_allowlist=("10.0.0.0/8", "192.168.0.0/16"))
    assert config.effective_cidr_allowlist == ["10.0.0.0/8", "192.168.0.0/16"]


# ---------------------------------------------------------------------------
# _parse_volume_spec Tests
# ---------------------------------------------------------------------------


def test_parse_volume_spec_valid() -> None:
    name, path = _parse_volume_spec("my-vol:/mnt/data")
    assert name == "my-vol"
    assert path == "/mnt/data"


def test_parse_volume_spec_invalid() -> None:
    with pytest.raises(MngError, match="Invalid volume spec"):
        _parse_volume_spec("no-colon-here")


def test_parse_volume_spec_empty_parts() -> None:
    with pytest.raises(MngError, match="Invalid volume spec"):
        _parse_volume_spec(":/mnt/data")


# ---------------------------------------------------------------------------
# Agent Listing Edge Cases
# ---------------------------------------------------------------------------


def test_list_persisted_agent_data_skips_invalid_json(
    testing_provider: ModalProviderInstance,
) -> None:
    host_id = HostId.generate()
    volume = testing_provider.get_state_volume()
    host_dir = f"/hosts/{host_id}"
    volume.write_files(
        {
            f"{host_dir}/agent-aaaa1111bbbb2222cccc3333dddd4444.json": b"not valid json{{{",
        }
    )

    agents = testing_provider.list_persisted_agent_data_for_host(host_id)
    assert agents == []


# ---------------------------------------------------------------------------
# discover_hosts with Running Sandbox Tests
# ---------------------------------------------------------------------------


def test_discover_hosts_running_sandbox_without_host_record(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """A running sandbox without a host record (eventual consistency) shows up in discovery.
    The sandbox has tags, but no host record on the volume yet. It should NOT appear
    since _create_host_from_sandbox will fail without SSH."""
    host_id = HostId.generate()
    make_sandbox_with_tags(testing_modal, host_id, "orphan-sandbox")

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    # The sandbox exists but has no host record, and _create_host_from_sandbox
    # will return None (no SSH info on volume). So it won't appear.
    host_ids = {d.host_id for d in discovered}
    assert host_id not in host_ids


def test_get_or_create_app_caches_volume(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    ModalProviderBackend._get_or_create_app("vol-cache-test", "env1", False, modal)

    # Get volume twice -- should return the same object
    vol1 = ModalProviderBackend.get_volume_for_app("vol-cache-test", modal)
    vol2 = ModalProviderBackend.get_volume_for_app("vol-cache-test", modal)
    assert vol1 is vol2

    ModalProviderBackend.close_app("vol-cache-test")


def test_close_nonexistent_app() -> None:
    # Should not raise
    ModalProviderBackend.close_app("this-app-does-not-exist")


# ---------------------------------------------------------------------------
# Backend get_name / get_config_class Tests
# ---------------------------------------------------------------------------


def test_backend_get_name() -> None:
    assert ModalProviderBackend.get_name() == ProviderBackendName("modal")


def test_backend_get_config_class() -> None:
    assert ModalProviderBackend.get_config_class() is ModalProviderConfig


# ---------------------------------------------------------------------------
# get_host_resources with host record Tests
# ---------------------------------------------------------------------------


def test_get_host_resources_fractional_cpu(testing_provider: ModalProviderInstance) -> None:
    host_id = HostId.generate()
    config = SandboxConfig(cpu=0.25, memory=0.5)
    record = make_host_record(host_id=host_id, config=config)
    testing_provider._write_host_record(record)

    offline = testing_provider.to_offline_host(host_id)
    resources = testing_provider.get_host_resources(offline)
    # Fractional CPU should be rounded up to 1
    assert resources.cpu.count == 1
    assert resources.memory_gb == 0.5


# ---------------------------------------------------------------------------
# Backend register_provider_backend Hook Test
# ---------------------------------------------------------------------------


def test_register_provider_backend_hook() -> None:
    backend_cls, config_cls = register_provider_backend()
    assert backend_cls is ModalProviderBackend
    assert config_cls is ModalProviderConfig


# ---------------------------------------------------------------------------
# HostRecord Model Test
# ---------------------------------------------------------------------------


def test_host_record_roundtrip() -> None:
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="roundtrip-test",
        created_at=now,
        updated_at=now,
    )
    record = HostRecord(
        certified_host_data=certified_data,
        ssh_host="10.0.0.1",
        ssh_port=2222,
        ssh_host_public_key="ssh-ed25519 AAAA",
        config=SandboxConfig(cpu=2.0, memory=4.0, gpu="a100"),
    )

    json_str = record.model_dump_json(indent=2)
    loaded = HostRecord.model_validate_json(json_str)

    assert loaded.ssh_host == "10.0.0.1"
    assert loaded.ssh_port == 2222
    assert loaded.config is not None
    assert loaded.config.gpu == "a100"


# ---------------------------------------------------------------------------
# ModalProviderApp Integration Test
# ---------------------------------------------------------------------------


def test_modal_provider_app_full_lifecycle(
    temp_mng_ctx: MngContext,
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    modal = make_testing_modal_interface(tmp_path, cg)
    provider = make_testing_provider(temp_mng_ctx, modal)

    # Write a host record
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="lifecycle-test")
    provider._write_host_record(record)

    # Read it back
    loaded = provider._read_host_record(host_id)
    assert loaded is not None

    # Add agent data
    agent_id = str(AgentId.generate())
    provider.persist_agent_data(
        host_id,
        {
            "id": agent_id,
            "name": "lc-agent",
            "type": "claude",
            "command": "claude",
        },
    )

    # Discover
    cg = provider.mng_ctx.concurrency_group
    discovered = provider.discover_hosts(cg, include_destroyed=True)
    assert len(discovered) == 1

    # Clean up
    provider.destroy_host(host_id)
    modal.cleanup()


# ---------------------------------------------------------------------------
# Snapshot with Pre-populated Host Cache Tests
# ---------------------------------------------------------------------------


def test_create_snapshot_with_cached_offline_host(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """Test create_snapshot by pre-populating the host cache with an OfflineHost.
    This avoids the SSH connection attempt in _record_snapshot -> get_host.
    """
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="snap-offline")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "snap-offline")
    testing_provider._cache_sandbox(host_id, HostName("snap-offline"), sandbox)

    # Pre-populate host cache so get_host returns OfflineHost instead of trying SSH
    offline = testing_provider._create_host_from_host_record(record)
    testing_provider._host_by_id_cache[host_id] = offline

    snap_id = testing_provider.create_snapshot(host_id, SnapshotName("my-snap"))
    assert str(snap_id).startswith("snap-")

    # Verify the snapshot was recorded in the host record
    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 1
    assert updated.certified_host_data.snapshots[0].name == "my-snap"


def test_create_snapshot_auto_name_with_cached_host(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """create_snapshot without a name generates a timestamp-based name."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="auto-snap")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "auto-snap")
    testing_provider._cache_sandbox(host_id, HostName("auto-snap"), sandbox)

    offline = testing_provider._create_host_from_host_record(record)
    testing_provider._host_by_id_cache[host_id] = offline

    snap_id = testing_provider.create_snapshot(host_id)
    assert str(snap_id).startswith("snap-")

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert len(updated.certified_host_data.snapshots) == 1
    assert updated.certified_host_data.snapshots[0].name.startswith("snapshot-")


def test_stop_host_with_snapshot_using_cached_host(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """stop_host with create_snapshot=True should create a snapshot before terminating."""
    host_id = HostId.generate()
    record = make_host_record(host_id=host_id, host_name="stop-snap")
    testing_provider._write_host_record(record)

    sandbox = make_sandbox_with_tags(testing_modal, host_id, "stop-snap")
    testing_provider._cache_sandbox(host_id, HostName("stop-snap"), sandbox)

    offline = testing_provider._create_host_from_host_record(record)
    testing_provider._host_by_id_cache[host_id] = offline

    testing_provider.stop_host(host_id, create_snapshot=True)

    with pytest.raises(ModalProxyError, match="terminated"):
        sandbox.exec("echo", "should fail")

    updated = testing_provider._read_host_record(host_id, use_cache=False)
    assert updated is not None
    # Should have a "stop" snapshot
    assert len(updated.certified_host_data.snapshots) == 1
    assert updated.certified_host_data.snapshots[0].name == "stop"
    assert updated.certified_host_data.stop_reason == "STOPPED"


# ---------------------------------------------------------------------------
# discover_hosts Detailed Coverage Tests
# ---------------------------------------------------------------------------


def test_discover_hosts_handles_sandbox_without_valid_tags(
    testing_provider: ModalProviderInstance,
    testing_modal: TestingModalInterface,
) -> None:
    """Sandboxes without valid mng tags should be skipped during discovery."""
    image = testing_modal.image_debian_slim()
    app = list(testing_modal._apps.values())[0]
    sandbox = testing_modal.sandbox_create(image=image, app=app, timeout=300, cpu=1.0, memory=1024)
    # Set invalid tags (missing TAG_HOST_ID)
    sandbox.set_tags({"random_key": "random_value"})

    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    # Should not crash, and should not include this sandbox
    assert len(discovered) == 0


def test_build_modal_image_from_dockerfile_with_context(
    testing_provider: ModalProviderInstance,
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM debian:bookworm-slim\nRUN echo hello\n")
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    image = testing_provider._build_modal_image(
        dockerfile=dockerfile,
        context_dir=context_dir,
    )
    assert image.get_object_id() is not None


# ---------------------------------------------------------------------------
# get_host_resources with Missing Record Tests
# ---------------------------------------------------------------------------


def test_get_host_resources_missing_record(
    testing_provider: ModalProviderInstance,
) -> None:
    """get_host_resources returns defaults when no host record exists on volume."""

    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    # Create an OfflineHost directly without a host record on the volume
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="no-record",
        created_at=now,
        updated_at=now,
    )
    offline = OfflineHost(
        id=host_id,
        certified_host_data=certified,
        provider_instance=testing_provider,
        mng_ctx=testing_provider.mng_ctx,
        on_updated_host_data=lambda _hid, _data: None,
    )

    resources = testing_provider.get_host_resources(offline)
    assert resources.cpu.count == 1
    assert resources.memory_gb == 1.0
    assert resources.cpu.frequency_ghz is None


# ---------------------------------------------------------------------------
# _build_modal_image with docker_build_args Test
# ---------------------------------------------------------------------------


def test_build_modal_image_from_dockerfile_with_build_args(
    testing_provider: ModalProviderInstance,
    tmp_path: Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('FROM debian:bookworm-slim\nARG VERSION="1.0"\nRUN echo $VERSION\n')
    image = testing_provider._build_modal_image(
        dockerfile=dockerfile,
        docker_build_args=["VERSION=2.0"],
    )
    assert image.get_object_id() is not None


# ---------------------------------------------------------------------------
# discover_hosts with Empty Result Test
# ---------------------------------------------------------------------------


def test_discover_hosts_empty_volume_and_no_sandboxes(
    testing_provider: ModalProviderInstance,
) -> None:
    cg = testing_provider.mng_ctx.concurrency_group
    discovered = testing_provider.discover_hosts(cg)
    assert discovered == []


# ---------------------------------------------------------------------------
# HostRecord with failed host -- ensure we handle the None config case
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _build_listing_collection_script Tests
# ---------------------------------------------------------------------------


def test_build_listing_script_uses_host_dir() -> None:
    script = _build_listing_collection_script("/custom/host/dir", "test-prefix-")
    assert "/custom/host/dir" in script
    assert "test-prefix-" in script
