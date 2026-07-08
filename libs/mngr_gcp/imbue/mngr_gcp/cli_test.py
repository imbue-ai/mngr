"""Tests for ``mngr gcp`` CLI subcommands.

Splits the test surface into two layers:

- The firewall-management logic that the ``prepare`` / ``cleanup`` callbacks
  invoke: exercised against ``_StubbedGcpVpsClient`` with hand-written fake
  Firewalls/Instances clients. Bypasses the click runtime so the
  create-when-missing / reuse-when-present and refuse-when-instances-exist /
  delete-when-clean contracts can be asserted directly.
- Click-level smoke tests: invoke the click commands through ``CliRunner`` to
  verify exit codes and user-facing error messages on the paths that don't need
  a real GCE call (``prepare`` / ``cleanup`` ``--help``; the no-credentials path).
"""

import json

import pluggy
import pytest
from click.testing import CliRunner
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.cli import _output_cleanup_result
from imbue.mngr_gcp.cli import _output_prepare_result
from imbue.mngr_gcp.cli import _perform_cleanup
from imbue.mngr_gcp.cli import _perform_state_bucket_cleanup
from imbue.mngr_gcp.cli import _refuse_cleanup_if_instances_exist
from imbue.mngr_gcp.cli import _resolve_provider_config
from imbue.mngr_gcp.cli import gcp_cli_group
from imbue.mngr_gcp.client import FirewallPrepareResult
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.errors import GcpStateBucketNotEmptyError
from imbue.mngr_gcp.testing import FakeFirewallsClient
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import _FAKE_CREDENTIALS
from imbue.mngr_gcp.testing import _FakeStorageClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient
from imbue.mngr_gcp.testing import _StubbedGcsStateBucket
from imbue.mngr_vps.errors import ManagedResourcesExistError


def _prepare_client(firewalls: FakeFirewallsClient) -> _StubbedGcpVpsClient:
    # No ``image``: the operator commands never create instances, so the client
    # is built image-less (mirrors ``_build_operator_client``).
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        allowed_ssh_cidrs=("0.0.0.0/0",),
        stubbed_firewalls_client=firewalls,
    )


def _cleanup_client(instances: FakeInstancesClient, firewalls: FakeFirewallsClient) -> _StubbedGcpVpsClient:
    return _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        stubbed_instances_client=instances,
        stubbed_firewalls_client=firewalls,
    )


def test_prepare_logic_creates_firewall_when_missing() -> None:
    """The privileged path creates the rule when it does not yet exist."""
    firewalls = FakeFirewallsClient()
    client = _prepare_client(firewalls)
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is True
    assert len(firewalls.inserted) == 1
    assert firewalls.inserted[0].name == "mngr-gcp-ssh"


def test_prepare_logic_reuses_firewall_when_present() -> None:
    """When the rule already exists, prepare is a no-op (no insert)."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    client = _prepare_client(firewalls)
    result = client.ensure_firewall()
    assert result.target_tag == "mngr-ssh"
    assert result.was_created is False
    assert firewalls.inserted == []


def test_cleanup_logic_deletes_firewall_when_no_instances() -> None:
    """With no mngr instances, cleanup deletes the rule and returns its name."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    # No aggregated_result on the fake -> no mngr-managed instances anywhere.
    instances = FakeInstancesClient()
    client = _cleanup_client(instances, firewalls)
    assert _perform_cleanup(client) == "mngr-gcp-ssh"
    assert firewalls.deleted == ["mngr-gcp-ssh"]


def test_cleanup_logic_is_noop_when_firewall_missing() -> None:
    """When the rule is already gone, cleanup deletes nothing and returns None (idempotent)."""
    client = _cleanup_client(FakeInstancesClient(), FakeFirewallsClient())
    assert _perform_cleanup(client) is None


def test_cleanup_logic_refuses_when_instances_exist() -> None:
    """A live mngr instance makes cleanup raise without deleting the firewall."""
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    instances = FakeInstancesClient()
    instances.aggregated_result = [
        (
            "zones/us-west1-a",
            [compute_v1.Instance(name="mngr-host-1", status="RUNNING", labels={"mngr-provider": "gcp"})],
        )
    ]
    client = _cleanup_client(instances, firewalls)
    with pytest.raises(ManagedResourcesExistError) as exc_info:
        _perform_cleanup(client)
    # The refusal must name the blocking instance so the operator knows what to destroy.
    assert "mngr-host-1" in str(exc_info.value)
    assert "Refusing" in str(exc_info.value)
    # The firewall must NOT have been deleted while an instance still exists.
    assert firewalls.deleted == []


# =============================================================================
# Cleanup-helpers: instance-exists guard + bucket teardown contract
# =============================================================================


def _make_state_bucket(fake_gcs: _FakeStorageClient, bucket_name: str) -> _StubbedGcsStateBucket:
    return _StubbedGcsStateBucket(
        credentials=_FAKE_CREDENTIALS,
        project_id="test-project",
        region="us-west1",
        bucket_name=bucket_name,
        stubbed_storage_client=fake_gcs,
    )


def test_refuse_cleanup_if_instances_exist_aborts_before_teardown() -> None:
    """The instance-exists guard runs before any teardown, so the bucket survives.

    Reproduces the callback ordering: when an instance is still alive, the guard
    raises before the bucket is touched, so a bucket holding host state stays
    intact for the operator to inspect after destroying the lingering host.
    """
    firewalls = FakeFirewallsClient()
    firewalls.existing = compute_v1.Firewall(name="mngr-gcp-ssh")
    instances = FakeInstancesClient()
    instances.aggregated_result = [
        (
            "zones/us-west1-a",
            [compute_v1.Instance(name="mngr-host-live", status="RUNNING", labels={"mngr-provider": "gcp"})],
        )
    ]
    client = _cleanup_client(instances, firewalls)
    fake_gcs = _FakeStorageClient()
    bucket = _make_state_bucket(fake_gcs, "mngr-state-refuse-first")
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    with pytest.raises(ManagedResourcesExistError, match="Refusing"):
        _refuse_cleanup_if_instances_exist(client)
    # The guard raised before any teardown, so the bucket and its state survive.
    assert bucket.bucket_exists() is True
    assert bucket.has_any_host_state() is True


def test_perform_state_bucket_cleanup_refuses_while_host_state_remains() -> None:
    """The bucket cleanup refuses (deletes nothing) while any host state remains.

    Without ``--force``, orphaned offline state from a removed host must not be
    silently dropped: the helper raises a ``GcpStateBucketNotEmptyError`` pointing
    the operator at ``--force``, and the bucket is left untouched.
    """
    fake_gcs = _FakeStorageClient()
    bucket = _make_state_bucket(fake_gcs, "mngr-state-cleanup-refuse")
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    with pytest.raises(GcpStateBucketNotEmptyError, match="still holds offline host state"):
        _perform_state_bucket_cleanup(bucket, force=False)
    # The bucket must still exist after a refusal.
    assert bucket.bucket_exists() is True


def test_perform_state_bucket_cleanup_force_deletes_despite_host_state() -> None:
    """``--force`` deletes the bucket (and its leftover state) instead of refusing."""
    fake_gcs = _FakeStorageClient()
    bucket = _make_state_bucket(fake_gcs, "mngr-state-cleanup-purge")
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    assert _perform_state_bucket_cleanup(bucket, force=True) == "mngr-state-cleanup-purge"
    assert bucket.bucket_exists() is False


def test_perform_state_bucket_cleanup_deletes_empty_bucket() -> None:
    """With no host state, the bucket cleanup deletes the bucket and returns its name."""
    fake_gcs = _FakeStorageClient()
    bucket = _make_state_bucket(fake_gcs, "mngr-state-cleanup-empty")
    bucket.ensure_bucket()
    assert _perform_state_bucket_cleanup(bucket, force=False) == "mngr-state-cleanup-empty"
    assert bucket.bucket_exists() is False


def test_perform_state_bucket_cleanup_is_noop_when_bucket_absent() -> None:
    """Calling cleanup on a bucket that was never created returns None (idempotent)."""
    fake_gcs = _FakeStorageClient()
    bucket = _make_state_bucket(fake_gcs, "mngr-state-cleanup-absent")
    assert _perform_state_bucket_cleanup(bucket, force=False) is None


# =============================================================================
# format-aware output (prepare / cleanup respect --format)
# =============================================================================


def test_output_prepare_result_human_emits_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode emits one result sentence to stdout (no bare echo line)."""
    result = FirewallPrepareResult(target_tag="mngr-ssh", was_created=True)
    _output_prepare_result(result, "mngr-gcp-ssh", "test-project", "mngr-state-test-project", True, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert (
        captured.out == "Prepared GCP firewall rule mngr-gcp-ssh (tag mngr-ssh) in project test-project\n"
        "Created GCS state bucket mngr-state-test-project in project test-project\n"
    )


def test_output_prepare_result_json_carries_created_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON mode emits a structured object including the created signals (firewall + bucket)."""
    result = FirewallPrepareResult(target_tag="mngr-ssh", was_created=False)
    _output_prepare_result(result, "mngr-gcp-ssh", "test-project", "mngr-state-test-project", False, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "firewall_name": "mngr-gcp-ssh",
        "target_tag": "mngr-ssh",
        "project_id": "test-project",
        "created": False,
        "state_bucket_name": "mngr-state-test-project",
        "state_bucket_created": False,
    }


def test_output_prepare_result_jsonl_emits_prepared_event(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL mode emits a ``prepared`` event with the same fields."""
    result = FirewallPrepareResult(target_tag="mngr-ssh", was_created=True)
    _output_prepare_result(result, "mngr-gcp-ssh", "test-project", "mngr-state-test-project", True, OutputFormat.JSONL)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "prepared"
    assert payload["created"] is True
    assert payload["firewall_name"] == "mngr-gcp-ssh"
    assert payload["state_bucket_name"] == "mngr-state-test-project"
    assert payload["state_bucket_created"] is True


def test_output_cleanup_result_json_reports_deleted(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=True when a rule was removed."""
    _output_cleanup_result(
        "mngr-gcp-ssh", "mngr-gcp-ssh", "test-project", "mngr-state-test-project", OutputFormat.JSON
    )
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "firewall_name": "mngr-gcp-ssh",
        "project_id": "test-project",
        "deleted": True,
        "state_bucket_deleted": "mngr-state-test-project",
    }


def test_output_cleanup_result_json_reports_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=False on the idempotent no-op path."""
    _output_cleanup_result(None, "mngr-gcp-ssh", "test-project", None, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["deleted"] is False
    assert payload["state_bucket_deleted"] is None


def test_prepare_command_help_is_reachable() -> None:
    """`mngr gcp prepare --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--project" in result.output
    assert "--allowed-ssh-cidr" in result.output


def test_cleanup_command_help_is_reachable() -> None:
    """`mngr gcp cleanup --help` should render without invoking GCP."""
    runner = CliRunner()
    result = runner.invoke(gcp_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--project" in result.output
    assert "--firewall-name" in result.output


def test_prepare_command_fails_clearly_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When ADC isn't resolvable, the click command surfaces a clean error.

    Forcing no-ADC: point GOOGLE_APPLICATION_CREDENTIALS at a nonexistent file.
    ``google.auth.default()`` checks that env var first and raises
    ``DefaultCredentialsError`` immediately when it names a missing file, so the
    well-known ADC file is never consulted and the test is hermetic regardless of
    the host's gcloud state. Passes ``obj=plugin_manager`` because ``prepare``
    runs through ``setup_command_context`` (to read ``[providers.NAME]`` from
    settings.toml as defaults), which reads the plugin manager off ``ctx.obj``.
    """
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/adc.json")
    result = cli_runner.invoke(
        gcp_cli_group,
        ["prepare", "--project", "test-project", "--allowed-ssh-cidr", "0.0.0.0/0"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "application default credentials not configured" in result.output.lower()


# =============================================================================
# Provider-config resolution for `mngr gcp prepare`
# =============================================================================
#
# ``_resolve_provider_config`` reads the user's resolved provider config off the
# ``MngrContext`` so a non-default ``default_zone`` / ``network`` /
# ``firewall_name`` in ``[providers.gcp]`` applies to the firewall rule even when
# the matching CLI flag is omitted, keeping prepare aligned with the runtime
# create path. These tests pin that behavior. Mirrors the AWS provider.


def _temp_mngr_ctx_with_provider(temp_mngr_ctx: MngrContext, name: str, config: ProviderInstanceConfig) -> MngrContext:
    """Return ``temp_mngr_ctx`` with ``config`` registered under ``name`` in ``providers``."""
    provider_name = ProviderInstanceName(name)
    new_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().providers, {provider_name: config})
    )
    return temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, new_config))


def test_resolve_provider_config_uses_user_provider_block(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """The happy path returns the configured ``GcpProviderConfig`` verbatim, silently.

    Pins the third leg of the three-case contract: configured GCP block ->
    return as-is, no warning. The two sibling tests cover the missing-block and
    non-GCP-block fallbacks (silent and warning respectively); pinning silence
    here too closes the {GCP / non-GCP / missing} x {warn / silent} matrix so a
    future regression that always-warns can't slip through.
    """
    user_config = GcpProviderConfig(
        backend=GCP_BACKEND_NAME,
        project_id="my-project",
        default_region="europe-west1",
        default_zone="europe-west1-b",
        network="custom-net",
        firewall_name="my-fw",
    )
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "gcp-prod", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "gcp-prod")

    assert resolved.project_id == "my-project"
    assert resolved.default_zone == "europe-west1-b"
    assert resolved.network == "custom-net"
    assert resolved.firewall_name == "my-fw"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When the named provider block doesn't exist, class defaults are used silently.

    Operator commands must work for first-run users who haven't yet pinned a
    ``[providers.gcp]`` block, so the fallback is a feature not a bug -- and no
    warning is emitted because this is the expected shape (distinct from the
    wrong-type case, which does warn).
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "gcp-does-not-exist")

    assert resolved == GcpProviderConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_when_named_block_is_non_gcp(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """If the user pointed ``[providers.gcp]`` at a non-GCP backend, fall back and warn.

    The operator CLI still works against the class defaults plus whatever the
    user passes on the command line; refusing here would block a legitimate
    out-of-band run. But the user's ``--provider`` selection did not have the
    intended effect, so a warning is emitted to make the silent-fallback visible
    (distinct from the missing-block case, which is silent because it is the
    expected first-run shape).
    """
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "gcp", LocalProviderConfig())

    resolved = _resolve_provider_config(ctx_with_provider, "gcp")

    assert resolved == GcpProviderConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'gcp'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
