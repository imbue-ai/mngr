"""``mngr azure ...`` operator commands.

``mngr azure prepare`` does the one-time privileged setup: it registers the
Compute/Network/Storage resource providers and creates the mngr-owned resource
group + vnet + subnet + NSG (tagged ``managed-by=mngr``). After it succeeds, the
regular ``mngr create`` path needs only VM/NIC/IP-create permissions, not the
network-management permissions that build the vnet/subnet/NSG -- it just resolves
(does not create) the existing subnet. Conventional split: an operator runs
prepare once; developers run create with limited credentials.

``mngr azure cleanup`` is the safe inverse of prepare: it deletes the mngr-owned
resource group (cascading the vnet/subnet/NSG). It refuses while any mngr-managed
VM still exists in the group, so it cannot strand a running agent, and it only
deletes a group it owns (tagged ``managed-by=mngr``).

Both commands read defaults from the user's ``[providers.<name>]`` settings.toml
block (selected with ``--provider``) so the resource group / vnet / subnet / NSG
land with the same names the runtime ``mngr create`` path will use; CLI options
override the resolved config, which in turn overrides class defaults.
"""

from typing import Any
from typing import assert_never

import click
from azure.core.exceptions import AzureError
from click_option_group import optgroup
from loguru import logger

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.client import AzureNetworkPrepareResult
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.errors import AzureProviderError


class _AzureOperatorCliOptions(CommonCliOptions):
    """Shared option shape for ``mngr azure prepare`` and ``mngr azure cleanup``.

    Both commands select a provider block (``--provider``) and can override the
    subscription / region / resource group the operation targets. ``prepare``
    additionally accepts ``--allowed-ssh-cidr`` (the NSG's ingress); ``cleanup``
    only deletes the resource group, so it needs none of that.
    """

    provider: str
    subscription_id: str | None
    region: str | None
    resource_group: str | None


class _AzurePrepareCliOptions(_AzureOperatorCliOptions):
    allowed_ssh_cidrs: tuple[str, ...]


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> AzureProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to create / delete the resource group, vnet,
    subnet, and NSG using the same names the runtime ``mngr create --provider
    <provider_name>`` path will later resolve. Class defaults
    (``AzureProviderConfig()``) are a fallback for the first-run case where the
    user has not yet pinned a provider block; if their settings.toml *does*
    configure the named provider, we honor it.

    When the looked-up config is not an ``AzureProviderConfig`` (e.g. the user
    pointed ``[providers.azure]`` at a non-Azure backend), fall back to class
    defaults rather than erroring -- the operator command's CLI options
    (``--subscription-id`` / ``--region`` / ``--resource-group``) can still drive
    an Azure-targeted run. A warning is emitted in this case so the user notices
    their ``--provider`` selection did not have the intended effect (a silent
    fallback to class defaults would otherwise create the resource group with the
    default name in the default region with no visible signal). The missing-block
    case is silent because that is the expected first-run shape. Mirrors
    ``mngr_aws.cli._resolve_provider_config``.
    """
    config = mngr_ctx.config.providers.get(ProviderInstanceName(provider_name))
    if isinstance(config, AzureProviderConfig):
        return config
    if config is not None:
        logger.warning(
            "Provider {!r} is configured but is not an Azure backend (got {}); "
            "falling back to AzureProviderConfig class defaults. Pass "
            "--subscription-id / --region / --resource-group to override, or point "
            "--provider at an Azure-backed block.",
            provider_name,
            type(config).__name__,
        )
    return AzureProviderConfig()


def _build_operator_client(
    base: AzureProviderConfig,
    subscription_id: str | None,
    region: str | None,
    resource_group: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> AzureVpsClient:
    """Construct an ``AzureVpsClient`` for the ``mngr azure`` operator commands.

    Bridges click options into the client constructor: each option falls back to
    the matching field on ``base`` (the user's resolved ``AzureProviderConfig``
    for the selected provider, or class defaults when none is pinned) so the
    resource group / vnet / subnet / NSG land with the names the runtime create
    path will use. ``prepare`` passes the effective ingress CIDRs; ``cleanup``
    passes ``()`` (it only deletes, never authorizes ingress, so the value is
    unused on that path).

    ``subscription_id`` precedence is explicit ``--subscription-id`` > the
    resolved config's ``subscription_id`` > the ``AZURE_SUBSCRIPTION_ID`` env var
    > the active ``az`` subscription (the latter three resolved by
    ``base.get_subscription_id``). Raises ``AzureSubscriptionError`` (an
    ``AzureProviderError`` / ``MngrError``) when none resolves; it propagates from
    the callbacks with its specific type and renders as a clean CLI message.
    """
    effective_subscription = subscription_id or base.get_subscription_id()
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
    to delete. Raises ``AzureProviderError`` (an ``MngrError``, so it renders as a
    clean CLI message) when any mngr-managed VM still exists, so cleanup never
    strands a running agent. Split from the click callback so the refuse/delete
    decision is unit-testable against a stubbed client, without the click runtime
    or real credentials.
    """
    vms = client.list_mngr_managed_vms()
    if vms:
        summary = ", ".join(str(vm["id"]) for vm in vms)
        raise AzureProviderError(
            f"Refusing to clean up resource group {client.resource_group}: {len(vms)} mngr-managed "
            f"VM(s) still exist: {summary}. Destroy them first with `mngr destroy <agent>`, then "
            "re-run `mngr azure cleanup`."
        )
    return client.delete_managed_resource_group()


def _output_prepare_result(result: AzureNetworkPrepareResult, output_format: OutputFormat) -> None:
    """Emit the result of ``mngr azure prepare`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` so a caller can
    tell a first-run create from an idempotent no-op. Mirrors ``mngr gcp prepare``.
    """
    data = {
        "resource_group": result.resource_group,
        "region": result.region,
        "created": result.was_created,
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event("prepared", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Prepared Azure resource group {} in region {}", result.resource_group, result.region)
        case _ as unreachable:
            assert_never(unreachable)


def _output_cleanup_result(
    deleted_resource_group: str | None,
    subscription_id: str,
    region: str,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr azure cleanup`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the resource group was
    already absent (idempotent no-op). Mirrors ``mngr gcp cleanup``.
    """
    data = {
        "resource_group": deleted_resource_group,
        "subscription_id": subscription_id,
        "region": region,
        "deleted": deleted_resource_group is not None,
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event("cleaned_up", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if deleted_resource_group is None:
                write_human_line(
                    "Nothing to clean up: no mngr-owned resource group in subscription {}.", subscription_id
                )
            else:
                write_human_line("Cleaned up Azure resource group {} in region {}", deleted_resource_group, region)
        case _ as unreachable:
            assert_never(unreachable)


@click.group(name="azure")
def azure_cli_group() -> None:
    """Azure-provider operator commands (one-time setup, teardown)."""


@azure_cli_group.command(name="prepare")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="azure",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(subscription_id, default_region, resource_group, vnet/subnet/nsg names, "
        "allowed_ssh_cidrs). When the block does not exist, AzureProviderConfig class "
        "defaults are used as the fallback. CLI options below override either source."
    ),
)
@optgroup.option(
    "--subscription-id",
    "subscription_id",
    default=None,
    help="Azure subscription ID. Defaults to the resolved provider config, then AZURE_SUBSCRIPTION_ID, then your active `az` subscription.",
)
@optgroup.option(
    "--region",
    "region",
    default=None,
    help="Azure region. Defaults to the resolved provider config's default_region (westus if unset).",
)
@optgroup.option(
    "--resource-group",
    "resource_group",
    default=None,
    help="Resource group to create / reuse. Defaults to the resolved provider config's resource_group.",
)
@optgroup.option(
    "--allowed-ssh-cidr",
    "allowed_ssh_cidrs",
    multiple=True,
    help=(
        "Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. "
        "Defaults to the resolved provider config's allowed_ssh_cidrs ('0.0.0.0/0'). Tighten for production."
    ),
)
@add_common_options
@click.pass_context
def prepare(ctx: click.Context, **_kwargs: Any) -> None:
    """Create (or reuse) the Azure resource group / vnet / subnet / NSG for mngr.

    Registers the Compute/Network/Storage resource providers, then creates the
    one-off infrastructure. Idempotent: re-running is a no-op when everything
    already exists. After this succeeds, ``mngr create --provider azure`` only
    needs VM/NIC/IP-create permissions (no network-management permissions).
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="azure prepare",
        command_class=_AzurePrepareCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    # Empty tuple => no --allowed-ssh-cidr flags passed: fall back to the
    # resolved provider config's value. Non-empty tuple => caller supplied
    # explicit values, use them verbatim. Mirrors ``mngr aws prepare``.
    effective_cidrs = opts.allowed_ssh_cidrs or base.allowed_ssh_cidrs
    try:
        client = _build_operator_client(base, opts.subscription_id, opts.region, opts.resource_group, effective_cidrs)
    except AzureError as e:
        # ``AzureError`` (the azure SDK base) is not an ``MngrError``, so wrap it
        # into one for a clean CLI message. The no-subscription case raised by
        # ``_build_operator_client`` (``AzureSubscriptionError``) is already an
        # ``AzureProviderError`` (``MngrError``) subclass, so it renders cleanly
        # and is left to propagate with its specific type intact rather than being
        # flattened.
        raise AzureProviderError(str(e)) from e
    # ensure_network's network writes go through _translate_azure_errors, which
    # raises VpsApiError (a MngrError, so a clean CLI message) on Azure API
    # failures (quota, auth, conflicting NSG, etc.); let it propagate with its
    # specific type. (allowed_ssh_cidrs is fail-open, so an empty list no longer
    # raises here -- it creates a no-ingress NSG with a warning.)
    result = client.ensure_network()
    # Best-effort: create the least-privilege custom role that lets each VM's
    # managed identity deallocate itself on idle (true cost parity). Returns None
    # (after a clear warning) when the operator lacks roleDefinitions/write -- idle
    # self-deallocate is then disabled but `mngr stop`/`start` still work, so this
    # never fails prepare.
    client.ensure_self_deallocate_role()
    _output_prepare_result(result, output_opts.output_format)


@azure_cli_group.command(name="cleanup")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="azure",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(subscription_id, default_region, resource_group). When the block does not "
        "exist, AzureProviderConfig class defaults are used as the fallback."
    ),
)
@optgroup.option(
    "--subscription-id",
    "subscription_id",
    default=None,
    help="Azure subscription ID. Defaults to the resolved provider config, then AZURE_SUBSCRIPTION_ID, then your active `az` subscription.",
)
@optgroup.option(
    "--region",
    "region",
    default=None,
    help="Azure region. Defaults to the resolved provider config's default_region (westus if unset).",
)
@optgroup.option(
    "--resource-group",
    "resource_group",
    default=None,
    help="Resource group to delete. Defaults to the resolved provider config's resource_group.",
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **_kwargs: Any) -> None:
    """Undo `mngr azure prepare`: delete the mngr-owned resource group.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed VM still exists in the group -- destroy those first with
    `mngr destroy <agent>` so a running agent is never stranded. With no VMs
    present, deletes the resource group (cascading its vnet/subnet/NSG), but only
    when it is tagged `managed-by=mngr` (created by prepare). Idempotent: a no-op
    (exit 0) when the group is already gone.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="azure cleanup",
        command_class=_AzureOperatorCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    try:
        client = _build_operator_client(base, opts.subscription_id, opts.region, opts.resource_group, ())
    except AzureError as e:
        # Same credential wrapping as the prepare path; the AzureSubscriptionError
        # (an MngrError) propagates with its specific type.
        raise AzureProviderError(str(e)) from e

    # _perform_cleanup raises AzureProviderError when a VM still exists, and
    # delete_managed_resource_group raises VpsApiError when the group lacks the
    # managed-by=mngr tag. Both are MngrErrors, so they render as clean CLI
    # messages; let them propagate with their specific type.
    deleted_resource_group = _perform_cleanup(client)
    _output_cleanup_result(deleted_resource_group, client.subscription_id, client.region, output_opts.output_format)
