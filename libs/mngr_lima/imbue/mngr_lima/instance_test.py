from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNameConflictError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.data_types import LimaSizeRequest
from imbue.mngr_lima.errors import LimaCommandUnavailableError
from imbue.mngr_lima.errors import LimaHostCreationError
from imbue.mngr_lima.errors import LimaResizeRefusedError
from imbue.mngr_lima.host_store import HostRecord
from imbue.mngr_lima.host_store import LimaHostConfig
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.instance import _find_conflicting_host_record
from imbue.mngr_lima.instance import _record_state_for_conflict_message
from imbue.mngr_lima.instance import _recorded_host_dir_override
from imbue.mngr_lima.limactl import LimaSshConfig
from imbue.mngr_lima.primitives import LimaCpuCount
from imbue.mngr_lima.primitives import LimaDiskSize
from imbue.mngr_lima.primitives import LimaMemoryGib
from imbue.mngr_lima.testing import install_fake_limactl


def test_discover_hosts_reports_provider_unavailable_when_limactl_crashes(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limactl that is installed and correctly versioned but crashes on ``list`` is
    reported as provider unavailability (like an unreachable Docker daemon), not
    silently swallowed into an all-offline view.

    ``--version`` succeeds so the availability check passes; only ``list`` crashes,
    mirroring a mid-session limactl startup fault (e.g. the getpwuid init panic).
    """
    bin_dir = tmp_path / "bin"
    install_fake_limactl(
        bin_dir,
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\n'
        'echo "panic: user: unknown userid 501" >&2\nexit 2\n',
        monkeypatch,
    )

    with pytest.raises(LimaCommandUnavailableError, match="not available"):
        lima_provider.discover_hosts(lima_provider.mngr_ctx.concurrency_group)


def test_discover_hosts_degrades_to_empty_when_limactl_unavailable(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When limactl is unavailable in a recognized way (here: too old to meet the
    minimum version), discovery degrades gracefully -- it returns hosts from local
    records only (all offline) rather than raising, so Lima-less/underprovisioned
    environments still work. With no host records that is an empty list.

    This is the counterpart to the crash case above: a *recognized* unavailability
    (absent/too-old limactl, both ProviderUnavailableError) is swallowed, whereas a
    limactl that runs but fails at runtime is reported as provider-unavailable.
    """
    bin_dir = tmp_path / "bin"
    install_fake_limactl(
        bin_dir,
        'if [ "$1" = "--version" ]; then echo "limactl version 0.9.0"; exit 0; fi\nexit 0\n',
        monkeypatch,
    )

    assert lima_provider.discover_hosts(lima_provider.mngr_ctx.concurrency_group) == []


def test_discover_hosts_maps_unknown_status_to_unknown_and_broken_to_crashed(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """limactl's "Unknown" status means it could not determine the VM's state, so it
    surfaces as UNKNOWN (non-evidence that consumers must not auto-restart off),
    while "Broken" is limactl positively reporting breakage -> CRASHED."""
    prefix = lima_provider.mngr_ctx.config.prefix
    unknown_host_id = HostId.generate()
    broken_host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    for host_id, name in ((unknown_host_id, "unknown-host"), (broken_host_id, "broken-host")):
        lima_provider._host_store.write_host_record(
            HostRecord(
                certified_host_data=CertifiedHostData(
                    host_id=str(host_id),
                    host_name=name,
                    user_tags={},
                    snapshots=[],
                    created_at=now,
                    updated_at=now,
                ),
                config=LimaHostConfig(
                    instance_name=f"{prefix}{host_id}",
                    is_host_data_volume_exposed=False,
                    host_data_disk_name=f"{prefix}{host_id}-data",
                ),
            )
        )
    bin_dir = tmp_path / "bin"
    install_fake_limactl(
        bin_dir,
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\n'
        f'echo \'{{"name": "{prefix}{unknown_host_id}", "status": "Unknown"}}\'\n'
        f'echo \'{{"name": "{prefix}{broken_host_id}", "status": "Broken"}}\'\n',
        monkeypatch,
    )

    discovered = lima_provider.discover_hosts(lima_provider.mngr_ctx.concurrency_group)

    states_by_id = {host.host_id: host.host_state for host in discovered}
    assert states_by_id[unknown_host_id] == HostState.UNKNOWN
    assert states_by_id[broken_host_id] == HostState.CRASHED


def test_provider_capabilities(lima_provider: LimaProviderInstance) -> None:
    assert lima_provider.supports_snapshots is False
    assert lima_provider.supports_shutdown_hosts is True
    assert lima_provider.supports_volumes is True
    assert lima_provider.supports_mutable_tags is True


def test_snapshot_methods_raise(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()

    with pytest.raises(SnapshotsNotSupportedError):
        lima_provider.create_snapshot(host_id, SnapshotName("test"))

    assert lima_provider.list_snapshots(host_id) == []

    with pytest.raises(SnapshotsNotSupportedError):
        lima_provider.delete_snapshot(host_id, SnapshotId("snap-1"))


def test_rename_host_raises_when_record_missing(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    with pytest.raises(HostNotFoundError):
        lima_provider.rename_host(host_id, HostName("new-name"))


def test_rename_host_updates_persisted_host_name(lima_provider: LimaProviderInstance) -> None:
    """Renaming a Lima host rewrites the host name on its record (the instance name is untouched)."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="old-name",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(
            instance_name=f"mngr-{host_id}",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc-data",
        ),
    )
    lima_provider._host_store.write_host_record(record)

    lima_provider.rename_host(host_id, HostName("new-name"))

    updated = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert updated is not None
    assert updated.certified_host_data.host_name == "new-name"
    # The limactl instance name is unchanged (no VM rename).
    assert updated.config is not None
    assert updated.config.instance_name == f"mngr-{host_id}"


def test_tags_crud(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()

    # Initially empty
    assert lima_provider.get_host_tags(host_id) == {}

    # Set tags
    lima_provider.set_host_tags(host_id, {"env": "test", "team": "infra"})
    assert lima_provider.get_host_tags(host_id) == {"env": "test", "team": "infra"}

    # Add tags
    lima_provider.add_tags_to_host(host_id, {"version": "1.0"})
    tags = lima_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "team": "infra", "version": "1.0"}

    # Remove tags
    lima_provider.remove_tags_from_host(host_id, ["team"])
    tags = lima_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "version": "1.0"}


def test_volume_dir_creation(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    volume_dir = lima_provider._ensure_host_volume_dir(host_id)
    assert volume_dir.exists()
    assert volume_dir.is_dir()


def test_list_volumes_empty(lima_provider: LimaProviderInstance) -> None:
    assert lima_provider.list_volumes() == []


def test_get_volume_for_nonexistent_host(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    assert lima_provider.get_volume_for_host(host_id) is None


def test_get_volume_for_existing_host(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    lima_provider._ensure_host_volume_dir(host_id)
    volume = lima_provider.get_volume_for_host(host_id)
    assert volume is not None


def test_get_volume_for_host_returns_none_for_btrfs_mode_record(lima_provider: LimaProviderInstance) -> None:
    """When the host record locks in is_host_data_volume_exposed=False,
    get_volume_for_host returns None even if a stray host-side volume dir
    exists. Callers (events.py, mngr_claude on_before_host_destroy) already
    handle None by skipping or falling back to online-host SSH."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="btrfs-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(
            instance_name="mngr-btrfs-host",
            is_host_data_volume_exposed=False,
            host_data_disk_name="mngr-abc-data",
        ),
    )
    lima_provider._host_store.write_host_record(record)
    # Even if a host-side volume dir is somehow present, the record's False
    # flag must short-circuit the result to None.
    lima_provider._ensure_host_volume_dir(host_id)
    assert lima_provider.get_volume_for_host(host_id) is None


def test_reset_caches(lima_provider: LimaProviderInstance) -> None:
    # Should not raise
    lima_provider.reset_caches()


def test_provider_dir_structure(lima_provider: LimaProviderInstance) -> None:
    # Verify the provider directory structure uses the provider name
    assert "lima-test" in str(lima_provider._provider_dir)
    assert "providers" in str(lima_provider._provider_dir)
    assert "lima" in str(lima_provider._provider_dir)


def test_ensure_host_keypair_creates_and_is_idempotent(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    private_key_path, public_key_path = lima_provider._host_keypair_paths(host_id)
    assert not private_key_path.exists()

    private_pem, public_openssh = lima_provider._ensure_host_keypair(host_id)
    assert "PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")
    assert private_key_path.exists()
    assert public_key_path.exists()

    # A second call must load the existing keypair rather than regenerate it.
    private_pem_again, public_openssh_again = lima_provider._ensure_host_keypair(host_id)
    assert private_pem_again == private_pem
    assert public_openssh_again == public_openssh


def test_record_pre_injected_host_key_writes_known_hosts(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    _, public_openssh = lima_provider._ensure_host_keypair(host_id)

    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60022)

    known_hosts = lima_provider._host_known_hosts_path(host_id).read_text()
    assert "[127.0.0.1]:60022" in known_hosts
    assert public_openssh.strip() in known_hosts


def test_record_pre_injected_host_key_rewrites_on_port_change(lima_provider: LimaProviderInstance) -> None:
    # Lima reassigns the forwarded port across restarts; the per-host known_hosts
    # file must reflect only the current port, with no stale entries from prior ports.
    host_id = HostId.generate()
    lima_provider._ensure_host_keypair(host_id)

    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60022)
    lima_provider._record_pre_injected_host_key(host_id, "127.0.0.1", 60099)

    known_hosts = lima_provider._host_known_hosts_path(host_id).read_text()
    assert "[127.0.0.1]:60099" in known_hosts
    assert "[127.0.0.1]:60022" not in known_hosts
    assert known_hosts.count("\n") == 1


def test_effective_ssh_user_non_root_uses_lima_user(lima_provider: LimaProviderInstance) -> None:
    """The default (non-root) provider connects as Lima's own user and key."""
    ssh_config = LimaSshConfig(
        hostname="127.0.0.1", port=60022, user="josh", identity_file=Path("/home/josh/.lima/key")
    )
    user, identity = lima_provider._effective_ssh_user_and_identity(
        ssh_config, is_run_as_root=False, host_id=HostId.generate()
    )
    assert user == "josh"
    assert identity == Path("/home/josh/.lima/key")


def test_effective_ssh_user_root_uses_root_key(temp_mngr_ctx: MngrContext) -> None:
    """A run-as-root host connects as root using mngr's injected root client key."""
    config = LimaProviderConfig(
        host_dir=Path("/mngr"),
        is_host_data_volume_exposed=False,
        is_run_as_root=True,
        default_idle_timeout=60,
    )
    provider = LimaProviderInstance(
        name=ProviderInstanceName("lima-root-test"),
        host_dir=Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=config,
    )
    ssh_config = LimaSshConfig(
        hostname="127.0.0.1", port=60022, user="josh", identity_file=Path("/home/josh/.lima/key")
    )
    user, identity = provider._effective_ssh_user_and_identity(
        ssh_config, is_run_as_root=True, host_id=HostId.generate()
    )
    assert user == "root"
    # The injected root client key materializes under the host's own keys dir.
    assert identity.name == "root_ssh_key"
    assert identity.exists()


def test_root_ssh_keypair_heals_legacy_shared_pair_into_the_host_keys_dir(
    lima_provider: LimaProviderInstance,
) -> None:
    """A VM from before per-host client keys only authorizes the legacy provider-wide key.

    The fallback must materialize that key inside the host's own keys dir (as a
    symlink), so consumers that derive the pinned-host-keys file as the key's
    sibling ``known_hosts`` land on the per-host pin file lima renders.
    """
    host_id = HostId.generate()
    legacy_path, legacy_public = load_or_create_ssh_keypair(lima_provider._ensure_keys_dir(), "root_ssh_key")

    resolved_path, resolved_public = lima_provider._root_ssh_keypair(host_id)

    assert resolved_path == lima_provider._host_keys_dir(host_id) / "root_ssh_key"
    assert resolved_path.is_symlink()
    assert resolved_path.resolve() == legacy_path.resolve()
    assert resolved_path.read_text() == legacy_path.read_text()
    assert resolved_public == legacy_public
    # The public half is linked alongside, so the per-host pair is complete and
    # wins resolution outright on the next call.
    public_link_path = lima_provider._host_keys_dir(host_id) / "root_ssh_key.pub"
    assert public_link_path.is_symlink()
    second_path, second_public = lima_provider._root_ssh_keypair(host_id)
    assert second_path == resolved_path
    assert second_public == legacy_public


def test_root_ssh_keypair_heal_leaves_an_unrelated_partial_pair_alone(
    lima_provider: LimaProviderInstance,
) -> None:
    """An unrelated private key without its .pub must not be completed with legacy halves.

    Linking the legacy public key next to it would mint a mismatched pair that
    wins resolution but can never authenticate; the resolver must keep using
    the legacy provider-wide pair instead.
    """
    host_id = HostId.generate()
    legacy_path, legacy_public = load_or_create_ssh_keypair(lima_provider._ensure_keys_dir(), "root_ssh_key")
    host_keys_dir = lima_provider._host_keys_dir(host_id)
    host_keys_dir.mkdir(parents=True, exist_ok=True)
    stray_private_path = host_keys_dir / "root_ssh_key"
    stray_private_path.write_text("unrelated-key-material")

    resolved_path, resolved_public = lima_provider._root_ssh_keypair(host_id)

    assert resolved_path == legacy_path
    assert resolved_public == legacy_public
    # The stray file is untouched and no legacy .pub was linked alongside it.
    assert stray_private_path.read_text() == "unrelated-key-material"
    assert not (host_keys_dir / "root_ssh_key.pub").exists()


def test_root_ssh_keypair_prefers_per_host_pair_over_legacy(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    load_or_create_ssh_keypair(lima_provider._ensure_keys_dir(), "root_ssh_key")
    per_host_path, per_host_public = lima_provider._create_per_host_root_ssh_keypair(host_id)

    resolved_path, resolved_public = lima_provider._root_ssh_keypair(host_id)

    assert resolved_path == per_host_path
    assert resolved_public == per_host_public


def test_create_per_host_root_ssh_keypair_is_unique_per_host(lima_provider: LimaProviderInstance) -> None:
    _, first_public = lima_provider._create_per_host_root_ssh_keypair(HostId.generate())
    _, second_public = lima_provider._create_per_host_root_ssh_keypair(HostId.generate())
    assert first_public != second_public


def test_delete_host_removes_keypair_dir(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    lima_provider._ensure_host_keypair(host_id)
    host_keys_dir = lima_provider._host_keys_dir(host_id)
    assert host_keys_dir.exists()

    now = datetime.now(timezone.utc)
    host_record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="test-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        )
    )
    lima_provider._host_store.write_host_record(host_record)

    lima_provider.delete_host(lima_provider._create_offline_host(host_record))
    assert not host_keys_dir.exists()


def _write_active_record(
    provider: LimaProviderInstance,
    host_name: str,
    is_creation_in_progress: bool = False,
    failure_reason: str | None = None,
    stop_reason: str | None = None,
) -> HostId:
    """Write a host record shaped like the given lifecycle situation, returning its id."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    provider._host_store.write_host_record(
        HostRecord(
            certified_host_data=CertifiedHostData(
                host_id=str(host_id),
                host_name=host_name,
                user_tags={},
                snapshots=[],
                failure_reason=failure_reason,
                stop_reason=stop_reason,
                created_at=now,
                updated_at=now,
            ),
            config=LimaHostConfig(instance_name=f"mngr-{host_id.get_uuid().hex}") if failure_reason is None else None,
            is_creation_in_progress=is_creation_in_progress,
        )
    )
    return host_id


def test_find_conflicting_host_record_matches_active_and_skips_failed_and_destroyed(
    lima_provider: LimaProviderInstance,
) -> None:
    _write_active_record(lima_provider, "failed-host", failure_reason="limactl start failed")
    _write_active_record(lima_provider, "destroyed-host", stop_reason=HostState.DESTROYED.value)
    active_id = _write_active_record(lima_provider, "active-host")
    records = lima_provider._host_store.list_all_host_records()

    assert _find_conflicting_host_record(HostName("failed-host"), records) is None
    assert _find_conflicting_host_record(HostName("destroyed-host"), records) is None
    conflicting = _find_conflicting_host_record(HostName("active-host"), records)
    assert conflicting is not None
    assert conflicting.certified_host_data.host_id == str(active_id)


def test_record_state_for_conflict_message_distinguishes_building_and_stopped(
    lima_provider: LimaProviderInstance,
) -> None:
    _write_active_record(lima_provider, "building-host", is_creation_in_progress=True)
    _write_active_record(lima_provider, "stopped-host", stop_reason=HostState.STOPPED.value)
    records = {r.certified_host_data.host_name: r for r in lima_provider._host_store.list_all_host_records()}

    assert _record_state_for_conflict_message(records["building-host"]) == "BUILDING"
    assert _record_state_for_conflict_message(records["stopped-host"]) == "STOPPED"


def test_create_host_raises_conflict_with_remediation_for_building_reservation(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name held by an in-flight (or abandoned) create conflicts, and the error
    tells the user how to clear the abandoned reservation."""
    monkeypatch.setenv("LIMA_HOME", "/tmp/lima-test-home")
    reservation_id = _write_active_record(lima_provider, "held-name", is_creation_in_progress=True)
    bin_dir = tmp_path / "bin"
    install_fake_limactl(
        bin_dir,
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\nexit 0\n',
        monkeypatch,
    )

    with pytest.raises(HostNameConflictError) as exc_info:
        lima_provider.create_host(name=HostName("held-name"))

    message = str(exc_info.value)
    assert str(reservation_id) in message
    assert "BUILDING" in message
    assert f"mngr destroy @{reservation_id}" in message


def test_create_host_conflicts_with_stopped_host_without_remediation(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIMA_HOME", "/tmp/lima-test-home")
    _write_active_record(lima_provider, "stopped-name", stop_reason=HostState.STOPPED.value)
    bin_dir = tmp_path / "bin"
    install_fake_limactl(
        bin_dir,
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\nexit 0\n',
        monkeypatch,
    )

    with pytest.raises(HostNameConflictError) as exc_info:
        lima_provider.create_host(name=HostName("stopped-name"))

    message = str(exc_info.value)
    assert "STOPPED" in message
    assert "mngr destroy @" not in message


def _install_logging_fake_limactl(
    bin_dir: Path,
    invocation_log: Path,
    start_stderr: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install a fake limactl that logs every invocation and fails `start` with the given stderr."""
    install_fake_limactl(
        bin_dir,
        f'echo "$@" >> "{invocation_log}"\n'
        'case "$*" in\n'
        '  *--version*) echo "limactl version 2.0.3"; exit 0;;\n'
        f'  *" start "*) echo "{start_stderr}" >&2; exit 1;;\n'
        "  *) exit 0;;\n"
        "esac\n",
        monkeypatch,
    )


def test_create_host_does_not_retry_permanent_download_failure(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 on the base image fails the create immediately (no retry), tears the
    instance down, and leaves a FAILED record -- which frees the name for reuse."""
    monkeypatch.setenv("LIMA_HOME", "/tmp/lima-test-home")
    invocation_log = tmp_path / "invocations.log"
    _install_logging_fake_limactl(
        tmp_path / "bin",
        invocation_log,
        "failed to download: unexpected HTTP status Not Found",
        monkeypatch,
    )

    with pytest.raises(LimaHostCreationError):
        lima_provider.create_host(name=HostName("no-image-host"))

    invocations = invocation_log.read_text().splitlines()
    start_count = sum(1 for line in invocations if " start " in f" {line} ")
    assert start_count == 1
    # The reservation was replaced by a FAILED record, so the name is reusable.
    records = lima_provider._host_store.list_all_host_records()
    assert _find_conflicting_host_record(HostName("no-image-host"), records) is None
    failed_records = [r for r in records if r.certified_host_data.host_name == "no-image-host"]
    assert len(failed_records) == 1
    assert failed_records[0].certified_host_data.failure_reason is not None


def test_create_host_failure_before_limactl_start_frees_reserved_name(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the name reservation but before `limactl start` (here: a
    missing user `--file` Lima YAML) replaces the BUILDING reservation with a
    FAILED record, so the name stays reusable instead of being stranded."""
    monkeypatch.setenv("LIMA_HOME", "/tmp/lima-test-home")
    install_fake_limactl(
        tmp_path / "bin",
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\nexit 0\n',
        monkeypatch,
    )

    with pytest.raises(LimaHostCreationError):
        lima_provider.create_host(
            name=HostName("bad-user-file-host"),
            build_args=("--file", str(tmp_path / "does-not-exist.yaml")),
        )

    records = lima_provider._host_store.list_all_host_records()
    assert _find_conflicting_host_record(HostName("bad-user-file-host"), records) is None
    failed_records = [r for r in records if r.certified_host_data.host_name == "bad-user-file-host"]
    assert len(failed_records) == 1
    assert failed_records[0].certified_host_data.failure_reason is not None
    assert failed_records[0].is_creation_in_progress is False


def test_create_host_retries_transient_download_failure_once(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient download failure retries `limactl start` exactly once, deleting
    the half-created instance between attempts."""
    monkeypatch.setenv("LIMA_HOME", "/tmp/lima-test-home")
    invocation_log = tmp_path / "invocations.log"
    _install_logging_fake_limactl(
        tmp_path / "bin",
        invocation_log,
        "failed to download: net/http: TLS handshake timeout",
        monkeypatch,
    )

    with pytest.raises(LimaHostCreationError):
        lima_provider.create_host(name=HostName("flaky-download-host"))

    invocations = invocation_log.read_text().splitlines()
    start_lines = [line for line in invocations if " start " in f" {line} "]
    delete_lines = [line for line in invocations if line.startswith("delete ")]
    assert len(start_lines) == 2
    # One delete between the attempts, one from the final failure teardown.
    assert len(delete_lines) == 2


def test_discover_hosts_reports_building_for_reservation_record(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name-reservation record shows as BUILDING whether or not its VM exists yet,
    while a destroyed reservation shows as DESTROYED (not BUILDING)."""
    building_id = _write_active_record(lima_provider, "mid-create-host", is_creation_in_progress=True)
    destroyed_id = _write_active_record(
        lima_provider, "dead-create-host", is_creation_in_progress=True, stop_reason=HostState.DESTROYED.value
    )
    install_fake_limactl(
        tmp_path / "bin",
        'if [ "$1" = "--version" ]; then echo "limactl version 2.0.3"; exit 0; fi\nexit 0\n',
        monkeypatch,
    )

    discovered = lima_provider.discover_hosts(lima_provider.mngr_ctx.concurrency_group, include_destroyed=True)

    states_by_id = {host.host_id: host.host_state for host in discovered}
    assert states_by_id[building_id] == HostState.BUILDING
    assert states_by_id[destroyed_id] == HostState.DESTROYED


def test_delete_host_deletes_vm_definition_and_record(
    lima_provider: LimaProviderInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_host removes the Lima VM definition itself (not just records/disk), so
    gc's offline path cannot strand a VM in `limactl list`."""
    host_id = _write_active_record(lima_provider, "stranded-host", stop_reason=HostState.STOPPED.value)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    instance_name = record.config.instance_name
    invocation_log = tmp_path / "invocations.log"
    install_fake_limactl(
        tmp_path / "bin",
        f'echo "$@" >> "{invocation_log}"\nexit 0\n',
        monkeypatch,
    )

    lima_provider.delete_host(lima_provider.to_offline_host(host_id))

    invocations = invocation_log.read_text()
    assert f"delete --force {instance_name}" in invocations
    assert lima_provider._host_store.read_host_record(host_id, use_cache=False) is None


def _record_with_host_dir(host_id: HostId, host_dir: str | None) -> HostRecord:
    now = datetime.now(timezone.utc)
    return HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="probe-host",
            user_tags={},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        config=LimaHostConfig(instance_name=f"mngr-{host_id}", host_dir=host_dir),
    )


def test_recorded_host_dir_override_prefers_the_record_over_ambient_config() -> None:
    """A host created with a custom host_dir is read at that path, not the current config's."""
    record = _record_with_host_dir(HostId.generate(), "/home/user/.mngr")

    assert _recorded_host_dir_override(record) == Path("/home/user/.mngr")


def test_recorded_host_dir_override_is_none_for_legacy_records() -> None:
    """Records written before host_dir was persisted fall back to the provider instance."""
    record = _record_with_host_dir(HostId.generate(), None)

    assert _recorded_host_dir_override(record) is None


def test_offline_host_reads_at_the_recorded_host_dir(lima_provider: LimaProviderInstance) -> None:
    """The offline host targets the recorded host_dir even when provider config says otherwise."""
    record = _record_with_host_dir(HostId.generate(), "/home/user/.mngr")

    offline_host = lima_provider._create_offline_host(record)

    assert offline_host.host_dir == Path("/home/user/.mngr")
    assert lima_provider.host_dir != Path("/home/user/.mngr")


def test_host_log_dir_follows_the_hosts_own_host_dir(lima_provider: LimaProviderInstance) -> None:
    """Service logs default beside the data the host uses, not the ambient provider host_dir."""
    recorded_host_dir = Path("/home/user/.mngr")

    assert lima_provider._host_log_dir_str(recorded_host_dir) == "/home/user/.mngr/logs"
    assert lima_provider._host_log_dir_str(lima_provider.host_dir) == "/mngr/logs"


def test_offline_host_log_dir_uses_the_recorded_host_dir(lima_provider: LimaProviderInstance) -> None:
    """A host created with a custom host_dir resolves its log dir there from any context."""
    record = _record_with_host_dir(HostId.generate(), "/home/user/.mngr")

    offline_host = lima_provider._create_offline_host(record)

    assert lima_provider._host_log_dir_str(offline_host.host_dir) == "/home/user/.mngr/logs"


def _write_sized_record(
    provider: LimaProviderInstance,
    host_name: str,
    start_args: tuple[str, ...],
    host_data_disk_name: str | None,
    host_data_disk_size: str | None,
    resources: HostResources | None,
) -> HostId:
    """Write a completed host record carrying the given size, returning its id."""
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    provider._host_store.write_host_record(
        HostRecord(
            certified_host_data=CertifiedHostData(
                host_id=str(host_id), host_name=host_name, user_tags={}, snapshots=[], created_at=now, updated_at=now
            ),
            ssh_hostname="127.0.0.1",
            config=LimaHostConfig(
                instance_name=f"mngr-{host_id.get_uuid().hex}",
                start_args=start_args,
                host_data_disk_name=host_data_disk_name,
                host_data_disk_size=LimaDiskSize(host_data_disk_size) if host_data_disk_size is not None else None,
                is_host_data_volume_exposed=host_data_disk_name is None,
            ),
            resources=resources,
        )
    )
    return host_id


def _install_fake_limactl_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    instance_name: str,
    status: str,
    cpus: int,
    memory_gib: int,
    disk_name: str,
    disk_gib: int,
) -> Path:
    """A limactl whose ``list`` and ``disk list`` report one instance and one disk, logging every other call."""
    invocation_log = tmp_path / "invocations.log"
    memory_bytes = memory_gib * 1024**3
    disk_bytes = disk_gib * 1024**3
    install_fake_limactl(
        tmp_path / "bin",
        f'echo "$@" >> "{invocation_log}"\n'
        'if [ "$1" = "--version" ]; then echo "limactl version 2.1.2"; exit 0; fi\n'
        'if [ "$1" = "list" ]; then echo \'{"name": "'
        f"{instance_name}"
        '", "status": "'
        f"{status}"
        '", "cpus": '
        f"{cpus}"
        ', "memory": '
        f"{memory_bytes}"
        ', "disk": 21474836480}\'; exit 0; fi\n'
        'if [ "$1" = "disk" ] && [ "$2" = "list" ]; then echo \'{"name": "'
        f"{disk_name}"
        '", "size": '
        f"{disk_bytes}"
        "}'; exit 0; fi\n"
        "exit 0\n",
        monkeypatch,
    )
    return invocation_log


def test_get_host_resources_reports_the_recorded_size(lima_provider: LimaProviderInstance) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "sized-host", ("--cpus=2",), "mngr-x-data", "100GiB", recorded)

    assert lima_provider.get_host_resources(lima_provider.to_offline_host(host_id)) == recorded


def test_get_host_resources_derives_a_legacy_record_from_its_start_args_and_configured_disk(
    lima_provider: LimaProviderInstance,
) -> None:
    """A record written before sizes were recorded honestly answers from its start args and the provider's disk size."""
    host_id = _write_sized_record(
        lima_provider, "legacy-host", ("--cpus=2", "--memory=4", "--disk=20"), "mngr-x-data", None, None
    )

    resources = lima_provider.get_host_resources(lima_provider.to_offline_host(host_id))

    assert resources.cpu.count == 2
    assert resources.memory_gb == 4.0
    assert resources.disk_gb == 100.0


def test_start_args_win_over_the_placeholder_size_older_records_hold(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record from before honest size recording holds 4 CPU / 4 GiB whatever its start args say; the VM must not be edited down to it."""
    placeholder = HostResources(cpu=CpuResources(count=4), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(
        lima_provider, "older-host", ("--cpus=8", "--memory=16", "--disk=20"), None, None, placeholder
    )
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    invocation_log = _install_fake_limactl_reporting(
        tmp_path, monkeypatch, record.config.instance_name, "Stopped", 8, 16, "unrelated-data", 1
    )

    resources = lima_provider.get_host_resources(lima_provider.to_offline_host(host_id))
    assert resources == HostResources(cpu=CpuResources(count=8), memory_gb=16.0, disk_gb=20.0, gpu=None)

    assert lima_provider._apply_recorded_size(record) is True
    assert "edit" not in invocation_log.read_text()


def test_get_host_resources_raises_for_an_unknown_host(lima_provider: LimaProviderInstance) -> None:
    host_id = HostId.generate()
    with pytest.raises(HostNotFoundError):
        lima_provider.get_host_resources(lima_provider.to_offline_host(host_id))


def test_resize_host_reconfigures_a_stopped_vm_and_grows_its_disk_before_recording(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(
        lima_provider, "stopped-host", ("--cpus=2", "--memory=4", "--disk=20"), "mngr-x-data", "100GiB", recorded
    )
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    invocation_log = _install_fake_limactl_reporting(
        tmp_path, monkeypatch, record.config.instance_name, "Stopped", 2, 4, "mngr-x-data", 100
    )

    outcome = lima_provider.resize_host(
        host_id,
        LimaSizeRequest(cpus=LimaCpuCount(4), memory_gib=LimaMemoryGib(8), data_disk_size=LimaDiskSize("200GiB")),
    )

    assert outcome.is_applied_to_instance is True
    assert outcome.resources == HostResources(cpu=CpuResources(count=4), memory_gb=8.0, disk_gb=200.0, gpu=None)
    invocations = invocation_log.read_text()
    assert f"edit --tty=false --cpus=4 --memory=8 {record.config.instance_name}" in invocations
    assert "disk resize mngr-x-data --size 200GiB" in invocations
    rewritten = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert rewritten is not None and rewritten.config is not None
    assert rewritten.config.start_args == ("--disk=20", "--cpus=4", "--memory=8")
    assert rewritten.config.host_data_disk_size == "200GiB"
    assert rewritten.resources == outcome.resources


def test_resize_host_only_records_for_a_running_vm(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "running-host", ("--cpus=2",), "mngr-x-data", "100GiB", recorded)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    invocation_log = _install_fake_limactl_reporting(
        tmp_path, monkeypatch, record.config.instance_name, "Running", 2, 4, "mngr-x-data", 100
    )

    outcome = lima_provider.resize_host(host_id, LimaSizeRequest(memory_gib=LimaMemoryGib(8)))

    assert outcome.is_applied_to_instance is False
    assert outcome.resources.memory_gb == 8.0
    assert outcome.resources.cpu.count == 2
    assert "edit" not in invocation_log.read_text()
    rewritten = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert rewritten is not None and rewritten.config is not None
    assert rewritten.config.start_args == ("--cpus=2", "--memory=8")


def test_resize_host_keeps_the_boot_disk_of_an_exposed_layout_host(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no data disk the reported disk is the boot disk, which a CPU or memory resize must not touch."""
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=20.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "exposed-host", ("--cpus=2", "--disk=20"), None, None, recorded)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    _install_fake_limactl_reporting(tmp_path, monkeypatch, record.config.instance_name, "Running", 2, 4, "none", 1)

    outcome = lima_provider.resize_host(host_id, LimaSizeRequest(cpus=LimaCpuCount(4)))

    assert outcome.resources == HostResources(cpu=CpuResources(count=4), memory_gb=4.0, disk_gb=20.0, gpu=None)


def test_resize_host_leaves_the_record_alone_when_lima_refuses(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "refused-host", ("--cpus=2",), "mngr-x-data", "100GiB", recorded)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    install_fake_limactl(
        tmp_path / "bin",
        'if [ "$1" = "list" ]; then echo \'{"name": "'
        f"{record.config.instance_name}"
        '", "status": "Stopped", "cpus": 2, "memory": 4294967296, "disk": 1}\'; exit 0; fi\n'
        'if [ "$1" = "edit" ]; then echo "memory too large" >&2; exit 1; fi\nexit 0\n',
        monkeypatch,
    )

    with pytest.raises(MngrError, match="Lima refused the recorded size"):
        lima_provider.resize_host(host_id, LimaSizeRequest(memory_gib=LimaMemoryGib(999)))

    unchanged = lima_provider._host_store.read_host_record(host_id, use_cache=False)
    assert unchanged is not None
    assert unchanged.resources == recorded


def test_resize_host_refuses_a_disk_shrink_and_a_disk_on_the_exposed_layout(
    lima_provider: LimaProviderInstance,
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    btrfs_id = _write_sized_record(lima_provider, "btrfs-host", (), "mngr-x-data", "100GiB", recorded)
    exposed_id = _write_sized_record(lima_provider, "exposed-host", (), None, None, recorded)

    with pytest.raises(LimaResizeRefusedError, match="never shrinks"):
        lima_provider.resize_host(btrfs_id, LimaSizeRequest(data_disk_size=LimaDiskSize("50GiB")))
    with pytest.raises(LimaResizeRefusedError, match="no data disk"):
        lima_provider.resize_host(exposed_id, LimaSizeRequest(data_disk_size=LimaDiskSize("200GiB")))
    with pytest.raises(HostNotFoundError):
        lima_provider.resize_host(HostId.generate(), LimaSizeRequest(cpus=LimaCpuCount(2)))


def test_apply_recorded_size_skips_an_instance_already_at_the_recorded_size(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "settled-host", ("--cpus=2",), "mngr-x-data", "100GiB", recorded)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    invocation_log = _install_fake_limactl_reporting(
        tmp_path, monkeypatch, record.config.instance_name, "Stopped", 2, 4, "mngr-x-data", 100
    )

    assert lima_provider._apply_recorded_size(record) is True

    invocations = invocation_log.read_text()
    assert "edit" not in invocations
    assert "disk resize" not in invocations


def test_apply_recorded_size_warns_instead_of_shrinking_a_larger_disk(
    lima_provider: LimaProviderInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = HostResources(cpu=CpuResources(count=2), memory_gb=4.0, disk_gb=100.0, gpu=None)
    host_id = _write_sized_record(lima_provider, "shrunk-host", ("--cpus=2",), "mngr-x-data", "100GiB", recorded)
    record = lima_provider._host_store.read_host_record(host_id)
    assert record is not None and record.config is not None
    invocation_log = _install_fake_limactl_reporting(
        tmp_path, monkeypatch, record.config.instance_name, "Stopped", 2, 4, "mngr-x-data", 300
    )

    with capture_loguru() as captured:
        lima_provider._apply_recorded_size(record)

    assert "disk resize" not in invocation_log.read_text()
    assert "never shrinks" in captured.getvalue()
