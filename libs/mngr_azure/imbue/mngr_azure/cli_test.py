from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from imbue.mngr_azure.cli import _perform_cleanup
from imbue.mngr_azure.cli import azure_cli_group
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient


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
    compute.virtual_machines.list_result = [SimpleNamespace(name="vm-a", tags={"mngr-provider": "azure"})]
    client = _operator_client(compute=compute)
    with pytest.raises(click.ClickException, match="Refusing to clean up"):
        _perform_cleanup(client)


def test_cleanup_logic_noop_when_rg_missing() -> None:
    # Resource group get_result left None -> the fake raises 404 -> None returned.
    client = _operator_client()
    assert _perform_cleanup(client) is None


def test_prepare_command_help_is_reachable() -> None:
    result = CliRunner().invoke(azure_cli_group, ["prepare", "--help"])
    assert result.exit_code == 0
    assert "--allowed-ssh-cidr" in result.output
    assert "--resource-group" in result.output


def test_cleanup_command_help_is_reachable() -> None:
    result = CliRunner().invoke(azure_cli_group, ["cleanup", "--help"])
    assert result.exit_code == 0
    assert "--resource-group" in result.output


def test_prepare_command_fails_clearly_without_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    result = CliRunner().invoke(azure_cli_group, ["prepare", "--allowed-ssh-cidr", "0.0.0.0/0"])
    assert result.exit_code != 0
    assert "subscription_id" in result.output
