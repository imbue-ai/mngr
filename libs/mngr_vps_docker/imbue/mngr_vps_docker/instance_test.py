"""Tests for VPS Docker provider instance utilities."""

from pathlib import Path

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr_vps_docker.container_setup import emit_docker_build_output
from imbue.mngr_vps_docker.container_setup import is_retryable_rsync_error
from imbue.mngr_vps_docker.container_setup import redact_secret_env
from imbue.mngr_vps_docker.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps_docker.container_setup import resolve_dockerfile_paths
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import _parse_build_args
from imbue.mngr_vps_docker.instance import build_vps_tags

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
    remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    result = known_hosts.read_text()
    assert "192.168.1.100" not in result
    assert "192.168.1.101" in result


def test_remove_host_from_known_hosts_nonstandard_port(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[192.168.1.100]:2222 ssh-ed25519 AAAA key1\n192.168.1.100 ssh-ed25519 BBBB key2\n")
    remove_host_from_known_hosts(known_hosts, "192.168.1.100", 2222)
    result = known_hosts.read_text()
    assert "[192.168.1.100]:2222" not in result
    # The port-22 entry should remain
    assert "192.168.1.100 ssh-ed25519 BBBB key2" in result


def test_remove_host_from_known_hosts_file_not_exists(tmp_path: Path) -> None:
    known_hosts = tmp_path / "nonexistent"
    # Should not raise
    remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)


def test_remove_host_from_known_hosts_no_match(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    original = "192.168.1.101 ssh-ed25519 AAAA key1\n"
    known_hosts.write_text(original)
    remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    assert known_hosts.read_text() == original


def test_remove_host_from_known_hosts_empty_file(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    remove_host_from_known_hosts(known_hosts, "192.168.1.100", 22)
    assert known_hosts.read_text() == ""


# -- resolve_dockerfile_paths tests --


def test_resolve_dockerfile_paths_rewrites_file_equals() -> None:
    result = resolve_dockerfile_paths(["--file=Dockerfile"], "/tmp/build")
    assert result == ("--file=/tmp/build/Dockerfile",)


def test_resolve_dockerfile_paths_rewrites_f_equals() -> None:
    result = resolve_dockerfile_paths(["-f=Dockerfile"], "/tmp/build")
    assert result == ("-f=/tmp/build/Dockerfile",)


def test_resolve_dockerfile_paths_rewrites_f_separate_arg() -> None:
    result = resolve_dockerfile_paths(["-f", "Dockerfile"], "/tmp/build")
    assert result == ("-f", "/tmp/build/Dockerfile")


def test_resolve_dockerfile_paths_rewrites_file_separate_arg() -> None:
    result = resolve_dockerfile_paths(["--file", "my.Dockerfile"], "/tmp/build")
    assert result == ("--file", "/tmp/build/my.Dockerfile")


def test_resolve_dockerfile_paths_preserves_absolute_path() -> None:
    result = resolve_dockerfile_paths(["--file=/abs/Dockerfile"], "/tmp/build")
    assert result == ("--file=/abs/Dockerfile",)


def test_resolve_dockerfile_paths_preserves_absolute_separate_arg() -> None:
    result = resolve_dockerfile_paths(["-f", "/abs/Dockerfile"], "/tmp/build")
    assert result == ("-f", "/abs/Dockerfile")


def test_resolve_dockerfile_paths_preserves_other_args() -> None:
    result = resolve_dockerfile_paths(
        ["--build-arg=FOO=bar", "--file=Dockerfile", "--no-cache"],
        "/tmp/build",
    )
    assert result == ("--build-arg=FOO=bar", "--file=/tmp/build/Dockerfile", "--no-cache")


def test_resolve_dockerfile_paths_empty_args() -> None:
    result = resolve_dockerfile_paths([], "/tmp/build")
    assert result == ()


_HOST_ID = HostId.generate()


def test_build_vps_tags_emits_baseline_when_extras_empty() -> None:
    """No MNGR_VPS_EXTRA_TAGS -> just the two always-on tags."""
    assert build_vps_tags(_HOST_ID, "vultr", "") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=vultr",
    ]


def test_build_vps_tags_appends_single_extra() -> None:
    """One ``key=value`` extra tag is appended verbatim."""
    assert build_vps_tags(_HOST_ID, "vultr", "minds_env=dev-josh") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=vultr",
        "minds_env=dev-josh",
    ]


def test_build_vps_tags_appends_multiple_comma_separated_extras() -> None:
    """Comma-separated extras are split + appended in order."""
    assert build_vps_tags(_HOST_ID, "vultr", "a=1,b=2,c=3") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=vultr",
        "a=1",
        "b=2",
        "c=3",
    ]


def test_build_vps_tags_strips_whitespace_around_extras() -> None:
    """Whitespace around each comma-separated entry is trimmed."""
    assert build_vps_tags(_HOST_ID, "vultr", " a=1 , b=2 ") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=vultr",
        "a=1",
        "b=2",
    ]


def test_build_vps_tags_skips_blank_entries_from_trailing_commas() -> None:
    """Trailing / doubled commas don't emit empty tags."""
    assert build_vps_tags(_HOST_ID, "vultr", "a=1,,b=2,") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=vultr",
        "a=1",
        "b=2",
    ]


def test_build_vps_tags_uses_provided_provider_name() -> None:
    """The provider name is interpolated, not hard-coded."""
    assert build_vps_tags(_HOST_ID, "ovh", "") == [
        f"mngr-host-id={_HOST_ID}",
        "mngr-provider=ovh",
    ]


def test_redact_secret_env_replaces_known_var_value() -> None:
    """Known secret env-var assignments have their value redacted."""
    cmd = "DEPOT_TOKEN=tok-12345 docker build ."
    assert redact_secret_env(cmd) == "DEPOT_TOKEN=<redacted> docker build ."


def test_redact_secret_env_replaces_single_quoted_value() -> None:
    """Single-quoted secret values are also redacted."""
    cmd = "DEPOT_TOKEN='tok with spaces' docker build ."
    assert redact_secret_env(cmd) == "DEPOT_TOKEN=<redacted> docker build ."


def test_redact_secret_env_leaves_unknown_vars_alone() -> None:
    """Non-secret env-var assignments are untouched."""
    cmd = "FOO=bar docker build ."
    assert redact_secret_env(cmd) == "FOO=bar docker build ."


def test_redact_secret_env_no_op_when_no_match() -> None:
    """A command with no env-var assignments comes back unchanged."""
    cmd = "docker build ."
    assert redact_secret_env(cmd) == "docker build ."


def test_is_retryable_rsync_error_true_on_known_pattern() -> None:
    """Connection-class rsync stderr strings are flagged retryable."""
    # Pick from the documented retry patterns; any one of them suffices.
    assert is_retryable_rsync_error("rsync: Connection reset by peer")


def test_is_retryable_rsync_error_false_on_unknown_pattern() -> None:
    """Unrelated rsync stderr should not be retried."""
    assert not is_retryable_rsync_error("rsync: permission denied")


def test_is_retryable_rsync_error_false_on_empty_string() -> None:
    """No stderr -> nothing to match -> not retryable."""
    assert not is_retryable_rsync_error("")


def test_emit_docker_build_output_handles_nonempty_line() -> None:
    """Non-empty lines are logged; the call should not raise."""
    emit_docker_build_output("Step 1/5 : FROM debian:bookworm-slim")


def test_emit_docker_build_output_skips_whitespace_only() -> None:
    """Whitespace-only / empty lines are silently dropped."""
    emit_docker_build_output("")
    emit_docker_build_output("   ")
    emit_docker_build_output("\n")
