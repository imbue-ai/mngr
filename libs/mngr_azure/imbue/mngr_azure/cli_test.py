import json
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError
from click.testing import CliRunner

from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_azure.cli import _ensure_state_bucket
from imbue.mngr_azure.cli import _output_cleanup_result
from imbue.mngr_azure.cli import _output_prepare_result
from imbue.mngr_azure.cli import _perform_cleanup
from imbue.mngr_azure.cli import _perform_state_bucket_cleanup
from imbue.mngr_azure.cli import _refuse_cleanup_if_vms_exist
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.client import AzureNetworkPrepareResult
from imbue.mngr_azure.errors import AzureProviderError
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import FakeTokenCredential
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_vps.errors import ManagedResourcesExistError


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


def test_ensure_state_bucket_wraps_blob_grant_failure_with_actionable_guidance() -> None:
    """A failed operator blob-data grant surfaces an actionable error, not the bare Azure message.

    The grant needs ``Microsoft.Authorization/roleAssignments/write``, which an
    operator able to create storage may lack; the account is created first, so the
    error must tell the operator they can grant the role out of band and re-run.
    """
    authorization = FakeAuthorizationClient()
    authorization.role_assignments.create_error = HttpResponseError(message="AuthorizationFailed")
    bucket = _stubbed_bucket(FakeBlobStorageBackend(), authorization=authorization)
    with pytest.raises(AzureProviderError, match="roleAssignments/write"):
        _ensure_state_bucket(bucket)
    # The account was still created (the grant is the step that failed), so the
    # error message names it and points at re-running prepare.
    assert bucket.account_exists() is True


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
    with pytest.raises(ManagedResourcesExistError, match="Refusing to clean up"):
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
    with pytest.raises(ManagedResourcesExistError, match="Refusing to clean up"):
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
    _output_prepare_result(result, None, False, OutputFormat.HUMAN)
    assert capsys.readouterr().out == "Prepared Azure resource group mngr in region westus\n"


def test_output_prepare_result_human_emits_bucket_line(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode emits a second line for the state storage account when it was set up."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=True)
    _output_prepare_result(result, "mngrstabc123", True, OutputFormat.HUMAN)
    out = capsys.readouterr().out
    assert "Prepared Azure resource group mngr in region westus\n" in out
    assert "Created Azure state storage account mngrstabc123 in region westus\n" in out


def test_output_prepare_result_json_carries_created_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON mode emits a structured object including the created + state-bucket signals."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=False)
    _output_prepare_result(result, "mngrstabc123", False, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "resource_group": "mngr",
        "region": "westus",
        "created": False,
        "state_storage_account_name": "mngrstabc123",
        "state_bucket_created": False,
    }


def test_output_prepare_result_jsonl_emits_prepared_event(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL mode emits a ``prepared`` event with the same fields."""
    result = AzureNetworkPrepareResult(resource_group="mngr", region="westus", was_created=True)
    _output_prepare_result(result, None, False, OutputFormat.JSONL)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "prepared"
    assert payload["created"] is True
    assert payload["resource_group"] == "mngr"


def test_output_cleanup_result_json_reports_deleted(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=True when a resource group was removed."""
    _output_cleanup_result("mngr", "sub-123", "westus", "mngrstabc123", OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "resource_group": "mngr",
        "subscription_id": "sub-123",
        "region": "westus",
        "deleted": True,
        "state_storage_account_deleted": "mngrstabc123",
    }


def test_output_cleanup_result_json_reports_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON cleanup output reports deleted=False on the idempotent no-op path."""
    _output_cleanup_result(None, "sub-123", "westus", None, OutputFormat.JSON)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["deleted"] is False
    assert payload["resource_group"] is None
    assert payload["state_storage_account_deleted"] is None
