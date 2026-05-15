import pytest

from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.utils.testing import find_free_port
from imbue.mngr_lima.errors import LimaHostRenameError
from imbue.mngr_lima.instance import LimaProviderInstance
from imbue.mngr_lima.instance import _parse_keyscan_lines
from imbue.mngr_lima.instance import _parse_size_to_gb


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


def test_rename_host_raises(lima_provider: LimaProviderInstance) -> None:
    from imbue.mngr.primitives import HostName

    host_id = HostId.generate()
    with pytest.raises(LimaHostRenameError):
        lima_provider.rename_host(host_id, HostName("new-name"))


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


def test_parse_size_to_gb() -> None:
    assert _parse_size_to_gb("4GiB") == 4.0
    assert _parse_size_to_gb("512MiB") == 0.5
    assert _parse_size_to_gb("1TiB") == 1024.0
    assert _parse_size_to_gb("8") == 8.0
    assert _parse_size_to_gb("invalid") == 4.0  # default fallback


def test_reset_caches(lima_provider: LimaProviderInstance) -> None:
    # Should not raise
    lima_provider.reset_caches()


def test_provider_dir_structure(lima_provider: LimaProviderInstance) -> None:
    # Verify the provider directory structure uses the provider name
    assert "lima-test" in str(lima_provider._provider_dir)
    assert "providers" in str(lima_provider._provider_dir)
    assert "lima" in str(lima_provider._provider_dir)


def test_parse_keyscan_lines_empty() -> None:
    assert _parse_keyscan_lines("") == []
    assert _parse_keyscan_lines("\n\n") == []


def test_parse_keyscan_lines_skips_comments_and_blanks() -> None:
    stdout = "# 127.0.0.1:2222 SSH-2.0-OpenSSH_9.6p1\n\n# another comment\n"
    assert _parse_keyscan_lines(stdout) == []


def test_parse_keyscan_lines_multi_key() -> None:
    stdout = (
        "# [127.0.0.1]:2222 SSH-2.0-OpenSSH_9.6p1\n"
        "[127.0.0.1]:2222 ssh-rsa AAAARSAKEY\n"
        "[127.0.0.1]:2222 ecdsa-sha2-nistp256 AAAAECDSAKEY\n"
        "[127.0.0.1]:2222 ssh-ed25519 AAAAED25519KEY\n"
    )
    assert _parse_keyscan_lines(stdout) == [
        ("ssh-rsa", "AAAARSAKEY"),
        ("ecdsa-sha2-nistp256", "AAAAECDSAKEY"),
        ("ssh-ed25519", "AAAAED25519KEY"),
    ]


def test_parse_keyscan_lines_skips_malformed() -> None:
    # Fewer than 3 whitespace-split tokens means we cannot identify a key.
    stdout = "only-one-field\nhost-only ssh-rsa\n[127.0.0.1]:2222 ssh-rsa AAAAGOOD\n"
    assert _parse_keyscan_lines(stdout) == [("ssh-rsa", "AAAAGOOD")]


def test_record_host_keys_raises_when_unreachable(lima_provider: LimaProviderInstance) -> None:
    # Drive the polling-then-raise path with a tight budget: ssh-keyscan to a
    # closed loopback port returns ECONNREFUSED fast on every attempt, so the
    # poll exhausts its budget and the typed error is raised.
    lima_provider._keys_dir.mkdir(parents=True, exist_ok=True)
    closed_port = find_free_port()
    with pytest.raises(HostConnectionError, match="ssh-keyscan could not read a host key"):
        lima_provider._record_host_keys("127.0.0.1", closed_port, timeout=0.5, poll_interval=0.1)
