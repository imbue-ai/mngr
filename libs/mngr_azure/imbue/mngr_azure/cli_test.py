import json
from pathlib import Path
from types import SimpleNamespace

import pluggy
import pytest
from azure.core.exceptions import HttpResponseError
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr_azure.backend import AZURE_BACKEND_NAME
from imbue.mngr_azure.cli import _ensure_state_bucket
from imbue.mngr_azure.cli import _output_cleanup_result
from imbue.mngr_azure.cli import _output_prepare_result
from imbue.mngr_azure.cli import _perform_cleanup
from imbue.mngr_azure.cli import _perform_host_identity_cleanup
from imbue.mngr_azure.cli import _perform_state_bucket_cleanup
from imbue.mngr_azure.cli import _provision_host_identity
from imbue.mngr_azure.cli import _refuse_cleanup_if_vms_exist
from imbue.mngr_azure.cli import _resolve_provider_config
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.client import AzureNetworkPrepareResult
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.errors import AzureProviderError
from imbue.mngr_azure.state_bucket import BlobStateHostIdentityError
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeManagedServiceIdentityClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import FakeTokenCredential
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_azure.testing import _StubbedBlobStateHostIdentity


def _stubbed_bucket(
    backend: FakeBlobStorageBackend, *, authorization: FakeAuthorizationClient | None = None
) -> _StubbedBlobStateBucket:
    return _StubbedBlobStateBucket(
        credential=FakeTokenCredential(),
        subscription_id="sub-123",
        resource_group="mngr",
        region="westus",
        account_name="mngrststateacct1234",
        fake_backend=backend,
        fake_authorization=authorization or FakeAuthorizationClient(),
    )


def test_ensure_state_bucket_creates_account_and_grants_operator() -> None:
    authorization = FakeAuthorizationClient()
    bucket = _stubbed_bucket(FakeBlobStorageBackend(), authorization=authorization)
    account_name, was_created = _ensure_state_bucket(bucket)
    assert account_name == "mngrststateacct1234"
    assert was_created is True
    # prepare also grants the operator's own principal data-plane blob access
    # (FakeTokenCredential's default token carries oid "operator-oid-1").
    assert len(authorization.role_assignments.created) == 1
    _scope, _name, parameters = authorization.role_assignments.created[0]
    assert parameters.principal_id == "operator-oid-1"


def test_perform_state_bucket_cleanup_deletes_when_empty() -> None:
    backend = FakeBlobStorageBackend()
    bucket = _stubbed_bucket(backend)
    bucket.ensure_bucket()
    assert _perform_state_bucket_cleanup(bucket, force=False) == "mngrststateacct1234"
    assert backend.deleted_account is True


def test_perform_state_bucket_cleanup_noop_when_account_absent() -> None:
    bucket = _stubbed_bucket(FakeBlobStorageBackend())
    assert _perform_state_bucket_cleanup(bucket, force=False) is None


def test_perform_state_bucket_cleanup_refuses_with_host_state() -> None:
    backend = FakeBlobStorageBackend()
    bucket = _stubbed_bucket(backend)
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    with pytest.raises(AzureProviderError, match="still holds offline host state"):
        _perform_state_bucket_cleanup(bucket, force=False)
    # Refusal deletes nothing.
    assert backend.deleted_account is False


def test_perform_state_bucket_cleanup_force_deletes_despite_host_state() -> None:
    """``--force`` deletes the account (and its leftover state) instead of refusing."""
    backend = FakeBlobStorageBackend()
    bucket = _stubbed_bucket(backend)
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    assert _perform_state_bucket_cleanup(bucket, force=True) == "mngrststateacct1234"
    assert backend.deleted_account is True


def _operator_client(
    *,
    compute: FakeComputeClient | None = None,
    resource: FakeResourceClient | None = None,
) -> _StubbedAzureVpsClient:
    return _StubbedAzureVpsClient(
        credential=object(),
        subscription_id="sub-123",
        region="westus",
        allowed_ssh_cidrs=("203.0.113.4/32",),
        stubbed_compute_client=compute or FakeComputeClient(),
        stubbed_network_client=FakeNetworkClient(),
        stubbed_resource_client=resource or FakeResourceClient(),
    )


def test_cleanup_logic_deletes_rg_when_no_vms() -> None:
    resource = FakeResourceClient()
    resource.resource_groups.get_result = SimpleNamespace(name="mngr", tags={"managed-by": "mngr"})
    client = _operator_client(resource=resource)
    assert _perform_cleanup(client) == "mngr"
    assert resource.resource_groups.deleted == ["mngr"]


def test_cleanup_logic_refuses_when_vms_exist() -> None:
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-a", tags={"managed-by": "mngr", "mngr-provider": "azure"}, instance_view=None)
    ]
    client = _operator_client(compute=compute)
    with pytest.raises(AzureProviderError, match="Refusing to clean up"):
        _perform_cleanup(client)


def test_cleanup_logic_noop_when_rg_missing() -> None:
    # Resource group get_result left None -> the fake raises 404 -> None returned.
    client = _operator_client()
    assert _perform_cleanup(client) is None


def test_refuse_cleanup_if_vms_exist_aborts_before_teardown() -> None:
    """The VM-exists refusal runs first, so the storage account is never torn down.

    Reproduces the callback ordering: when a VM is still alive, the guard raises
    before any bucket teardown, so a state account holding host state survives.
    """
    compute = FakeComputeClient()
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-live", tags={"managed-by": "mngr", "mngr-provider": "azure"}, instance_view=None)
    ]
    client = _operator_client(compute=compute)
    backend = FakeBlobStorageBackend()
    bucket = _stubbed_bucket(backend)
    bucket.ensure_bucket()
    bucket.write_host_record_json(HostId.generate(), "{}")
    with pytest.raises(AzureProviderError, match="Refusing to clean up"):
        _refuse_cleanup_if_vms_exist(client)
    # The guard raised before any teardown, so the account and its state survive.
    assert backend.deleted_account is False
    assert bucket.has_any_host_state() is True


def test_prepare_command_help_is_reachable() -> None:
    result = CliRunner().invoke(azure_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--allowed-ssh-cidr" in result.output
    assert "--resource-group" in result.output


def test_cleanup_command_help_is_reachable() -> None:
    result = CliRunner().invoke(azure_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--resource-group" in result.output


# =============================================================================
# Format-aware prepare / cleanup output (the --format surface)
# =============================================================================


def test_output_prepare_result_human_emits_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode emits one result sentence to stdout when the bucket setup is skipped."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=True)
    _output_prepare_result(result, None, False, None, OutputFormat.HUMAN)
    assert capsys.readouterr().out == "Prepared Azure resource group mngr in region westus\n"


def test_output_prepare_result_human_emits_bucket_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode emits a second line for the state storage account when it was set up."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=True)
    _output_prepare_result(result, "mngrstabc123", True, None, OutputFormat.HUMAN)
    out = capsys.readouterr().out
    assert "Prepared Azure resource group mngr in region westus\n" in out
    assert "Created Azure state storage account mngrstabc123 in region westus\n" in out


def test_output_prepare_result_json_carries_created_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON mode emits a structured object including the created + state-bucket signals."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=False)
    _output_prepare_result(result, "mngrstabc123", False, "mngrid-mngrstabc123", OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "resource_group": "mngr",
        "region": "westus",
        "created": False,
        "state_storage_account_name": "mngrstabc123",
        "state_bucket_created": False,
        "host_identity_name": "mngrid-mngrstabc123",
    }


def test_output_prepare_result_jsonl_emits_prepared_event(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL mode emits a ``prepared`` event with the same fields."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=True)
    _output_prepare_result(result, None, False, None, OutputFormat.JSONL)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "prepared"
    assert payload["created"] is True
    assert payload["resource_group"] == "mngr"


def test_output_cleanup_result_json_reports_deleted(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=True when a resource group was removed."""
    _output_cleanup_result("mngr", "sub-123", "westus", "mngrstabc123", "mngrid-mngrstabc123", OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "resource_group": "mngr",
        "subscription_id": "sub-123",
        "region": "westus",
        "deleted": True,
        "state_storage_account_deleted": "mngrstabc123",
        "host_identity_deleted": "mngrid-mngrstabc123",
    }


def test_output_cleanup_result_json_reports_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=False on the idempotent no-op path."""
    _output_cleanup_result(None, "sub-123", "westus", None, None, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["deleted"] is False
    assert payload["resource_group"] is None
    assert payload["state_storage_account_deleted"] is None


# =============================================================================
# host-dir identity provisioning (gated on is_offline_host_dir_enabled; raises on failure)
# =============================================================================


def _stubbed_identity(*, exists: bool = False) -> _StubbedBlobStateHostIdentity:
    msi = FakeManagedServiceIdentityClient()
    msi.user_assigned_identities.exists = exists
    return _StubbedBlobStateHostIdentity(
        credential=None,
        subscription_id="sub-123",
        resource_group="mngr",
        region="westus",
        account_name="mngrstabc123",
        fake_msi_client=msi,
        fake_authorization_client=FakeAuthorizationClient(),
    )


def test_provision_host_identity_creates_identity() -> None:
    identity = _stubbed_identity()
    assert _provision_host_identity(identity) == identity.identity_name
    assert identity.host_identity_exists() is True


def test_perform_host_identity_cleanup_deletes_then_is_idempotent() -> None:
    identity = _stubbed_identity()
    identity.ensure_host_identity()
    assert _perform_host_identity_cleanup(identity) == identity.identity_name
    # Now absent: a second cleanup is a no-op (returns None).
    assert _perform_host_identity_cleanup(identity) is None


def test_perform_host_identity_cleanup_noop_when_absent() -> None:
    assert _perform_host_identity_cleanup(_stubbed_identity(exists=False)) is None


def test_provision_host_identity_raises_on_msi_failure() -> None:
    """A managed-identity API failure during provisioning propagates (no warn-and-continue)."""
    identity = _stubbed_identity()
    identity.fake_msi_client.user_assigned_identities.create_error = HttpResponseError(message="forbidden")
    with pytest.raises(BlobStateHostIdentityError):
        _provision_host_identity(identity)


def test_perform_host_identity_cleanup_raises_on_delete_failure() -> None:
    """A delete failure surfaces so cleanup reports an incomplete teardown rather than swallowing it."""
    identity = _stubbed_identity()
    identity.ensure_host_identity()
    identity.fake_msi_client.user_assigned_identities.delete_error = HttpResponseError(message="forbidden")
    with pytest.raises(BlobStateHostIdentityError):
        _perform_host_identity_cleanup(identity)


def test_prepare_command_fails_clearly_without_subscription(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """When no subscription resolves, the click command surfaces a clean error.

    Passes ``obj=plugin_manager`` because ``prepare`` now runs through
    ``setup_command_context`` (so it can read ``[providers.NAME]`` from
    settings.toml as defaults), and ``setup_command_context`` reads the plugin
    manager off ``ctx.obj``.
    """
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    # Isolate AZURE_CONFIG_DIR (the conftest autouse fixture pins it at the real
    # ~/.azure) so no az-default subscription resolves -- otherwise prepare would
    # proceed and make real Azure calls in a unit test.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    result = cli_runner.invoke(
        azure_cli_group,
        ["prepare", "--allowed-ssh-cidr", "0.0.0.0/0"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "subscription" in result.output.lower()


# =============================================================================
# Provider-config resolution (the bug-fix surface for `mngr azure prepare`)
# =============================================================================
#
# Earlier versions of ``_build_operator_client`` built ``AzureProviderConfig``
# with only ``subscription_id``, using class defaults for the resource group /
# vnet / subnet / NSG names and region, so a user with non-default values in
# ``[providers.azure]`` running ``mngr azure prepare`` without the matching CLI
# flag would create infrastructure with different names than the runtime create
# path later resolved. ``_resolve_provider_config`` fixes this by reading the
# user's resolved provider config off the ``MngrContext``; these tests pin that
# behavior. Mirrors the AWS and GCP providers' identical fix.


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
    """The happy path returns the configured ``AzureProviderConfig`` verbatim, silently.

    Pins the third leg of the three-case contract: configured Azure block ->
    return as-is, no warning. The two sibling tests cover the missing-block and
    non-Azure-block fallbacks (silent and warning respectively); pinning silence
    here too closes the {Azure / non-Azure / missing} x {warn / silent} matrix so
    a future regression that always-warns can't slip through.
    """
    user_config = AzureProviderConfig(
        backend=AZURE_BACKEND_NAME,
        subscription_id="sub-abc",
        default_region="eastus",
        resource_group="my-rg",
        vnet_name="my-vnet",
        nsg_name="my-nsg",
    )
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "azure-prod", user_config)

    resolved = _resolve_provider_config(ctx_with_provider, "azure-prod")

    assert resolved.subscription_id == "sub-abc"
    assert resolved.default_region == "eastus"
    assert resolved.resource_group == "my-rg"
    assert resolved.vnet_name == "my-vnet"
    assert resolved.nsg_name == "my-nsg"
    assert log_warnings == [], f"happy path must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_to_class_defaults_when_missing(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """When the named provider block doesn't exist, class defaults are used silently.

    Operator commands must work for first-run users who haven't yet pinned a
    ``[providers.azure]`` block, so the fallback is a feature not a bug -- and no
    warning is emitted because this is the expected shape (distinct from the
    wrong-type case, which does warn).
    """
    resolved = _resolve_provider_config(temp_mngr_ctx, "azure-does-not-exist")

    assert resolved == AzureProviderConfig()
    assert log_warnings == [], f"missing-block fallback must be silent, got {log_warnings!r}"


def test_resolve_provider_config_falls_back_when_named_block_is_non_azure(
    temp_mngr_ctx: MngrContext,
    log_warnings: list[str],
) -> None:
    """If the user pointed ``[providers.azure]`` at a non-Azure backend, fall back and warn.

    The operator CLI still works against the class defaults plus whatever the
    user passes on the command line; refusing here would block a legitimate
    out-of-band run. But the user's ``--provider`` selection did not have the
    intended effect, so a warning is emitted to make the silent-fallback visible
    (distinct from the missing-block case, which is silent because it is the
    expected first-run shape).
    """
    ctx_with_provider = _temp_mngr_ctx_with_provider(temp_mngr_ctx, "azure", LocalProviderConfig())

    resolved = _resolve_provider_config(ctx_with_provider, "azure")

    assert resolved == AzureProviderConfig()
    assert len(log_warnings) == 1, f"expected exactly one warning, got {log_warnings!r}"
    assert "'azure'" in log_warnings[0]
    assert "LocalProviderConfig" in log_warnings[0]
