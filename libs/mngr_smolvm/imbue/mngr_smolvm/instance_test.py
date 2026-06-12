from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_smolvm.errors import SmolvmCommandError
from imbue.mngr_smolvm.errors import SmolvmHostRenameError
from imbue.mngr_smolvm.host_store import HostRecord
from imbue.mngr_smolvm.host_store import SmolvmMachineConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance
from imbue.mngr_smolvm.instance import _find_pass_through_arg_value
from imbue.mngr_smolvm.instance import _parse_build_args
from imbue.mngr_smolvm.testing import make_smolvm_provider


def _write_pack_stub(tmp_path: Path, calls_log: Path, is_failing: bool) -> Path:
    """Write a stub smolvm binary handling `pack create ... -o <output>`.

    The stub records each invocation in calls_log and emits the stub +
    sidecar pair the way the real `smolvm pack create -o PATH` does. The
    failing variant writes only a partial sidecar and exits non-zero,
    simulating an interrupted pack.
    """
    failure_tail = "exit 1" if is_failing else ""
    sidecar_content = "partial" if is_failing else "sidecar-content"
    script = f"""#!/bin/sh
echo "$@" >> {calls_log}
out=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-o" ]; then out="$arg"; fi
    prev="$arg"
done
printf 'stub' > "$out"
printf '{sidecar_content}' > "$out.smolmachine"
{failure_tail}
"""
    stub_path = tmp_path / "smolvm-stub"
    stub_path.write_text(script)
    stub_path.chmod(0o755)
    return stub_path


def test_pack_from_archive_cached_packs_atomically_and_caches(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A packed archive lands at its content-hash cache path with no temp
    leftovers, and a second call is served from the cache without invoking
    smolvm again."""
    calls_log = tmp_path / "calls.log"
    stub_path = _write_pack_stub(tmp_path, calls_log, is_failing=False)
    provider = make_smolvm_provider(temp_mngr_ctx, smolvm_command=str(stub_path))
    archive_path = tmp_path / "image.tar"
    archive_path.write_bytes(b"fake-archive")

    sidecar_path = provider._pack_from_archive_cached(archive_path)

    assert sidecar_path.exists()
    assert sidecar_path.parent == provider._packs_dir
    assert sidecar_path.read_text() == "sidecar-content"
    # The temp output and temp sidecar were cleaned up: the cache dir holds
    # only the final sidecar.
    assert list(provider._packs_dir.iterdir()) == [sidecar_path]
    assert len(calls_log.read_text().splitlines()) == 1

    # Second call: cache hit, smolvm not invoked again.
    assert provider._pack_from_archive_cached(archive_path) == sidecar_path
    assert len(calls_log.read_text().splitlines()) == 1


def test_pack_from_archive_failure_does_not_poison_cache(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """A failed pack create must not leave anything at the cache path (which
    later calls would treat as a valid cached pack) nor any temp files."""
    calls_log = tmp_path / "calls.log"
    stub_path = _write_pack_stub(tmp_path, calls_log, is_failing=True)
    provider = make_smolvm_provider(temp_mngr_ctx, smolvm_command=str(stub_path))
    archive_path = tmp_path / "image.tar"
    archive_path.write_bytes(b"fake-archive")

    with pytest.raises(SmolvmCommandError):
        provider._pack_from_archive_cached(archive_path)

    assert list(provider._packs_dir.iterdir()) == []

    # The cache did not record the failure: a later attempt retries the pack.
    with pytest.raises(SmolvmCommandError):
        provider._pack_from_archive_cached(archive_path)
    assert len(calls_log.read_text().splitlines()) == 2


def test_provider_capabilities(smolvm_provider: SmolvmProviderInstance) -> None:
    assert smolvm_provider.supports_snapshots is False
    assert smolvm_provider.supports_shutdown_hosts is True
    assert smolvm_provider.supports_volumes is True
    assert smolvm_provider.supports_mutable_tags is True


def test_snapshot_methods_raise(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()

    with pytest.raises(SnapshotsNotSupportedError):
        smolvm_provider.create_snapshot(host_id, SnapshotName("test"))

    assert smolvm_provider.list_snapshots(host_id) == []

    with pytest.raises(SnapshotsNotSupportedError):
        smolvm_provider.delete_snapshot(host_id, SnapshotId("snap-1"))


def test_rename_host_raises(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    with pytest.raises(SmolvmHostRenameError):
        smolvm_provider.rename_host(host_id, HostName("new-name"))


def test_tags_crud(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()

    # Initially empty
    assert smolvm_provider.get_host_tags(host_id) == {}

    # Set tags
    smolvm_provider.set_host_tags(host_id, {"env": "test", "team": "infra"})
    assert smolvm_provider.get_host_tags(host_id) == {"env": "test", "team": "infra"}

    # Add tags
    smolvm_provider.add_tags_to_host(host_id, {"version": "1.0"})
    tags = smolvm_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "team": "infra", "version": "1.0"}

    # Remove tags
    smolvm_provider.remove_tags_from_host(host_id, ["team"])
    tags = smolvm_provider.get_host_tags(host_id)
    assert tags == {"env": "test", "version": "1.0"}


def test_volume_dir_creation(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    volume_dir = smolvm_provider._ensure_host_volume_dir(host_id)
    assert volume_dir.exists()
    assert volume_dir.is_dir()


def test_list_volumes_empty(smolvm_provider: SmolvmProviderInstance) -> None:
    assert smolvm_provider.list_volumes() == []


def test_get_volume_for_nonexistent_host(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    assert smolvm_provider.get_volume_for_host(host_id) is None


def test_get_volume_for_existing_host(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    smolvm_provider._ensure_host_volume_dir(host_id)
    volume = smolvm_provider.get_volume_for_host(host_id)
    assert volume is not None


def test_get_volume_for_host_returns_none_for_btrfs_mode_record(smolvm_provider: SmolvmProviderInstance) -> None:
    """When the host record locks in is_host_data_volume_exposed=False,
    get_volume_for_host returns None even if a stray host-side volume dir
    exists."""
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
        config=SmolvmMachineConfig(
            machine_name="mngr-btrfs-host",
            ssh_host_port=2222,
            is_host_data_volume_exposed=False,
            data_disk_spec="size=100,target=/mngr",
        ),
    )
    smolvm_provider._host_store.write_host_record(record)
    smolvm_provider._ensure_host_volume_dir(host_id)
    assert smolvm_provider.get_volume_for_host(host_id) is None


def test_parse_build_args_empty() -> None:
    parsed = _parse_build_args(())
    assert parsed.image_archive is None
    assert parsed.from_pack is None


def test_parse_build_args_image_archive() -> None:
    parsed = _parse_build_args(("--image-archive", "/tmp/img.tar"))
    assert parsed.image_archive is not None
    assert str(parsed.image_archive) == "/tmp/img.tar"
    assert parsed.from_pack is None


def test_parse_build_args_from_pack() -> None:
    parsed = _parse_build_args(("--from", "/tmp/x.smolmachine"))
    assert parsed.from_pack is not None
    assert str(parsed.from_pack) == "/tmp/x.smolmachine"


def test_parse_build_args_dockerfile_inline_form() -> None:
    parsed = _parse_build_args(("--dockerfile=Dockerfile",))
    assert parsed.dockerfile is not None
    assert str(parsed.dockerfile) == "Dockerfile"


def test_parse_build_args_rejects_both_sources() -> None:
    with pytest.raises(MngrError, match="mutually exclusive"):
        _parse_build_args(("--image-archive", "/tmp/img.tar", "--from", "/tmp/x.smolmachine"))


def test_parse_build_args_rejects_unknown_arg() -> None:
    with pytest.raises(MngrError, match="Unsupported smolvm build arg"):
        _parse_build_args(("--bogus",))


def test_parse_build_args_rejects_missing_value() -> None:
    with pytest.raises(MngrError, match="requires a PATH"):
        _parse_build_args(("--image-archive",))


def test_parse_build_args_rejects_empty_inline_value() -> None:
    with pytest.raises(MngrError, match="requires a PATH"):
        _parse_build_args(("--dockerfile=",))


def test_find_pass_through_arg_value_absent() -> None:
    assert _find_pass_through_arg_value(("--storage", "20"), "--cpus") == (False, None)


def test_find_pass_through_arg_value_separate_form() -> None:
    assert _find_pass_through_arg_value(("--storage", "20", "--cpus", "8"), "--cpus") == (True, "8")


def test_find_pass_through_arg_value_inline_form() -> None:
    assert _find_pass_through_arg_value(("--mem=2048",), "--mem") == (True, "2048")


def test_find_pass_through_arg_value_missing_value() -> None:
    # Malformed trailing flag: present, but with no value (smolvm itself
    # rejects this loudly).
    assert _find_pass_through_arg_value(("--cpus",), "--cpus") == (True, None)


def test_reset_caches(smolvm_provider: SmolvmProviderInstance) -> None:
    # Should not raise
    smolvm_provider.reset_caches()


def test_provider_dir_structure(smolvm_provider: SmolvmProviderInstance) -> None:
    assert "smolvm-test" in str(smolvm_provider._provider_dir)
    assert "providers" in str(smolvm_provider._provider_dir)
    assert "smolvm" in str(smolvm_provider._provider_dir)


def test_ensure_host_keypair_creates_and_is_idempotent(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    private_key_path, public_key_path = smolvm_provider._host_keypair_paths(host_id)
    assert not private_key_path.exists()

    returned_private_key_path, public_openssh = smolvm_provider._ensure_host_keypair(host_id)
    assert returned_private_key_path == private_key_path
    private_pem = private_key_path.read_text()
    assert "PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")
    assert public_key_path.exists()

    # Idempotent: a second call returns the same key
    returned_private_key_path_2, public_openssh_2 = smolvm_provider._ensure_host_keypair(host_id)
    assert returned_private_key_path_2 == private_key_path
    assert private_key_path.read_text() == private_pem
    assert public_openssh_2 == public_openssh


def test_host_record_roundtrip_via_store(smolvm_provider: SmolvmProviderInstance) -> None:
    host_id = HostId.generate()
    now = datetime.now(timezone.utc)
    record = HostRecord(
        certified_host_data=CertifiedHostData(
            host_id=str(host_id),
            host_name="roundtrip-host",
            user_tags={"a": "b"},
            snapshots=[],
            created_at=now,
            updated_at=now,
        ),
        ssh_hostname="127.0.0.1",
        ssh_port=43210,
        ssh_user="root",
        config=SmolvmMachineConfig(
            machine_name="mngr-roundtrip-host",
            ssh_host_port=43210,
            image="alpine:3.19",
        ),
    )
    smolvm_provider._host_store.write_host_record(record)
    loaded = smolvm_provider._host_store.read_host_record(host_id, use_cache=False)
    assert loaded is not None
    assert loaded.config is not None
    assert loaded.config.machine_name == "mngr-roundtrip-host"
    assert loaded.config.ssh_host_port == 43210
    assert loaded.ssh_user == "root"
