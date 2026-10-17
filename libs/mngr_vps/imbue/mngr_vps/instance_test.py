"""Tests for VPS provider instance utilities."""

from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import SnapshotRecord
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.ssh_utils import per_host_key_dir
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_vps.bare_realizer import BareRealizer
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vps.container_setup import emit_docker_build_output
from imbue.mngr_vps.container_setup import is_retryable_rsync_error
from imbue.mngr_vps.container_setup import redact_secret_env
from imbue.mngr_vps.container_setup import remove_host_from_known_hosts
from imbue.mngr_vps.container_setup import resolve_dockerfile_paths
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import VpsListingEntry
from imbue.mngr_vps.docker_realizer import DockerRealizer
from imbue.mngr_vps.errors import BareIsolationNotSupportedError
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vps.errors import VpsHostKeyMissingError
from imbue.mngr_vps.errors import VpsHostSizeUnknownError
from imbue.mngr_vps.errors import VpsInstanceLeakedError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.host_setup import MEM_TOTAL_PROBE_COMMAND
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.instance import MinimalVpsProvider
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vps.instance import _wait_for_cloud_init_marker
from imbue.mngr_vps.instance import build_vps_tags
from imbue.mngr_vps.instance import listing_entry_from_host_id_marker
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.interfaces import SnapshotCapableRealizer
from imbue.mngr_vps.primitives import ISOLATION_TAG_KEY
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME
from imbue.mngr_vps.primitives import VpsDiskGb
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus
from imbue.mngr_vps.primitives import VpsMemoryMib
from imbue.mngr_vps.primitives import VpsVcpuCount
from imbue.mngr_vps.primitives import isolation_from_marker
from imbue.mngr_vps.sizing import VpsInstanceShape
from imbue.mngr_vps.vps_client import ExternallyManagedVpsClient
from imbue.mngr_vps.vps_client import VpsClientInterface
from imbue.mngr_vps.vps_keys import mint_vps_root_keypair

# MinimalVpsProvider._parse_build_args (no-provisioning, no-prefix shape)


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


# One representative stderr string per retryable pattern, so every retryable branch is
# exercised (a dropped pattern flips exactly one case to failing). The last case comes
# from mngr's shared classifier rather than from this project's own list.
@pytest.mark.parametrize(
    "stderr",
    [
        "rsync: write error: Broken pipe (32)",
        "rsync: Connection reset by peer",
        "ssh: connect to host 1.2.3.4 port 22: Connection refused",
        "rsync: [sender] failed: Connection timed out",
        "client_loop: send disconnect: Broken pipe",
        "ssh: connect to host 1.2.3.4 port 22: No route",
        "connect to address 1.2.3.4: Network is unreachable",
        "kex_exchange_identification: read: Connection reset by peer",
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


# _wait_for_cloud_init_marker


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


# create_host runs pre-create validation before any provider write


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


# create_host: an instance whose boot wait fails must not leak


class _BootNeverReadyVpsClient(ExternallyManagedVpsClient):
    """Client whose instance launches but never becomes active; records what create cleans up."""

    destroyed: list[VpsInstanceId] = Field(default_factory=list)
    deleted_ssh_keys: list[str] = Field(default_factory=list)

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        return "key-1"

    def delete_ssh_key(self, key_id: str) -> None:
        self.deleted_ssh_keys.append(key_id)

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        return VpsInstanceId("vps-never-ready")

    def wait_for_instance_active(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> str:
        raise VpsProvisioningError(f"instance {instance_id} did not become active within {timeout_seconds}s")

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed.append(instance_id)


def _boot_never_ready_provider(temp_mngr_ctx: MngrContext) -> tuple[MinimalVpsProvider, _BootNeverReadyVpsClient]:
    client = _BootNeverReadyVpsClient()
    return _minimal_provider(temp_mngr_ctx, vps_client=client), client


def test_create_host_destroys_the_instance_when_its_boot_wait_fails(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instance exists and bills from the moment it is created, so a boot wait that gives up must destroy it.

    ``create_host`` only learns the instance id from ``_provision_vps``'s return
    value, which a failed wait never reaches.
    """
    monkeypatch.delenv("MNGR_KEEP_FAILED_HOSTS", raising=False)
    provider, client = _boot_never_ready_provider(temp_mngr_ctx)

    with pytest.raises(VpsProvisioningError, match="did not become active"):
        provider.create_host(HostName("never-ready"))

    assert client.destroyed == [VpsInstanceId("vps-never-ready")]
    assert client.deleted_ssh_keys == ["key-1"]


def test_create_host_keeps_the_instance_whose_boot_wait_fails_when_asked_to_keep_failed_hosts(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_KEEP_FAILED_HOSTS", "1")
    provider, client = _boot_never_ready_provider(temp_mngr_ctx)

    with pytest.raises(VpsProvisioningError, match="did not become active"):
        provider.create_host(HostName("never-ready"))

    assert client.destroyed == []
    assert client.deleted_ssh_keys == []


# Realizer selection (isolation axis)


def _minimal_provider(
    temp_mngr_ctx: MngrContext,
    isolation: IsolationMode = IsolationMode.CONTAINER,
    vps_client: VpsClientInterface | None = None,
    config: VpsProviderConfig | None = None,
    provider_cls: type[MinimalVpsProvider] = MinimalVpsProvider,
) -> MinimalVpsProvider:
    """A ``provider_cls`` on the test context; ``config`` replaces the default config built from ``isolation``."""
    return provider_cls(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=(
            VpsProviderConfig(backend=ProviderBackendName("test-vps-docker"), isolation=isolation)
            if config is None
            else config
        ),
        vps_client=ExternallyManagedVpsClient() if vps_client is None else vps_client,
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
    host_id = HostId.generate()
    bare_record = _record_for(host_id, container_name=None)
    realizer = provider._realizer_for_record(bare_record)
    assert isinstance(realizer, BareRealizer)
    # The bare agent endpoint is the VM's own port 22 -- not the container port
    # 2222 -- opened with the host's own VPS-root key (minted at create).
    mint_vps_root_keypair(provider._key_dir(), host_id)
    assert realizer.agent_endpoint("10.0.0.1", host_id).port == 22


def test_realizer_for_record_picks_container_for_a_container_record(temp_mngr_ctx: MngrContext) -> None:
    """A container host (record names a container) resolves to the DockerRealizer."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    container_record = _record_for(HostId.generate(), container_name="mngr-agent-abc")
    realizer = provider._realizer_for_record(container_record)
    assert isinstance(realizer, DockerRealizer)
    assert realizer.agent_endpoint("10.0.0.1", HostId.generate()).port == provider.config.container_ssh_port


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
    def _make_outer_for_vps_ip(self, vps_ip: str, host_id: HostId) -> Iterator[OuterHostInterface]:
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


class _ExtraStartArgsMinimalProvider(MinimalVpsProvider):
    """MinimalVpsProvider with provider-computed start args, to pin the composition order.

    Stands in for providers like the imbue_cloud slice provider, whose
    ``_compute_extra_start_args`` injects the per-slice container memory cap.
    """

    def _compute_extra_start_args(self, mem_total_kib: int) -> tuple[str, ...]:
        cap_mib = mem_total_kib // 1024 - 1024
        return (f"--memory={cap_mib}m", f"--memory-swap={cap_mib}m")


def test_effective_start_args_compose_in_last_one_wins_order(temp_mngr_ctx: MngrContext) -> None:
    """Runtime, tmpfs, restart policy, then default_start_args, provider-computed args, and the caller's last.

    Docker's last-one-wins semantics make this ordering the contract: an explicit
    caller flag must be able to override a provider-computed one (e.g. the slice
    memory cap), and the provider-computed args must actually reach the composed
    tuple at all.
    """
    provider = _ExtraStartArgsMinimalProvider(
        name=ProviderInstanceName("test-vps"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(
            backend=ProviderBackendName("test-vps"),
            docker_runtime="runsc",
            default_start_args=("--cap-add=SYS_PTRACE",),
        ),
        vps_client=ExternallyManagedVpsClient(),
    )
    assert provider._compose_effective_start_args(["--memory=4096m"], _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--runtime",
        "runsc",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp:exec,size=993m",
        "--restart=unless-stopped",
        "--cap-add=SYS_PTRACE",
        "--memory=6921m",
        "--memory-swap=6921m",
        "--memory=4096m",
    )


# An 8 GB cloud VM reports about 7945 MiB of MemTotal, so its /tmp cap is 993 MiB.
_EIGHT_GB_VM_MEM_TOTAL_KIB = 8136000


def test_effective_start_args_add_only_the_restart_policy_by_default(temp_mngr_ctx: MngrContext) -> None:
    """The base provider computes no extra args and configures no runtime; every container gets the restart policy."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    assert provider._compose_effective_start_args(["--cpus=2"], _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--restart=unless-stopped",
        "--cpus=2",
    )
    assert provider._compose_effective_start_args(None, _EIGHT_GB_VM_MEM_TOTAL_KIB) == ("--restart=unless-stopped",)


def test_effective_start_args_keep_the_callers_restart_policy(temp_mngr_ctx: MngrContext) -> None:
    """A caller that sets a policy (the gen-2 slice create template) gets no second one, in either position."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    assert provider._compose_effective_start_args(
        ["--workdir=/", "--restart=unless-stopped"], _EIGHT_GB_VM_MEM_TOTAL_KIB
    ) == (
        "--workdir=/",
        "--restart=unless-stopped",
    )
    configured = _minimal_provider(
        temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps"), default_start_args=("--restart=no",)),
    )
    assert configured._compose_effective_start_args(None, _EIGHT_GB_VM_MEM_TOTAL_KIB) == ("--restart=no",)


def test_effective_start_args_add_no_tmpfs_under_runc(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(
        temp_mngr_ctx, config=VpsProviderConfig(backend=ProviderBackendName("test-vps"), docker_runtime="runc")
    )
    assert provider._compose_effective_start_args(None, _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--runtime",
        "runc",
        "--restart=unless-stopped",
    )


def test_effective_start_args_under_runsc_add_only_the_tmpfs_mounts_not_already_configured(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A caller that already mounts /run (the gen-2 slice bake, the old minds AWS block) gets no duplicate."""
    provider = _minimal_provider(
        temp_mngr_ctx,
        config=VpsProviderConfig(
            backend=ProviderBackendName("test-vps"),
            docker_runtime="runsc",
            default_start_args=("--tmpfs", "/run", "--workdir=/"),
        ),
    )
    assert provider._compose_effective_start_args(None, _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--runtime",
        "runsc",
        "--tmpfs",
        "/tmp:exec,size=993m",
        "--restart=unless-stopped",
        "--tmpfs",
        "/run",
        "--workdir=/",
    )
    assert provider._compose_effective_start_args(["--tmpfs=/tmp:rw,size=1g"], _EIGHT_GB_VM_MEM_TOTAL_KIB) == (
        "--runtime",
        "runsc",
        "--restart=unless-stopped",
        "--tmpfs",
        "/run",
        "--workdir=/",
        "--tmpfs=/tmp:rw,size=1g",
    )


class _CappingMinimalProvider(MinimalVpsProvider):
    """MinimalVpsProvider that keeps the base provider's create-time memory cap (which Minimal disables)."""

    def _apply_container_memory_cap(self, outer: OuterHostInterface, container_name: str) -> None:
        VpsProvider._apply_container_memory_cap(self, outer, container_name)


def _capping_provider(temp_mngr_ctx: MngrContext) -> _CappingMinimalProvider:
    return _CappingMinimalProvider(
        name=ProviderInstanceName("test-vps"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps")),
        vps_client=ExternallyManagedVpsClient(),
    )


def test_apply_container_memory_cap_starts_the_reconciler_and_accepts_the_expected_cap(
    temp_mngr_ctx: MngrContext,
) -> None:
    issued: list[str] = []

    def responder(command: str) -> CommandResult:
        issued.append(command)
        # An 8 GB VM: MemTotal 8136000 KiB -> cap 6921 MiB.
        return CommandResult(stdout="mngr-memory-unit=present\n8136000\n7257194496\n", stderr="", success=True)

    outer, _stub = _scripted_outer(responder)
    _capping_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")
    assert len(issued) == 1
    assert "systemctl start mngr-vps-container-memory.service" in issued[0]
    assert issued[0].endswith("mngr-my-host")


def test_apply_container_memory_cap_raises_when_the_cap_did_not_apply(temp_mngr_ctx: MngrContext) -> None:
    outer, _stub = _scripted_outer(
        lambda command: CommandResult(stdout="mngr-memory-unit=present\n8136000\n0\n", stderr="", success=True)
    )
    with pytest.raises(VpsProvisioningError, match="memory cap is 0 bytes, expected 7257194496 bytes"):
        _capping_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")


def test_apply_container_memory_cap_raises_when_the_command_fails(temp_mngr_ctx: MngrContext) -> None:
    outer, _stub = _scripted_outer(
        lambda command: CommandResult(stdout="", stderr="Failed to start unit", success=False)
    )
    with pytest.raises(VpsProvisioningError, match="Failed to cap the memory"):
        _capping_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")


def test_apply_container_memory_cap_leaves_hosts_without_the_reconciler_alone(temp_mngr_ctx: MngrContext) -> None:
    """A host whose setup never installed the unit (e.g. a slice VM) is not capped and does not fail."""
    outer, stub = _scripted_outer(
        lambda command: CommandResult(stdout="mngr-memory-unit=absent\n8136000\n7340032000\n", stderr="", success=True)
    )
    _capping_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")
    assert stub.call_count == 1


def test_apply_container_memory_cap_tolerates_a_vm_too_small_to_cap(temp_mngr_ctx: MngrContext) -> None:
    outer, stub = _scripted_outer(
        lambda command: CommandResult(stdout="mngr-memory-unit=present\n900000\n0\n", stderr="", success=True)
    )
    _capping_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")
    assert stub.call_count == 1


def test_read_vm_mem_total_kib_issues_the_probe_and_parses_its_value(temp_mngr_ctx: MngrContext) -> None:
    issued: list[str] = []

    def responder(command: str) -> CommandResult:
        issued.append(command)
        return CommandResult(stdout="8136000\n", stderr="", success=True)

    outer, _stub = _scripted_outer(responder)
    assert _capping_provider(temp_mngr_ctx)._read_vm_mem_total_kib(outer) == 8136000
    assert issued == [MEM_TOTAL_PROBE_COMMAND]


def test_read_vm_mem_total_kib_raises_when_the_probe_fails(temp_mngr_ctx: MngrContext) -> None:
    outer, _stub = _scripted_outer(
        lambda command: CommandResult(stdout="", stderr="cannot open /proc/meminfo", success=False)
    )
    with pytest.raises(VpsProvisioningError, match="MemTotal"):
        _capping_provider(temp_mngr_ctx)._read_vm_mem_total_kib(outer)


def test_minimal_vps_provider_never_caps_container_memory(temp_mngr_ctx: MngrContext) -> None:
    """An externally provisioned VPS is sized by its owner; the base cap must not reach it."""
    outer, stub = _scripted_outer(lambda command: CommandResult(stdout="", stderr="", success=True))
    _minimal_provider(temp_mngr_ctx)._apply_container_memory_cap(outer, "mngr-my-host")
    assert stub.call_count == 0


# Host size reporting


_RECORDED_SHAPE = VpsInstanceShape(
    vcpu_count=VpsVcpuCount(2), memory_mib=VpsMemoryMib(4096), root_disk_gb=VpsDiskGb(80)
)


def _record_with_shape(host_id: HostId, shape: VpsInstanceShape | None, plan: str = "p") -> VpsHostRecord:
    now = datetime.now(timezone.utc)
    return VpsHostRecord(
        certified_host_data=CertifiedHostData(host_id=str(host_id), host_name="h", created_at=now, updated_at=now),
        vps_ip="10.0.0.1",
        config=VpsHostConfig(
            vps_instance_id=VpsInstanceId("i-1"), region="r", plan=plan, shape=shape, container_name="c"
        ),
    )


class _PlanTableProvider(MinimalVpsProvider):
    """A provider whose legacy plan table knows exactly one plan."""

    def _legacy_shape_for_plan(self, plan: str) -> VpsInstanceShape | None:
        if plan == "known-plan":
            return VpsInstanceShape(vcpu_count=VpsVcpuCount(1), memory_mib=VpsMemoryMib(2048), root_disk_gb=None)
        return None


def test_get_host_resources_reports_the_shape_recorded_at_create(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    record = _record_with_shape(host_id, _RECORDED_SHAPE)
    provider._host_record_cache[host_id] = record
    host = provider._create_offline_host(record, observed_state=None)

    resources = provider.get_host_resources(host)

    assert resources.cpu.count == 2
    assert resources.memory_gb == 4.0
    assert resources.disk_gb == 80.0


def test_get_host_resources_falls_back_to_the_plan_table_for_a_record_without_a_shape(
    temp_mngr_ctx: MngrContext,
) -> None:
    provider = _PlanTableProvider(
        name=ProviderInstanceName("test-vps-docker"),
        host_dir=temp_mngr_ctx.config.default_host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=VpsProviderConfig(backend=ProviderBackendName("test-vps-docker")),
        vps_client=ExternallyManagedVpsClient(),
    )
    host_id = HostId.generate()
    record = _record_with_shape(host_id, None, plan="known-plan")
    provider._host_record_cache[host_id] = record
    host = provider._create_offline_host(record, observed_state=None)

    resources = provider.get_host_resources(host)

    assert resources.cpu.count == 1
    assert resources.memory_gb == 2.0
    assert resources.disk_gb is None


def test_get_host_resources_raises_rather_than_invent_a_size_for_an_unknown_plan(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    record = _record_with_shape(host_id, None, plan="never-seen-plan")
    provider._host_record_cache[host_id] = record
    host = provider._create_offline_host(record, observed_state=None)

    with pytest.raises(VpsHostSizeUnknownError, match="never-seen-plan"):
        provider.get_host_resources(host)


def test_recorded_resources_or_none_reports_an_unknown_size_as_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    record = _record_with_shape(host_id, None, plan="never-seen-plan")
    provider._host_record_cache[host_id] = record
    host = provider._create_offline_host(record, observed_state=None)

    with capture_loguru() as captured:
        assert provider._recorded_resources_or_none(host) is None
    assert any("Could not determine the size of host" in line for line in captured.getvalue().splitlines())


class _ShapeDescribingVpsClient(ExternallyManagedVpsClient):
    """Client whose shape describe answers from a canned value or raises an API error."""

    shape: VpsInstanceShape | None = Field(default=None)
    api_error_message: str | None = Field(default=None)

    def get_instance_shape(self, instance_id: VpsInstanceId, plan: str) -> VpsInstanceShape | None:
        if self.api_error_message is not None:
            raise VpsApiError(500, self.api_error_message)
        return self.shape


def test_describe_instance_shape_returns_what_the_client_reports(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx, vps_client=_ShapeDescribingVpsClient(shape=_RECORDED_SHAPE))

    assert provider._describe_instance_shape(VpsInstanceId("i-1"), "p") == _RECORDED_SHAPE


def test_describe_instance_shape_tolerates_an_api_error_so_the_create_still_finishes(
    temp_mngr_ctx: MngrContext,
) -> None:
    provider = _minimal_provider(temp_mngr_ctx, vps_client=_ShapeDescribingVpsClient(api_error_message="throttled"))

    with capture_loguru() as captured:
        assert provider._describe_instance_shape(VpsInstanceId("i-1"), "p") is None
    assert any("throttled" in line for line in captured.getvalue().splitlines())


# Per-host VPS-root client key: listing entries, create-time minting, reads


def test_listing_entry_from_host_id_marker_parses_a_host_id() -> None:
    host_id = HostId.generate()
    entry = listing_entry_from_host_id_marker("203.0.113.1", str(host_id), "i-1")
    assert entry == VpsListingEntry(hostname="203.0.113.1", host_id=host_id)


def test_listing_entry_from_host_id_marker_skips_missing_or_malformed_markers() -> None:
    """A row whose marker is missing or not a host id has no per-host key to open it with.

    It is skipped with a warning that names the row and the offending marker, so
    an unexpected value stamped on an instance is visible on every listing.
    """
    with capture_loguru() as logs:
        assert listing_entry_from_host_id_marker("203.0.113.2", None, "i-untagged") is None
        assert listing_entry_from_host_id_marker("203.0.113.3", "not-a-host-id", "i-garbage") is None
    output = logs.getvalue()
    assert "i-untagged" in output
    assert "i-garbage" in output
    assert "not-a-host-id" in output


def test_make_outer_for_vps_ip_raises_before_connecting_when_no_key_opens_the_host(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Read paths resolve the host's key and never mint one; a keyless host fails before any SSH."""
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    host_id = HostId.generate()
    with pytest.raises(VpsHostKeyMissingError):
        with provider._make_outer_for_vps_ip("192.0.2.1", host_id):
            raise AssertionError("an outer must not be opened without a key")
    assert not per_host_key_dir(provider._key_dir(), host_id).exists()
    assert not (provider._key_dir() / VPS_SSH_KEY_NAME).exists()


def test_record_outer_host_key_attributes_the_pin_to_the_host(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx, IsolationMode.CONTAINER)
    host_id = HostId.generate()
    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExampleEx"

    provider.record_outer_host_key("203.0.113.5", 22, public_key, host_id=host_id)

    record = load_host_key_record(provider._vps_known_hosts_path(), host_id)
    assert record is not None
    assert [(pin.address, pin.port, pin.public_key) for pin in record.pins] == [("203.0.113.5", 22, public_key)]


class _RecordingVpsClient(VpsClientInterface):
    """VPS client that records the key/instance calls the create path makes and provisions nothing real."""

    uploaded_public_keys: list[str] = Field(default_factory=list)
    deleted_key_ids: list[str] = Field(default_factory=list)
    created_user_data: list[str] = Field(default_factory=list)
    created_ssh_key_ids: list[tuple[str, ...]] = Field(default_factory=list)
    destroyed_instance_ids: list[VpsInstanceId] = Field(default_factory=list)
    instance_ip: str = Field(default="203.0.113.9")
    is_destroy_failing: bool = Field(default=False)
    is_upload_failing: bool = Field(default=False)

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        self.created_user_data.append(user_data)
        self.created_ssh_key_ids.append(tuple(ssh_key_ids))
        return VpsInstanceId("i-recorded")

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        self.destroyed_instance_ids.append(instance_id)
        if self.is_destroy_failing:
            raise MngrError(f"simulated destroy failure for {instance_id}")

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        return VpsInstanceStatus.ACTIVE

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        return self.instance_ip

    def get_instance_shape(self, instance_id: VpsInstanceId, plan: str) -> VpsInstanceShape | None:
        return None

    def wait_for_instance_active(self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0) -> str:
        return self.instance_ip

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        if self.is_upload_failing:
            raise MngrError("simulated SSH key upload failure")
        self.uploaded_public_keys.append(public_key)
        return f"key-{len(self.uploaded_public_keys)}"

    def delete_ssh_key(self, key_id: str) -> None:
        self.deleted_key_ids.append(key_id)


class _StopAfterProvisionProbe(MngrError):
    """Sentinel raised by the probe provider once the pre-provision state has been captured."""


class _ProvisionProbeProvider(MinimalVpsProvider):
    """Captures what ``create_host`` had on disk (and threaded in) at the moment it provisions."""

    _threaded_public_key: str | None = PrivateAttr(default=None)
    _per_host_key_present_at_provision: bool = PrivateAttr(default=False)
    _legacy_key_present_at_provision: bool = PrivateAttr(default=True)

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        parsed: ParsedVpsBuildOptions,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
        vps_public_key: str,
    ) -> tuple[VpsInstanceId, str]:
        self._threaded_public_key = vps_public_key
        self._per_host_key_present_at_provision = (
            per_host_key_dir(self._key_dir(), host_id) / VPS_SSH_KEY_NAME
        ).exists()
        self._legacy_key_present_at_provision = (self._key_dir() / VPS_SSH_KEY_NAME).exists()
        raise _StopAfterProvisionProbe("stop after capturing the pre-provision state")


def test_opening_hosts_with_the_legacy_provider_key_is_reported_once_per_process(temp_mngr_ctx: MngrContext) -> None:
    """Hosts that only authorize the retired provider-wide key are reported at info once, naming the instance."""
    provider = _minimal_provider(temp_mngr_ctx)
    save_ssh_keypair(provider._key_dir(), VPS_SSH_KEY_NAME)
    first_host_id = HostId.generate()
    second_host_id = HostId.generate()

    with capture_loguru("INFO") as log_output:
        first_keypair = provider._resolve_vps_ssh_keypair(first_host_id)
        second_keypair = provider._resolve_vps_ssh_keypair(second_host_id)
        provider._resolve_vps_ssh_keypair(first_host_id)

    assert first_keypair.is_legacy_provider_key is True
    assert second_keypair.is_legacy_provider_key is True
    assert provider.is_legacy_key_fallback_logged is True
    legacy_lines = [line for line in log_output.getvalue().splitlines() if "legacy provider-wide VPS key" in line]
    assert len(legacy_lines) == 1
    assert str(provider.name) in legacy_lines[0]
    assert str(first_host_id) in legacy_lines[0]


def test_opening_a_host_with_its_own_key_reports_nothing(temp_mngr_ctx: MngrContext) -> None:
    provider = _minimal_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    mint_vps_root_keypair(provider._key_dir(), host_id)

    with capture_loguru("INFO") as log_output:
        keypair = provider._resolve_vps_ssh_keypair(host_id)

    assert keypair.is_legacy_provider_key is False
    assert provider.is_legacy_key_fallback_logged is False
    assert "legacy provider-wide VPS key" not in log_output.getvalue()


def test_create_host_mints_a_per_host_vps_key_uploads_it_and_cleans_it_up_on_failure(
    temp_mngr_ctx: MngrContext,
) -> None:
    """``create_host`` mints the host's own VPS-root key (no provider-wide key), uploads exactly that
    public key to the cloud, threads it into provisioning, and removes the per-host material again
    when the create fails (alongside the cloud-side key)."""
    client = _RecordingVpsClient()
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_ProvisionProbeProvider)
    assert isinstance(provider, _ProvisionProbeProvider)

    with pytest.raises(_StopAfterProvisionProbe):
        provider.create_host(HostName("probe-host"))

    assert provider._per_host_key_present_at_provision is True
    assert provider._legacy_key_present_at_provision is False
    assert client.uploaded_public_keys == [provider._threaded_public_key]
    assert client.deleted_key_ids == ["key-1"]
    # The failed create leaves no per-host material (and no provider-wide key) behind.
    assert list((provider._key_dir() / "host_keys").glob("*")) == []
    assert not (provider._key_dir() / VPS_SSH_KEY_NAME).exists()


def test_create_host_removes_the_per_host_key_when_the_cloud_key_upload_fails(temp_mngr_ctx: MngrContext) -> None:
    """The key material is minted before the first provider write, so a failed upload must drop it again.

    Nothing was created cloud-side (no instance, no registered key), so the cleanup
    has nothing to destroy or delete there and only removes the orphaned local material.
    """
    client = _RecordingVpsClient(is_upload_failing=True)
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_ProvisionProbeProvider)
    assert isinstance(provider, _ProvisionProbeProvider)

    with pytest.raises(MngrError, match="simulated SSH key upload failure"):
        provider.create_host(HostName("upload-fails-host"))

    assert provider._threaded_public_key is None, "provisioning must not start without a registered key"
    assert client.deleted_key_ids == []
    assert client.destroyed_instance_ids == []
    assert list((provider._key_dir() / "host_keys").glob("*")) == []
    assert not (provider._key_dir() / VPS_SSH_KEY_NAME).exists()


class _OuterOpenFailure(MngrError):
    """Sentinel raised in place of opening the outer host once the VPS has been provisioned."""


class _FailAfterProvisionProvider(MinimalVpsProvider):
    """Provisions a (fake) VPS and then fails on the first outer connection, exercising create's cleanup."""

    _created_host_id: HostId | None = PrivateAttr(default=None)

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        parsed: ParsedVpsBuildOptions,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
        vps_public_key: str,
    ) -> tuple[VpsInstanceId, str]:
        self._created_host_id = host_id
        return VpsInstanceId("i-leaked"), "203.0.113.9"

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str, host_id: HostId) -> Iterator[OuterHostInterface]:
        raise _OuterOpenFailure("simulated outer connection failure after provisioning")
        yield


@pytest.mark.parametrize(
    ("is_destroy_failing", "is_key_kept"),
    [
        pytest.param(False, False, id="destroyed"),
        pytest.param(True, True, id="destroy-failed"),
    ],
)
def test_create_host_keeps_the_per_host_key_when_the_failed_vps_cannot_be_destroyed(
    temp_mngr_ctx: MngrContext, is_destroy_failing: bool, is_key_kept: bool
) -> None:
    """A leaked VPS authorizes only this host's per-host key, so the failed create keeps it on disk.

    When the cleanup destroy succeeds the material opens nothing and is removed;
    when it fails the instance lives on under this host id, and dropping the key
    would leave it unreachable from (and invisible to) this machine.
    """
    client = _RecordingVpsClient(is_destroy_failing=is_destroy_failing)
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_FailAfterProvisionProvider)
    assert isinstance(provider, _FailAfterProvisionProvider)

    with pytest.raises(_OuterOpenFailure):
        provider.create_host(HostName("leaked-host"))

    host_id = provider._created_host_id
    assert host_id is not None
    assert client.destroyed_instance_ids == [VpsInstanceId("i-leaked")]
    assert client.deleted_key_ids == ["key-1"]
    assert (per_host_key_dir(provider._key_dir(), host_id) / VPS_SSH_KEY_NAME).exists() is is_key_kept


class _BootstrapProvisionProvider(MinimalVpsProvider):
    """Runs the real ``_provision_vps`` with the boot/SSH waits stubbed out (no network)."""

    _outer_host_ids: list[HostId] = PrivateAttr(default_factory=list)

    def _wait_for_sshd_on_vps(self, vps_ip: str, timeout_seconds: float) -> None:
        pass

    def _wait_for_cloud_init(self, outer: OuterHostInterface, timeout_seconds: float) -> None:
        pass

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str, host_id: HostId) -> Iterator[OuterHostInterface]:
        self._outer_host_ids.append(host_id)
        yield cast(OuterHostInterface, object())


def test_provision_vps_injects_the_per_host_key_into_the_bootstrap_and_pins_the_vps_host_key(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The first-boot payload authorizes the host's own VPS-root public key on root, the cloud
    create receives the uploaded per-host key id, and the VPS sshd host key is pinned under the
    host's own store record."""
    client = _RecordingVpsClient()
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_BootstrapProvisionProvider)
    assert isinstance(provider, _BootstrapProvisionProvider)
    host_id = HostId.generate()
    _vps_key_path, vps_public_key = provider._mint_vps_ssh_keypair(host_id)
    vps_host_key_path, vps_host_public_key = provider._get_vps_host_keypair(host_id)
    key_id = client.upload_ssh_key(f"mngr-{provider.name}-{host_id}", vps_public_key)

    instance_id, vps_ip = provider._provision_vps(
        host_id=host_id,
        name=HostName("boot-host"),
        parsed=provider._parse_build_args(None),
        vps_host_key_path=vps_host_key_path,
        vps_host_public_key=vps_host_public_key,
        vps_ssh_key_id=key_id,
        vps_public_key=vps_public_key,
    )

    assert (instance_id, vps_ip) == (VpsInstanceId("i-recorded"), "203.0.113.9")
    assert client.created_ssh_key_ids == [(key_id,)]
    assert vps_public_key in client.created_user_data[0]
    # The bootstrap wait opened the outer for THIS host (its per-host key).
    assert provider._outer_host_ids == [host_id]
    record = load_host_key_record(provider._vps_known_hosts_path(), host_id)
    assert record is not None
    assert [(pin.address, pin.port, pin.public_key) for pin in record.pins] == [
        ("203.0.113.9", 22, vps_host_public_key)
    ]


class _CloudInitFailure(MngrError):
    """Sentinel raised in place of the cloud-init wait once the (fake) VPS instance exists."""


class _BootstrapFailingProvider(_BootstrapProvisionProvider):
    """Runs the real ``_provision_vps`` whose bootstrap wait fails after the instance was created."""

    def _wait_for_cloud_init(self, outer: OuterHostInterface, timeout_seconds: float) -> None:
        raise _CloudInitFailure("simulated bootstrap timeout")


def test_provision_vps_destroys_its_instance_when_a_later_wait_fails(temp_mngr_ctx: MngrContext) -> None:
    """A boot/bootstrap wait failing after the cloud create must not leak the instance.

    ``create_host`` only learns the instance id from ``_provision_vps``'s return value,
    so the base ``_provision_vps`` tears down the instance it created itself; the
    caller's cleanup then correctly treats the VPS as gone and removes the per-host
    key material (alongside the cloud-side key).
    """
    client = _RecordingVpsClient()
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_BootstrapFailingProvider)
    assert isinstance(provider, _BootstrapFailingProvider)

    with pytest.raises(_CloudInitFailure):
        provider.create_host(HostName("boot-fails-host"))

    assert client.destroyed_instance_ids == [VpsInstanceId("i-recorded")]
    assert client.deleted_key_ids == ["key-1"]
    (host_id,) = provider._outer_host_ids
    assert not per_host_key_dir(provider._key_dir(), host_id).exists()


def test_provision_vps_surfaces_a_leaked_instance_and_keeps_its_key(temp_mngr_ctx: MngrContext) -> None:
    """When the instance a failed provision created cannot be destroyed, the leak is reported and the key kept.

    ``create_host`` never sees the instance id of a ``_provision_vps`` that raised,
    so the leak travels on ``VpsInstanceLeakedError``, which names the bootstrap
    failure and chains it as its cause; the cleanup then
    keeps the per-host key -- the only key the leaked VPS authorizes -- exactly as
    it does for a leak after provisioning returned.
    """
    client = _RecordingVpsClient(is_destroy_failing=True)
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_BootstrapFailingProvider)
    assert isinstance(provider, _BootstrapFailingProvider)

    with pytest.raises(VpsInstanceLeakedError) as exc_info:
        provider.create_host(HostName("boot-fails-leaked-host"))

    assert exc_info.value.vps_instance_id == VpsInstanceId("i-recorded")
    assert isinstance(exc_info.value.__cause__, _CloudInitFailure)
    assert "simulated bootstrap timeout" in str(exc_info.value)
    # One destroy attempt (in the provision path), none repeated by create's cleanup.
    assert client.destroyed_instance_ids == [VpsInstanceId("i-recorded")]
    assert client.deleted_key_ids == ["key-1"]
    (host_id,) = provider._outer_host_ids
    assert (per_host_key_dir(provider._key_dir(), host_id) / VPS_SSH_KEY_NAME).exists()


def test_provision_vps_keeps_its_instance_and_key_when_failed_hosts_are_kept(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MNGR_KEEP_FAILED_HOSTS=1`` leaves the created instance, its cloud key, and the local key material for debugging."""
    monkeypatch.setenv("MNGR_KEEP_FAILED_HOSTS", "1")
    client = _RecordingVpsClient()
    provider = _minimal_provider(temp_mngr_ctx, vps_client=client, provider_cls=_BootstrapFailingProvider)
    assert isinstance(provider, _BootstrapFailingProvider)

    with pytest.raises(_CloudInitFailure):
        provider.create_host(HostName("boot-fails-kept-host"))

    assert client.destroyed_instance_ids == []
    assert client.deleted_key_ids == []
    (host_id,) = provider._outer_host_ids
    assert (per_host_key_dir(provider._key_dir(), host_id) / VPS_SSH_KEY_NAME).exists()


class _NoopTeardownRealizer:
    """Duck-typed realizer whose placement teardown is a no-op, so destroy runs without real docker."""

    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> None:
        pass


class _DestroyProbeProvider(MinimalVpsProvider):
    """Destroys a host whose record is seeded in the cache, with the outer connection stubbed out."""

    @contextmanager
    def _make_outer_for_vps_ip(self, vps_ip: str, host_id: HostId) -> Iterator[OuterHostInterface]:
        yield cast(OuterHostInterface, object())


def _provider_with_cached_container_host(
    mngr_ctx: MngrContext, client: _RecordingVpsClient, provider_cls: type[MinimalVpsProvider]
) -> tuple[MinimalVpsProvider, HostId]:
    """A provider holding one cached container-placed host record, with the placement teardown stubbed out."""
    provider = _minimal_provider(mngr_ctx, vps_client=client, provider_cls=provider_cls)
    provider._realizer_cache = {IsolationMode.CONTAINER: cast(HostRealizer, _NoopTeardownRealizer())}
    host_id = HostId.generate()
    provider._host_record_cache[host_id] = _record_for(host_id, container_name="mngr-host")
    return provider, host_id


@pytest.mark.parametrize(
    ("is_destroy_failing", "is_key_kept"),
    [
        pytest.param(False, False, id="destroyed"),
        pytest.param(True, True, id="destroy-failed"),
    ],
)
def test_destroy_host_removes_the_local_key_material_only_once_the_vps_is_gone(
    temp_mngr_ctx: MngrContext, is_destroy_failing: bool, is_key_kept: bool
) -> None:
    """The per-host key and pins are removed with the VPS -- and kept when the VPS destroy fails.

    The per-host key is the only key that opens the VPS root, so dropping it after
    a failed cloud destroy would leave the still-billing instance unreachable
    from this machine: a retried destroy would fail on the missing key before it
    ever reached the cloud API again.
    """
    client = _RecordingVpsClient(is_destroy_failing=is_destroy_failing)
    provider, host_id = _provider_with_cached_container_host(temp_mngr_ctx, client, _DestroyProbeProvider)
    mint_vps_root_keypair(provider._key_dir(), host_id)
    provider.record_outer_host_key(
        "10.0.0.1", 22, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExampleEx", host_id
    )

    if is_destroy_failing:
        with pytest.raises(CleanupFailedGroup):
            provider.destroy_host(host_id)
    else:
        provider.destroy_host(host_id)

    assert client.destroyed_instance_ids == [VpsInstanceId("i-1")]
    assert (per_host_key_dir(provider._key_dir(), host_id) / VPS_SSH_KEY_NAME).exists() is is_key_kept
    assert (load_host_key_record(provider._vps_known_hosts_path(), host_id) is not None) is is_key_kept


def test_destroy_host_still_destroys_the_vps_when_no_local_key_opens_it(temp_mngr_ctx: MngrContext) -> None:
    """A host this machine holds no key for is still destroyed through the cloud API.

    The placement teardown needs the outer connection (and so the per-host key),
    but the VPS destroy takes the placement with it, so a missing key skips that
    step with a warning instead of aborting before the cloud destroy -- otherwise a
    host created elsewhere (or one whose key dir was removed) could never be
    destroyed from here.
    """
    client = _RecordingVpsClient()
    provider, host_id = _provider_with_cached_container_host(temp_mngr_ctx, client, MinimalVpsProvider)
    assert not per_host_key_dir(provider._key_dir(), host_id).exists()

    with capture_loguru() as logs:
        provider.destroy_host(host_id)

    assert client.destroyed_instance_ids == [VpsInstanceId("i-1")]
    assert "Skipping the placement teardown" in logs.getvalue()
