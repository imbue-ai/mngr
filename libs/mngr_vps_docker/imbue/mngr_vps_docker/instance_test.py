"""Tests for VPS Docker provider instance utilities."""

from pathlib import Path

from imbue.mngr_vps_docker.instance import _remove_host_from_known_hosts


def test_remove_host_from_known_hosts_port_22(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "192.168.1.100 ssh-ed25519 AAAA key1\n"
        "192.168.1.101 ssh-ed25519 BBBB key2\n"
    )
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    result = known_hosts.read_text()
    assert "192.168.1.100" not in result
    assert "192.168.1.101" in result


def test_remove_host_from_known_hosts_nonstandard_port(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "[192.168.1.100]:2222 ssh-ed25519 AAAA key1\n"
        "192.168.1.100 ssh-ed25519 BBBB key2\n"
    )
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 2222)
    result = known_hosts.read_text()
    assert "[192.168.1.100]:2222" not in result
    # The port-22 entry should remain
    assert "192.168.1.100 ssh-ed25519 BBBB key2" in result


def test_remove_host_from_known_hosts_file_not_exists(tmp_path: Path) -> None:
    known_hosts = tmp_path / "nonexistent"
    # Should not raise
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)


def test_remove_host_from_known_hosts_no_match(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    original = "192.168.1.101 ssh-ed25519 AAAA key1\n"
    known_hosts.write_text(original)
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    assert known_hosts.read_text() == original


def test_remove_host_from_known_hosts_empty_file(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    assert known_hosts.read_text() == ""
