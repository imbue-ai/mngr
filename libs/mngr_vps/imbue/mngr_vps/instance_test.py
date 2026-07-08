"""Tests for VPS provider instance utilities."""

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SnapshotNotFoundError
from imbue.mngr.errors import SnapshotsNotSupportedError
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.container_setup import emit_docker_build_output
from imbue.mngr_vps.container_setup import is_retryable_rsync_error
from imbue.mngr_vps.container_setup import redact_secret_env
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.container_setup import resolve_dockerfile_paths
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.errors import BareIsolationNotSupportedError
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import MinimalVpsProvider
from imbue.mngr_vps.instance import _wait_for_cloud_init_marker
from imbue.mngr_vps.instance import build_vps_tags
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import isolation_from_marker
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient

# =============================================================================
# MinimalVpsProvider._parse_build_args (no-provisioning, no-prefix shape)
# =============================================================================


def test_minimal_vps_provider_parse_build_args_empty() -> None:
    """No build args -> no git_depth, no docker args; region/plan defaults are unused sentinels."""
    minimal = MinimalVpsProvider.model_construct()
    parsed = minimal._parse_build_args(None)
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ()


def test_minimal_vps_provider_parse_build_args_extracts_git_depth() -> None:
    """--git-depth=N is consumed and surfaced separately; not forwarded to docker."""
    minimal = MinimalVpsProvider.model_construct()
    parsed = minimal._parse_build_args(["--git-depth=1", "--file=Dockerfile", "."])
    assert parsed.git_depth == 1
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_minimal_vps_provider_parse_build_args_passes_through_unknown() -> None:
    """Docker flags and positional args pass through verbatim."""
    minimal = MinimalVpsProvider.model_construct()
    parsed = minimal._parse_build_args(["--build-arg=FOO=bar", "--no-cache", "."])
    assert parsed.git_depth is None
    assert parsed.docker_build_args == ("--build-arg=FOO=bar", "--no-cache", ".")


def test_minimal_vps_provider_parse_build_args_rejects_dropped_vps_prefix() -> None:
    """A caller still using --vps-* gets a clear migration error rather than silently forwarding to docker."""
    minimal = MinimalVpsProvider.model_construct()
    with pytest.raises(MngrError, match="no longer supported"):
        minimal._parse_build_args(["--vps-region=ewr"])


class _ParsedSubBuildOptions(ParsedVpsBuildOptions):
    """A provider-specific ParsedVpsBuildOptions subclass for _require_parsed tests."""


def test_require_parsed_returns_narrowed_instance() -> None:
    """_require_parsed returns the same object, typed as the expected subclass, on a match."""
    minimal = MinimalVpsProvider.model_construct()
    parsed = _ParsedSubBuildOptions(region="r", plan="p", docker_build_args=())
    assert minimal._require_parsed(parsed, _ParsedSubBuildOptions) is parsed


def test_require_parsed_raises_uniformly_on_mismatch() -> None:
    """_require_parsed raises a clear MngrError when the parsed shape is not the expected subclass."""
    minimal = MinimalVpsProvider.model_construct()
    parsed = ParsedVpsBuildOptions(region="r", plan="p", docker_build_args=())
    with pytest.raises(MngrError, match="expected _ParsedSubBuildOptions, got ParsedVpsBuildOptions"):
        minimal._require_parsed(parsed, _ParsedSubBuildOptions)


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
    """No MNGR_VPS_EXTRA_TAGS -> just the always-on identity + placement tags."""
    assert build_vps_tags(_HOST_ID, "vultr", "", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "vultr",
        ISOLATION_TAG_KEY: "container",
    }


def test_build_vps_tags_stamps_bare_isolation_marker() -> None:
    """A bare placement is stamped ``mngr-isolation=none`` so discovery can pick the bare realizer."""
    assert build_vps_tags(_HOST_ID, "aws", "", IsolationMode.NONE) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "aws",
        ISOLATION_TAG_KEY: "none",
    }


def test_build_vps_tags_appends_single_extra() -> None:
    """One ``key=value`` extra tag is merged in."""
    assert build_vps_tags(_HOST_ID, "vultr", "minds_env=dev-josh", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "vultr",
        ISOLATION_TAG_KEY: "container",
        "minds_env": "dev-josh",
    }


def test_build_vps_tags_appends_multiple_comma_separated_extras() -> None:
    """Comma-separated extras are split + merged in."""
    assert build_vps_tags(_HOST_ID, "vultr", "a=1,b=2,c=3", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "vultr",
        ISOLATION_TAG_KEY: "container",
        "a": "1",
        "b": "2",
        "c": "3",
    }


def test_build_vps_tags_strips_whitespace_around_extras() -> None:
    """Whitespace around each comma-separated entry is trimmed."""
    assert build_vps_tags(_HOST_ID, "vultr", " a=1 , b=2 ", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "vultr",
        ISOLATION_TAG_KEY: "container",
        "a": "1",
        "b": "2",
    }


def test_build_vps_tags_skips_blank_entries_from_trailing_commas() -> None:
    """Trailing / doubled commas don't emit empty tags."""
    assert build_vps_tags(_HOST_ID, "vultr", "a=1,,b=2,", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "vultr",
        ISOLATION_TAG_KEY: "container",
        "a": "1",
        "b": "2",
    }


def test_build_vps_tags_uses_provided_provider_name() -> None:
    """The provider name is interpolated, not hard-coded."""
    assert build_vps_tags(_HOST_ID, "ovh", "", IsolationMode.CONTAINER) == {
        "mngr-host-id": str(_HOST_ID),
        "mngr-provider": "ovh",
        ISOLATION_TAG_KEY: "container",
    }


def test_build_vps_tags_rejects_entry_without_equals() -> None:
    """Extras missing an ``=`` separator are an error, not silently dropped."""
    with pytest.raises(MngrError, match="Invalid VPS extra tag"):
        build_vps_tags(_HOST_ID, "vultr", "bare-tag", IsolationMode.CONTAINER)


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


# One representative stderr string per entry in _RETRYABLE_RSYNC_PATTERNS, so every
# retryable branch is exercised (a dropped pattern flips exactly one case to failing).
@pytest.mark.parametrize(
    "stderr",
    [
        "rsync: write error: Broken pipe (32)",
        "rsync: Connection reset by peer",
        "ssh: connect to host 1.2.3.4 port 22: Connection refused",
        "rsync: [sender] failed: Connection timed out",
        "client_loop: send disconnect: Broken pipe",
        "ssh: connect to host 1.2.3.4 port 22: No route",
        "kex_exchange_identification: read: Connection reset by peer",
        "connect to address 1.2.3.4: Network is unreachable",
    ],
)
def test_is_retryable_rsync_error_true_for_each_connection_class_pattern(stderr: str) -> None:
    """Every connection-class pattern in _RETRYABLE_RSYNC_PATTERNS must be flagged retryable."""
    assert is_retryable_rsync_error(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "rsync: permission denied (13)",
        "unexpected EOF in tar header",
        "",
    ],
)
def test_is_retryable_rsync_error_false_for_non_connection_errors(stderr: str) -> None:
    """Application-level rsync failures (and empty stderr) must NOT be retried."""
    assert not is_retryable_rsync_error(stderr)


def test_emit_docker_build_output_logs_stripped_nonempty_line_at_build_level() -> None:
    """A non-empty line is logged exactly once at BUILD level, with surrounding whitespace stripped."""
    with capture_loguru(level="BUILD") as log_output:
        emit_docker_build_output("  Step 1/5 : FROM debian:bookworm-slim  ")
    # The BUILD sink uses a "{message}" format, so the captured text is just the
    # rendered (stripped) line followed by loguru's trailing newline.
    assert log_output.getvalue() == "Step 1/5 : FROM debian:bookworm-slim\n"


def test_emit_docker_build_output_drops_whitespace_only_lines() -> None:
    """Whitespace-only / empty lines must produce no log output at all."""
    with capture_loguru(level="BUILD") as log_output:
        emit_docker_build_output("")
        emit_docker_build_output("   ")
        emit_docker_build_output("\n")
    assert log_output.getvalue() == ""


# =============================================================================
# _wait_for_cloud_init_marker
# =============================================================================


class _ScriptedOuter(MutableModel):
    """Outer host whose ``execute_idempotent_command`` is driven by a callable.

    ``responder`` is invoked on each call with the issued command and returns
    either a ``CommandResult`` (used as the return value) or raises an
    exception (propagated to the caller). This lets one test mix exceptions
    and successful responses in any order.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    responder: Callable[[str], CommandResult] = Field(description="Per-call responder")
    call_count: int = Field(default=0, description="Number of execute_idempotent_command calls observed")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Any = None,
        env: Any = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.call_count += 1
        return self.responder(command)


def _scripted_outer(responder: Callable[[str], CommandResult]) -> tuple[OuterHostInterface, _ScriptedOuter]:
    """Build a ``_ScriptedOuter`` typed as ``OuterHostInterface``.

    Returns the stub typed as the interface (for the helper under test) plus
    the concrete stub (for the test to introspect call_count).
    """
    stub = _ScriptedOuter(responder=responder)
    return cast(OuterHostInterface, stub), stub


def test_wait_for_cloud_init_marker_returns_when_marker_appears() -> None:
    """Returns immediately on the first poll where /var/run/mngr-ready exists."""

    def responder(_command: str) -> CommandResult:
        return CommandResult(stdout="", stderr="", success=True)

    outer, stub = _scripted_outer(responder)
    _wait_for_cloud_init_marker(outer, timeout_seconds=10.0, poll_interval_seconds=0.0)
    assert stub.call_count == 1


def test_wait_for_cloud_init_marker_keeps_polling_while_marker_missing() -> None:
    """Keeps polling as long as the marker is missing; returns once it appears."""
    poll_count = 0

    def responder(_command: str) -> CommandResult:
        nonlocal poll_count
        poll_count += 1
        success = poll_count >= 3
        return CommandResult(stdout="", stderr="", success=success)

    outer, stub = _scripted_outer(responder)
    _wait_for_cloud_init_marker(outer, timeout_seconds=10.0, poll_interval_seconds=0.0)
    assert stub.call_count == 3


def test_wait_for_cloud_init_marker_swallows_transient_connection_errors() -> None:
    """Per-poll ``HostConnectionError`` must NOT abort the wait loop.

    Regression test for the bootstrap reliability fix: while the bootstrap
    restarts sshd to apply the MaxSessions/MaxStartups tuning, an in-flight
    ``execute_idempotent_command`` can surface as ``HostConnectionError``. The
    poll treats it as "not ready yet" and retries until the marker appears or
    ``timeout_seconds`` is exhausted.
    """
    poll_count = 0

    def responder(_command: str) -> CommandResult:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            raise HostConnectionError("connection reset during sshd reload")
        if poll_count == 2:
            return CommandResult(stdout="", stderr="", success=False)
        return CommandResult(stdout="", stderr="", success=True)

    outer, stub = _scripted_outer(responder)
    _wait_for_cloud_init_marker(outer, timeout_seconds=10.0, poll_interval_seconds=0.0)
    assert stub.call_count == 3


def test_wait_for_cloud_init_marker_raises_mngr_error_on_timeout() -> None:
    """When the marker never appears within the timeout, raise ``MngrError``."""

    def responder(_command: str) -> CommandResult:
        return CommandResult(stdout="", stderr="", success=False)

    outer, stub = _scripted_outer(responder)
    with pytest.raises(MngrError, match="Cloud-init did not complete"):
        _wait_for_cloud_init_marker(outer, timeout_seconds=0.0, poll_interval_seconds=0.0)
    assert stub.call_count >= 1


def test_wait_for_cloud_init_marker_raises_on_persistent_connection_error() -> None:
    """If every poll raises ``HostConnectionError`` until the timeout, raise ``MngrError``.

    The connection errors are absorbed per-poll, so the failure mode after
    exhausting the timeout budget is the normal "did not complete" error,
    not a leaked ``HostConnectionError``.
    """

    def responder(_command: str) -> CommandResult:
        raise HostConnectionError("sshd never recovered")

    outer, stub = _scripted_outer(responder)
    with pytest.raises(MngrError, match="Cloud-init did not complete"):
        _wait_for_cloud_init_marker(outer, timeout_seconds=0.05, poll_interval_seconds=0.01)
    assert stub.call_count >= 2


# =========================================================================
# create_host runs pre-create validation before any provider write
# =========================================================================


class _ValidateRaisesProvider(MinimalVpsProvider):
    """MinimalVpsProvider whose pre-create validation always raises.

    Used to assert that ``create_host`` calls ``_validate_provider_args_for_create``
    before the first provider write (the SSH key upload), so a failed precondition
    aborts cleanly with no leaked resources.
    """

    def _validate_provider_args_for_create(self) -> None:
        raise MngrError("SENTINEL: pre-create validation ran")


def test_create_host_runs_pre_create_validation_before_any_provider_write(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A failing ``_validate_provider_args_for_create`` aborts ``create_host`` before upload_ssh_key.

    This guards the onboarding UX motivating the GCP firewall pre-flight: a
    provider precondition (e.g. a missing ``mngr gcp prepare`` firewall rule)
    must fail before any SSH key upload or instance creation, not mid-create
    under a "Host creation failed, attempting cleanup..." path.
    ``ExternallyManagedVpsClient`` raises on ``upload_ssh_key``; if validation
    did NOT run first, we would see that error (a different message) instead of
    the sentinel.
    """
    provider = _ValidateRaisesProvider(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps-docker")),
        vps_client=ExternallyManagedVpsClient(),
    )
    with pytest.raises(MngrError, match="SENTINEL: pre-create validation ran"):
        provider.create_host(HostName("test-host"))


# =========================================================================
# Realizer selection (isolation axis)
# =========================================================================


def _minimal_provider(temp_mngr_ctx: MngrContext, isolation: IsolationMode) -> MinimalVpsProvider:
    return MinimalVpsProvider(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps-docker"), isolation=isolation),
        vps_client=ExternallyManagedVpsClient(),
    )


def test_default_isolation_is_container() -> None:
    """The provider config defaults to container isolation, preserving the original behavior."""
    assert VpsProviderConfig(backend=ProviderBackendName("test-vps-docker")).isolation is IsolationMode.CONTAINER


def test_provider_builds_docker_realizer_for_container_isolation(temp_mngr_ctx: MngrContext) -> None:
    """``isolation=CONTAINER`` yields a DockerRealizer, and snapshot support reflects it."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    assert isinstance(provider._realizer, DockerRealizer)
    assert provider.supports_snapshots is True


def test_provider_builds_bare_realizer_for_none_isolation(temp_mngr_ctx: MngrContext) -> None:
    """``isolation=NONE`` yields a BareRealizer, which reports no snapshot support."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.NONE)
    assert isinstance(provider._realizer, BareRealizer)
    assert provider.supports_snapshots is False


def _record_for(host_id: HostId, *, container_name: str | None) -> VpsHostRecord:
    """Build a VpsHostRecord whose placement is bare (no container) or container.

    A bare host's ``config.container_name`` is None; a container host names its
    container. The created/updated timestamps are backfilled by CertifiedHostData.
    """
    now = datetime.now(timezone.utc)
    return VpsHostRecord(
        certified_host_data=CertifiedHostData(host_id=str(host_id), host_name="h", created_at=now, updated_at=now),
        vps_ip="10.0.0.1",
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId("i-1"),
            region="r",
            plan="p",
            container_name=container_name,
        ),
    )


def test_realizer_for_record_picks_bare_for_a_bare_record_under_default_container_config(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A bare host (record ``container_name is None``) resolves to the BARE realizer even
    when the provider config defaults to CONTAINER.

    This is the core fix: operations on an existing host use the realizer matching
    THAT host's recorded placement, not the config knob -- so a bare host reached
    by a default-container provider hits the VM's own port-22 sshd (no container
    probe), rather than being invisible/unreachable. Asserting on the realizer type
    and its agent endpoint port captures the exact behavior that was wrong.
    """
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    # The create-time default realizer is unchanged (still the container one).
    assert isinstance(provider._realizer, DockerRealizer)
    bare_record = _record_for(HostId.generate(), container_name=None)
    realizer = provider._realizer_for_record(bare_record)
    assert isinstance(realizer, BareRealizer)
    # The bare agent endpoint is the VM's own port 22 -- not the container port 2222.
    assert realizer.agent_endpoint("10.0.0.1").port == 22


def test_realizer_for_record_picks_container_for_a_container_record(temp_mngr_ctx: MngrContext) -> None:
    """A container host (record names a container) resolves to the DockerRealizer."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    container_record = _record_for(HostId.generate(), container_name="mngr-agent-abc")
    realizer = provider._realizer_for_record(container_record)
    assert isinstance(realizer, DockerRealizer)
    assert realizer.agent_endpoint("10.0.0.1").port == provider.config.container_ssh_port


def test_isolation_from_marker_defaults_absent_to_container() -> None:
    """An untagged (pre-marker) host defaults to CONTAINER; explicit values parse exactly.

    Backward-compat guard: hosts created before the ``mngr-isolation`` marker existed
    were all container placements, so an absent marker (``None``) must default to
    CONTAINER. A present value is parsed strictly -- an unrecognized marker raises
    rather than being silently mis-resolved.
    """
    assert isolation_from_marker(None) is IsolationMode.CONTAINER
    assert isolation_from_marker("none") is IsolationMode.NONE
    assert isolation_from_marker("container") is IsolationMode.CONTAINER
    with pytest.raises(ValueError):
        isolation_from_marker("garbage")


def test_bare_provider_rejects_snapshot_operations_up_front(temp_mngr_ctx: MngrContext) -> None:
    """A bare provider raises SnapshotsNotSupportedError before touching any host record."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.NONE)
    with pytest.raises(SnapshotsNotSupportedError):
        provider.create_snapshot(HostId.generate())
    with pytest.raises(SnapshotsNotSupportedError):
        provider.delete_snapshot(HostId.generate(), SnapshotId("snap-x"))


def _snapshot(id_: str, name: str) -> SnapshotRecord:
    return SnapshotRecord(id=id_, name=name, created_at=datetime.now(timezone.utc).isoformat())


def _record_with_snapshots(host_id: HostId, snapshots: list[SnapshotRecord]) -> VpsHostRecord:
    record = _record_for(host_id, container_name="c")
    certified = record.certified_host_data
    return record.with_certified_updates(to_update(certified.field_ref().snapshots, snapshots))


def test_delete_snapshot_raises_for_unknown_snapshot_id(temp_mngr_ctx: MngrContext) -> None:
    """An id not in the host record is a SnapshotNotFoundError (raised before any outer I/O)."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    host_id = HostId.generate()
    provider._host_record_cache[host_id] = _record_with_snapshots(host_id, [_snapshot("sha256:a", "keep")])
    with pytest.raises(SnapshotNotFoundError):
        provider.delete_snapshot(host_id, SnapshotId("sha256:missing"))


class _FakeSnapshotRealizer:
    """Duck-typed snapshot-capable realizer: the rmi is a no-op and the host store is
    never really opened, so ``delete_snapshot``'s record handling can be tested without
    real outer I/O."""

    def delete_snapshot_placement(self, outer: OuterHostInterface, snapshot_id: SnapshotId) -> None:
        pass

    def open_host_store(self, outer: OuterHostInterface, host_id: HostId) -> Any:
        return object()


class _CaptureDeleteProvider(MinimalVpsProvider):
    """delete_snapshot double: a fake realizer (seeded into the cache) skips real docker,
    and the persisted write is captured for assertion."""

    _captured_record: VpsHostRecord | None = PrivateAttr(default=None)

    def _require_snapshot_capable_realizer(self, realizer: HostRealizer) -> SnapshotCapableRealizer:
        return cast(SnapshotCapableRealizer, realizer)

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str) -> Iterator[OuterHostInterface]:
        yield cast(OuterHostInterface, object())

    def _write_and_mirror(self, host_store: Any, record: VpsHostRecord) -> None:
        self._captured_record = record


def test_delete_snapshot_drops_it_from_the_record_and_list(temp_mngr_ctx: MngrContext) -> None:
    """A successful delete removes the snapshot from the persisted record and from the
    cache-backed ``list_snapshots``, leaving the other snapshots intact."""
    provider = _CaptureDeleteProvider(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps-docker"), isolation=IsolationMode.CONTAINER),
        vps_client=ExternallyManagedVpsClient(),
    )
    provider._realizer_cache = {IsolationMode.CONTAINER: cast(HostRealizer, _FakeSnapshotRealizer())}
    host_id = HostId.generate()
    keep, doomed = _snapshot("sha256:keep", "keep"), _snapshot("sha256:doomed", "doomed")
    provider._host_record_cache[host_id] = _record_with_snapshots(host_id, [keep, doomed])

    provider.delete_snapshot(host_id, SnapshotId("sha256:doomed"))

    assert provider._captured_record is not None
    assert [s.id for s in provider._captured_record.certified_host_data.snapshots] == ["sha256:keep"]
    assert [str(s.id) for s in provider.list_snapshots(host_id)] == ["sha256:keep"]


def test_create_host_rejects_bare_on_a_provider_without_machine_lifecycle(temp_mngr_ctx: MngrContext) -> None:
    """A provider with no stop/start substrate refuses ``isolation=NONE`` before provisioning.

    ``MinimalVpsProvider`` does not override ``_supports_bare_isolation`` (default
    False), so a bare create must fail fast with the dedicated error rather than
    strand a VM the substrate can't restart.
    """
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.NONE)
    with pytest.raises(BareIsolationNotSupportedError, match="does not support isolation=NONE"):
        provider.create_host(HostName("test-host"))


class _BareCapableMinimalProvider(MinimalVpsProvider):
    """MinimalVpsProvider that claims bare support, to exercise the bare create-input guard.

    The base gate would otherwise reject ``isolation=NONE`` before the
    container-only-input check (Minimal has no machine lifecycle), so this stub
    flips the predicate to reach the guard under test.
    """

    @property
    def _supports_bare_isolation(self) -> bool:
        return True


def test_create_host_rejects_container_only_inputs_for_bare(temp_mngr_ctx: MngrContext) -> None:
    """A bare create rejects an image build / start-args rather than silently ignoring them."""
    provider = _BareCapableMinimalProvider(
        name=ProviderInstanceName("test-vps"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps"), isolation=IsolationMode.NONE),
        vps_client=ExternallyManagedVpsClient(),
    )
    with pytest.raises(MngrError, match="does not support.*Docker build args"):
        provider.create_host(HostName("test-host"), build_args=["--file=Dockerfile", "."])
    with pytest.raises(MngrError, match="does not support.*start args"):
        provider.create_host(HostName("test-host"), start_args=["--cpus=2"])
