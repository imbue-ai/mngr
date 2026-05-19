"""Tests for VPS Docker provider instance utilities."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import ErrorInfo
from imbue.mngr.interfaces.data_types import ProviderErrorInfo
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.instance import _parse_build_args
from imbue.mngr_vps_docker.instance import _remove_host_from_known_hosts
from imbue.mngr_vps_docker.instance import _resolve_dockerfile_paths

_DEFAULT_REGION = "ewr"
_DEFAULT_PLAN = "vc2-1c-1gb"
_DEFAULT_OS_ID = 2136


def _parse_with_defaults(build_args: list[str] | None) -> ParsedVpsBuildOptions:
    return _parse_build_args(
        build_args,
        default_region=_DEFAULT_REGION,
        default_plan=_DEFAULT_PLAN,
        default_os_id=_DEFAULT_OS_ID,
    )


def test_parse_build_args_defaults_when_none() -> None:
    parsed = _parse_with_defaults(None)
    assert parsed.region == "ewr"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.os_id == 2136
    assert parsed.docker_build_args == ()
    assert parsed.git_depth is None


def test_parse_build_args_defaults_when_empty() -> None:
    parsed = _parse_with_defaults([])
    assert parsed.region == "ewr"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.os_id == 2136
    assert parsed.docker_build_args == ()


def test_parse_build_args_vps_region() -> None:
    parsed = _parse_with_defaults(["--vps-region=lax"])
    assert parsed.region == "lax"
    assert parsed.plan == "vc2-1c-1gb"
    assert parsed.os_id == 2136
    assert parsed.docker_build_args == ()


def test_parse_build_args_vps_plan() -> None:
    parsed = _parse_with_defaults(["--vps-plan=vc2-2c-4gb"])
    assert parsed.plan == "vc2-2c-4gb"


def test_parse_build_args_vps_os() -> None:
    parsed = _parse_with_defaults(["--vps-os=9999"])
    assert parsed.os_id == 9999


def test_parse_build_args_docker_args_passthrough() -> None:
    parsed = _parse_with_defaults(["--file=Dockerfile", "."])
    assert parsed.region == "ewr"
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_mixed_vps_and_docker() -> None:
    parsed = _parse_with_defaults(
        ["--vps-plan=vc2-2c-4gb", "--file=Dockerfile", "--vps-region=lax", "."],
    )
    assert parsed.region == "lax"
    assert parsed.plan == "vc2-2c-4gb"
    assert parsed.os_id == 2136
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_all_vps_overrides() -> None:
    parsed = _parse_with_defaults(
        ["--vps-region=sjc", "--vps-plan=vc2-4c-8gb", "--vps-os=1234"],
    )
    assert parsed.region == "sjc"
    assert parsed.plan == "vc2-4c-8gb"
    assert parsed.os_id == 1234
    assert parsed.docker_build_args == ()


def test_parse_build_args_rejects_unknown_vps_arg() -> None:
    with pytest.raises(MngrError, match="Unknown VPS build arg.*--vps-regiom"):
        _parse_with_defaults(["--vps-regiom=ewr"])


def test_parse_build_args_git_depth() -> None:
    parsed = _parse_with_defaults(["--git-depth=1", "--file=Dockerfile", "."])
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_remove_host_from_known_hosts_port_22(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("192.168.1.100 ssh-ed25519 AAAA key1\n192.168.1.101 ssh-ed25519 BBBB key2\n")
    _remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    result = known_hosts.read_text()
    assert "192.168.1.100" not in result
    assert "192.168.1.101" in result


def test_remove_host_from_known_hosts_nonstandard_port(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[192.168.1.100]:2222 ssh-ed25519 AAAA key1\n192.168.1.100 ssh-ed25519 BBBB key2\n")
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


# -- _resolve_dockerfile_paths tests --


def test_resolve_dockerfile_paths_rewrites_file_equals() -> None:
    result = _resolve_dockerfile_paths(["--file=Dockerfile"], "/tmp/build")
    assert result == ("--file=/tmp/build/Dockerfile",)


def test_resolve_dockerfile_paths_rewrites_f_equals() -> None:
    result = _resolve_dockerfile_paths(["-f=Dockerfile"], "/tmp/build")
    assert result == ("-f=/tmp/build/Dockerfile",)


def test_resolve_dockerfile_paths_rewrites_f_separate_arg() -> None:
    result = _resolve_dockerfile_paths(["-f", "Dockerfile"], "/tmp/build")
    assert result == ("-f", "/tmp/build/Dockerfile")


def test_resolve_dockerfile_paths_rewrites_file_separate_arg() -> None:
    result = _resolve_dockerfile_paths(["--file", "my.Dockerfile"], "/tmp/build")
    assert result == ("--file", "/tmp/build/my.Dockerfile")


def test_resolve_dockerfile_paths_preserves_absolute_path() -> None:
    result = _resolve_dockerfile_paths(["--file=/abs/Dockerfile"], "/tmp/build")
    assert result == ("--file=/abs/Dockerfile",)


def test_resolve_dockerfile_paths_preserves_absolute_separate_arg() -> None:
    result = _resolve_dockerfile_paths(["-f", "/abs/Dockerfile"], "/tmp/build")
    assert result == ("-f", "/abs/Dockerfile")


def test_resolve_dockerfile_paths_preserves_other_args() -> None:
    result = _resolve_dockerfile_paths(
        ["--build-arg=FOO=bar", "--file=Dockerfile", "--no-cache"],
        "/tmp/build",
    )
    assert result == ("--build-arg=FOO=bar", "--file=/tmp/build/Dockerfile", "--no-cache")


def test_resolve_dockerfile_paths_empty_args() -> None:
    result = _resolve_dockerfile_paths([], "/tmp/build")
    assert result == ()


# -- _read_records_from_vps on_error tests --
#
# `_read_records_from_vps` is the canonical conversion site for the
# per-resource on_error feature: when outer SSH to a VPS fails, it must
# surface a ProviderErrorInfo so `mngr list --on-error abort` exits 1.
# These tests exercise the method against a minimal stub instead of
# constructing a full VpsDockerProvider, which would require fabricating
# a VpsClientInterface and full MngrContext.


class _ReadRecordsStub:
    """Minimal stand-in for `self` when calling `_read_records_from_vps` unbound.

    Exposes only the attributes the method reads. `_make_outer_for_vps_ip`
    is the only failure injection point in these tests; the host store
    accessor is never reached because the outer raises first.
    """

    def __init__(self, raise_on_outer: Exception | None = None) -> None:
        self._raise_on_outer = raise_on_outer
        self._host_record_cache: dict[str, Any] = {}
        self.name = ProviderInstanceName("test-vps-provider")

    @contextmanager
    def _make_outer_for_vps_ip(self, _vps_ip: str) -> Iterator[Any]:
        if self._raise_on_outer is not None:
            raise self._raise_on_outer
        yield object()

    def _get_existing_host_store(self, _outer: Any) -> Any:
        raise AssertionError("_get_existing_host_store should not be reached when outer raises")


def test_read_records_from_vps_emits_provider_error_info_on_outer_ssh_failure() -> None:
    """Outer SSH failure surfaces a ProviderErrorInfo through on_error and returns empty results."""
    stub = _ReadRecordsStub(raise_on_outer=HostConnectionError("vps 7 unreachable"))
    captured: list[ErrorInfo] = []

    records, agent_data = VpsDockerProvider._read_records_from_vps(
        cast(VpsDockerProvider, stub), "203.0.113.7", captured.append
    )

    assert records == []
    assert agent_data == {}
    assert len(captured) == 1
    error = captured[0]
    assert isinstance(error, ProviderErrorInfo)
    assert error.provider_name == ProviderInstanceName("test-vps-provider")
    assert "vps 7 unreachable" in error.message
    assert error.exception_type == "HostConnectionError"


def test_read_records_from_vps_with_none_on_error_swallows_failure() -> None:
    """on_error=None still returns empty results; no callback invocation needed."""
    stub = _ReadRecordsStub(raise_on_outer=MngrError("outer ssh blew up"))

    records, agent_data = VpsDockerProvider._read_records_from_vps(cast(VpsDockerProvider, stub), "203.0.113.8", None)

    assert records == []
    assert agent_data == {}
