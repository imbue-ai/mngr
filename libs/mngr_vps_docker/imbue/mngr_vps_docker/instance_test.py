"""Tests for VPS Docker provider instance utilities."""

from pathlib import Path

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_vps_docker.instance import _parse_build_args
from imbue.mngr_vps_docker.instance import _remove_host_from_known_hosts

_DEFAULT_REGION = "ewr"
_DEFAULT_PLAN = "vc2-1c-1gb"
_DEFAULT_OS_ID = 2136


def _parse_with_defaults(build_args: list[str] | None) -> tuple[str, str, int, tuple[str, ...]]:
    return _parse_build_args(
        build_args,
        default_region=_DEFAULT_REGION,
        default_plan=_DEFAULT_PLAN,
        default_os_id=_DEFAULT_OS_ID,
    )


def test_parse_build_args_defaults_when_none() -> None:
    region, plan, os_id, docker_args = _parse_with_defaults(None)
    assert region == "ewr"
    assert plan == "vc2-1c-1gb"
    assert os_id == 2136
    assert docker_args == ()


def test_parse_build_args_defaults_when_empty() -> None:
    region, plan, os_id, docker_args = _parse_with_defaults([])
    assert region == "ewr"
    assert plan == "vc2-1c-1gb"
    assert os_id == 2136
    assert docker_args == ()


def test_parse_build_args_vps_region() -> None:
    region, plan, os_id, docker_args = _parse_with_defaults(["--vps-region=lax"])
    assert region == "lax"
    assert plan == "vc2-1c-1gb"
    assert os_id == 2136
    assert docker_args == ()


def test_parse_build_args_vps_plan() -> None:
    _region, plan, _os_id, _docker_args = _parse_with_defaults(["--vps-plan=vc2-2c-4gb"])
    assert plan == "vc2-2c-4gb"


def test_parse_build_args_vps_os() -> None:
    _region, _plan, os_id, _docker_args = _parse_with_defaults(["--vps-os=9999"])
    assert os_id == 9999


def test_parse_build_args_docker_args_passthrough() -> None:
    region, _plan, _os_id, docker_args = _parse_with_defaults(["--file=Dockerfile", "."])
    assert region == "ewr"
    assert docker_args == ("--file=Dockerfile", ".")


def test_parse_build_args_mixed_vps_and_docker() -> None:
    region, plan, os_id, docker_args = _parse_with_defaults(
        ["--vps-plan=vc2-2c-4gb", "--file=Dockerfile", "--vps-region=lax", "."],
    )
    assert region == "lax"
    assert plan == "vc2-2c-4gb"
    assert os_id == 2136
    assert docker_args == ("--file=Dockerfile", ".")


def test_parse_build_args_all_vps_overrides() -> None:
    region, plan, os_id, docker_args = _parse_with_defaults(
        ["--vps-region=sjc", "--vps-plan=vc2-4c-8gb", "--vps-os=1234"],
    )
    assert region == "sjc"
    assert plan == "vc2-4c-8gb"
    assert os_id == 1234
    assert docker_args == ()


def test_parse_build_args_rejects_unknown_vps_arg() -> None:
    with pytest.raises(MngrError, match="Unknown VPS build arg.*--vps-regiom"):
        _parse_with_defaults(["--vps-regiom=ewr"])


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
