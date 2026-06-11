"""``mngr azure ...`` operator commands.

``mngr azure prepare`` does the one-time privileged setup: it registers the
Compute/Network/Storage resource providers and creates the mngr-owned resource
group + vnet + subnet + NSG (tagged ``managed-by=mngr``). After it succeeds, the
regular ``mngr create`` path runs with a restricted role -- it only resolves the
existing subnet (no network-write permission). Conventional split: an operator
runs prepare once; developers run create with limited credentials.

``mngr azure cleanup`` is the safe inverse of prepare: it deletes the mngr-owned
resource group (cascading the vnet/subnet/NSG). It refuses while any mngr-managed
VM still exists in the group, so it cannot strand a running agent, and it only
deletes a group it owns (tagged ``managed-by=mngr``).
"""

import click
from azure.core.exceptions import AzureError
from loguru import logger

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.errors import MngrError
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig


def _build_operator_client(
    subscription_id: str | None,
    region: str | None,
    resource_group: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> AzureVpsClient:
    """Construct an ``AzureVpsClient`` for the ``mngr azure`` operator commands.

    Bridges click options into the client constructor: each option falls back to
    the corresponding ``AzureProviderConfig`` default when not supplied. Pulled
    out of the click callbacks purely for readability. ``prepare`` passes the
    effective ingress CIDRs; ``cleanup`` passes ``()`` (it only deletes, never
    authorizes ingress, so the value is unused on that path).

    Raises ``ValueError`` (via ``get_subscription_id``) when no subscription is
    resolvable; the callbacks turn that into a clean ``click.ClickException``.
    """
    base = AzureProviderConfig(subscription_id=subscription_id or "")
    effective_subscription = base.get_subscription_id()
    return AzureVpsClient(
        credential=base.get_credential(),
        subscription_id=effective_subscription,
        region=region or base.default_region,
        resource_group=resource_group or base.resource_group,
        vnet_name=base.vnet_name,
        subnet_name=base.subnet_name,
        nsg_name=base.nsg_name,
        vnet_address_prefix=base.vnet_address_prefix,
        subnet_address_prefix=base.subnet_address_prefix,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        container_ssh_port=base.container_ssh_port,
    )


def _perform_cleanup(client: AzureVpsClient) -> str | None:
    """Core of ``mngr azure cleanup``: refuse if VMs exist, else delete the RG.

    Returns the deleted resource group name, or ``None`` when there was nothing
    to delete. Raises ``click.ClickException`` when any mngr-managed VM still
    exists, so cleanup never strands a running agent. Split from the click
    callback so the refuse/delete decision is unit-testable against a stubbed
    client, without the click runtime or real credentials.
    """
    vms = client.list_mngr_managed_vms()
    if vms:
        summary = ", ".join(str(vm["id"]) for vm in vms)
        raise click.ClickException(
            f"Refusing to clean up resource group {client.resource_group}: {len(vms)} mngr-managed "
            f"VM(s) still exist: {summary}. Destroy them first with `mngr destroy <agent>`, then "
            "re-run `mngr azure cleanup`."
        )
    return client.delete_managed_resource_group()


@click.group(name="azure")
def azure_cli_group() -> None:
    """Azure-provider operator commands (one-time setup, teardown)."""


@azure_cli_group.command(name="prepare")
@click.option(
    "--subscription-id",
    "subscription_id",
    default=None,
    help="Azure subscription ID. Defaults to the provider config / AZURE_SUBSCRIPTION_ID env var.",
)
@click.option(
    "--region",
    "region",
    default=None,
    help="Azure region. Defaults to the provider config's default_region (westus if unset).",
)
@click.option(
    "--resource-group",
    "resource_group",
    default=None,
    help="Resource group to create / reuse. Defaults to 'mngr'.",
)
@click.option(
    "--allowed-ssh-cidr",
    "allowed_ssh_cidrs",
    multiple=True,
    help=(
        "Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. "
        "Required (fail-closed): with none supplied, prepare refuses to create a wide-open NSG."
    ),
)
def prepare(
    subscription_id: str | None,
    region: str | None,
    resource_group: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> None:
    """Create (or reuse) the Azure resource group / vnet / subnet / NSG for mngr.

    Registers the Compute/Network/Storage resource providers, then creates the
    one-off infrastructure. Idempotent: re-running is a no-op when everything
    already exists. After this succeeds, ``mngr create --provider azure`` only
    needs VM/NIC/IP-create permissions (no network-management permissions).
    """
    try:
        client = _build_operator_client(subscription_id, region, resource_group, allowed_ssh_cidrs)
    except (ValueError, AzureError) as e:
        # ``ValueError`` covers the no-subscription case raised by
        # ``AzureProviderConfig.get_subscription_id``; ``AzureError`` covers
        # credential / SDK environment failures.
        raise click.ClickException(str(e)) from e
    try:
        resource_group_name = client.ensure_network()
    except MngrError as e:
        # Fail-closed: ensure_network raises when no --allowed-ssh-cidr was
        # supplied. Surface it as a clean CLI error rather than a traceback.
        raise click.ClickException(str(e)) from e
    logger.info("Prepared Azure resource group {} in region {}", resource_group_name, client.region)
    write_human_line(resource_group_name)


@azure_cli_group.command(name="cleanup")
@click.option(
    "--subscription-id",
    "subscription_id",
    default=None,
    help="Azure subscription ID. Defaults to the provider config / AZURE_SUBSCRIPTION_ID env var.",
)
@click.option(
    "--region",
    "region",
    default=None,
    help="Azure region. Defaults to the provider config's default_region (westus if unset).",
)
@click.option(
    "--resource-group",
    "resource_group",
    default=None,
    help="Resource group to delete. Defaults to 'mngr'.",
)
def cleanup(
    subscription_id: str | None,
    region: str | None,
    resource_group: str | None,
) -> None:
    """Undo `mngr azure prepare`: delete the mngr-owned resource group.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed VM still exists in the group -- destroy those first with
    `mngr destroy <agent>` so a running agent is never stranded. With no VMs
    present, deletes the resource group (cascading its vnet/subnet/NSG), but only
    when it is tagged `managed-by=mngr` (created by prepare). Idempotent: a no-op
    (exit 0) when the group is already gone.
    """
    try:
        client = _build_operator_client(subscription_id, region, resource_group, ())
    except (ValueError, AzureError) as e:
        raise click.ClickException(str(e)) from e

    try:
        deleted_resource_group = _perform_cleanup(client)
    except MngrError as e:
        # delete_managed_resource_group raises when the group lacks the
        # managed-by=mngr tag. Surface as a clean CLI error.
        raise click.ClickException(str(e)) from e
    if deleted_resource_group is None:
        write_human_line(
            f"Nothing to clean up: no mngr-owned resource group in subscription {client.subscription_id}."
        )
    else:
        logger.info("Cleaned up Azure resource group {} in region {}", deleted_resource_group, client.region)
        write_human_line(deleted_resource_group)
