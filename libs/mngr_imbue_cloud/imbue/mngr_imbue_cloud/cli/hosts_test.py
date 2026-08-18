from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.hosts import _find_host_state_dirs
from imbue.mngr_imbue_cloud.cli.hosts import _find_lease_by_ref
from imbue.mngr_imbue_cloud.cli.hosts import hosts
from imbue.mngr_imbue_cloud.primitives import LeaseDbId
from imbue.mngr_imbue_cloud.wire_types import LeasedHostInfo

_HOST_ID = "host-" + "a" * 32


def _make_lease(host_id: str, host_name: str) -> LeasedHostInfo:
    return LeasedHostInfo(
        host_db_id=LeaseDbId("11111111-2222-3333-4444-555555555555"),
        vps_address="203.0.113.5",
        ssh_port=22010,
        ssh_user="root",
        container_ssh_port=22011,
        agent_id="agent-" + "b" * 32,
        host_id=host_id,
        host_name=host_name,
        attributes={},
        leased_at="2026-01-01T00:00:00Z",
    )


def test_hosts_group_lists_the_rotate_subcommand() -> None:
    result = CliRunner().invoke(hosts, ["--help"])
    assert result.exit_code == 0
    for name in ("list", "release", "rotate", "enable-sharing"):
        assert name in result.output


def test_rotate_help_documents_arguments() -> None:
    result = CliRunner().invoke(hosts, ["rotate", "--help"])
    assert result.exit_code == 0
    assert "HOST_REF" in result.output
    assert "--account" in result.output


def test_find_lease_by_ref_matches_host_id_db_id_and_name() -> None:
    lease = _make_lease(_HOST_ID, "my-workspace")
    leases = [lease]
    assert _find_lease_by_ref(leases, _HOST_ID) is lease
    assert _find_lease_by_ref(leases, "11111111-2222-3333-4444-555555555555") is lease
    assert _find_lease_by_ref(leases, "my-workspace") is lease
    assert _find_lease_by_ref(leases, "host-" + "f" * 32) is None


def test_find_host_state_dirs_scans_provider_instances(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Lay out an mngr host_dir with one profile and three imbue_cloud provider
    # instances, only one of which holds this host's complete client keypair
    # (a private key without its .pub sibling is unusable for rotation). The
    # sessions dir must never be scanned as if it were a provider instance.
    host_dir = tmp_path / "mngr-home"
    profile_dir = host_dir / "profiles" / "prof-1"
    (host_dir).mkdir(parents=True)
    (host_dir / "config.toml").write_text('profile = "prof-1"\n')
    state_root = profile_dir / "providers" / "imbue_cloud"
    (state_root / "sessions").mkdir(parents=True)
    with_key = state_root / "imbue_cloud_alice" / "hosts" / _HOST_ID
    with_key.mkdir(parents=True)
    (with_key / "ssh_key").write_text("fake-private-key")
    (with_key / "ssh_key.pub").write_text("ssh-ed25519 AAAAFAKE fake-public-key")
    without_key = state_root / "imbue_cloud_bob" / "hosts" / _HOST_ID
    without_key.mkdir(parents=True)
    with_private_key_only = state_root / "imbue_cloud_carol" / "hosts" / _HOST_ID
    with_private_key_only.mkdir(parents=True)
    (with_private_key_only / "ssh_key").write_text("fake-private-key")
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))

    found = _find_host_state_dirs(_HOST_ID)

    assert found == [with_key]
