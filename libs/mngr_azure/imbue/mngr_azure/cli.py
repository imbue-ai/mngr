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

import click
from azure.core.exceptions import AzureError
from click_option_group import optgroup

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_azure.client import AzureNetworkPrepareResult
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.errors import AzureProviderError
from imbue.mngr_azure.state_bucket import BlobStateBucket
from imbue.mngr_azure.state_bucket import BlobStateBucketError
from imbue.mngr_vps.cli_helpers import refuse_if_managed_resources_exist
from imbue.mngr_vps.cli_helpers import resolve_provider_config


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


class _AzureCleanupCliOptions(_AzureOperatorCliOptions):
    force: bool


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> AzureProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to create / delete the resource group, vnet,
    subnet, and NSG using the same names the runtime ``mngr create --provider
    <provider_name>`` path will later resolve. Thin wrapper over the shared
    ``resolve_provider_config`` (see it for the {configured / wrong-backend /
    missing} contract).
    """
    return resolve_provider_config(
        mngr_ctx,
        provider_name,
        config_cls=AzureProviderConfig,
        default_factory=AzureProviderConfig,
        cloud_label="an Azure backend",
        override_hint=(
            "Pass --subscription-id / --region / --resource-group to override, or "
            "point --provider at an Azure-backed block."
        ),
    )


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


def _refuse_cleanup_if_vms_exist(client: AzureVpsClient) -> None:
    """Raise ``ManagedResourcesExistError`` when any mngr-managed VM still exists.

    Run first by ``mngr azure cleanup``, before any teardown, so a still-running
    VM aborts the whole cleanup (storage account + identity + RG) and strands
    nothing. Split out so the refusal is unit-testable against a stubbed client.
    """
    vms = client.list_mngr_managed_vms()
    refuse_if_managed_resources_exist(
        [str(vm["id"]) for vm in vms],
        summary=", ".join(str(vm["id"]) for vm in vms),
        resource_noun="VM",
        scope_description=f"resource group {client.resource_group}",
        cleanup_command="mngr azure cleanup",
    )


def _perform_cleanup(client: AzureVpsClient) -> str | None:
    """Core of ``mngr azure cleanup``: refuse if VMs exist, else delete the RG.

    Returns the deleted resource group name, or ``None`` when there was nothing
    to delete. Raises ``ManagedResourcesExistError`` (an ``MngrError``, so it
    renders as a clean CLI message) when any mngr-managed VM still exists, so
    cleanup never strands a running agent. Split from the click callback so the
    refuse/delete decision is unit-testable against a stubbed client, without the
    click runtime or real credentials.
    """
    _refuse_cleanup_if_vms_exist(client)
    return client.delete_managed_resource_group()


def _build_state_bucket(base: AzureProviderConfig, client: AzureVpsClient) -> BlobStateBucket:
    """Build the Blob state bucket for the operator commands from the resolved subscription.

    The storage-account name is always derivable (``mngrst<hash>`` from the
    subscription + resource group), so this never returns None -- unlike AWS,
    which needs an STS call to learn the account id. The bucket's region /
    resource group track the operator client's, so it lands alongside the network.
    """
    return BlobStateBucket(
        credential=base.get_credential(),
        subscription_id=client.subscription_id,
        resource_group=client.resource_group,
        region=client.region,
        account_name=base.resolve_state_storage_account_name(client.subscription_id),
    )


def _ensure_state_bucket(bucket: BlobStateBucket) -> tuple[str, bool]:
    """Create (idempotently) the required state account + container, returning ``(account_name, was_created)``.

    The bucket is required infrastructure, so this is the primary job of ``mngr
    azure prepare``: a missing-permission / API failure raises (a network-only
    prepare would be misleading -- a stopped host could not be listed or resumed).
    Also grants the operator's own principal data-plane blob access on the account:
    Azure control-plane roles (the account creator) do NOT include blob data
    access, so without this the operator's offline reads/writes would fail with
    AuthorizationPermissionMismatch.

    The blob-data grant needs ``Microsoft.Authorization/roleAssignments/write``,
    which an operator who can create storage may legitimately lack. When the grant
    fails, the account already exists (it is created first), so the error is
    re-raised with actionable guidance: the role can be granted out of band and
    ``prepare`` re-run -- rather than leaving the operator with only the low-level
    ``AuthorizationFailed`` message.
    """
    was_created = bucket.ensure_bucket()
    try:
        bucket.ensure_operator_blob_access()
    except BlobStateBucketError as e:
        raise AzureProviderError(
            f"Created the Azure state storage account {bucket.account_name!r}, but could not grant the "
            "operator principal data-plane blob access (Storage Blob Data Contributor). This needs the "
            "`Microsoft.Authorization/roleAssignments/write` permission. Grant the role out of band (or "
            "re-run `mngr azure prepare` as a principal that can assign roles) -- without it, offline "
            f"host state reads/writes fail with AuthorizationPermissionMismatch. Underlying error: {e}"
        ) from e
    return bucket.account_name, was_created


def _perform_state_bucket_cleanup(bucket: BlobStateBucket, *, force: bool) -> str | None:
    """Delete the state storage account, refusing while any managed-host state remains.

    Returns the deleted account name, or ``None`` when none existed. Unless
    ``force`` is set, raises ``AzureProviderError`` when the account still
    holds ``hosts/`` state. By the time this runs the VM-exists check has already
    passed, so any remaining state is *orphaned* offline state (a host whose VM
    is gone but whose ``delete_host_state`` never ran, or one deleted outside
    mngr) -- deleting it silently could drop offline records the operator still
    wants, so we refuse and let ``--force`` opt into deleting it. Split out
    so the refuse/delete decision is unit-testable.
    """
    if not bucket.account_exists():
        return None
    if not force and bucket.container_exists() and bucket.has_any_host_state():
        raise AzureProviderError(
            f"Refusing to delete Azure state storage account {bucket.account_name!r}: it still holds offline "
            "host state (from hosts that are no longer running VMs). Re-run with `--force` to delete "
            "the account and the remaining state."
        )
    bucket.delete_bucket()
    return bucket.account_name


def _output_prepare_result(
    result: AzureNetworkPrepareResult,
    state_account_name: str | None,
    was_bucket_created: bool,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr azure prepare`` in the requested format.

    HUMAN: one (or two) result lines to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` (network) and
    ``state_storage_account_name`` / ``state_bucket_created``. Mirrors
    ``mngr aws prepare``.
    """
    account_verb = "Created" if was_bucket_created else "Reused existing"
    emit_operator_result(
        "prepared",
        [
            OperatorResultPart.shown(
                f"Prepared Azure resource group {result.resource_group} in region {result.region}",
                resource_group=result.resource_group,
                region=result.region,
                created=result.was_created,
            ),
            OperatorResultPart.shown_if(
                state_account_name,
                f"{account_verb} Azure state storage account {state_account_name} in region {result.region}",
                state_storage_account_name=state_account_name,
                state_bucket_created=was_bucket_created,
            ),
        ],
        output_format,
    )


def _output_cleanup_result(
    deleted_resource_group: str | None,
    subscription_id: str,
    region: str,
    deleted_account_name: str | None,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr azure cleanup`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the resource group was already
    absent; ``state_storage_account_deleted`` carries the deleted account name (or
    None when none existed). Mirrors ``mngr aws cleanup``.
    """
    emit_operator_result(
        "cleaned_up",
        [
            OperatorResultPart.shown(
                f"Cleaned up Azure resource group {deleted_resource_group} in region {region}"
                if deleted_resource_group is not None
                else f"Nothing to clean up: no mngr-owned resource group in subscription {subscription_id}.",
                resource_group=deleted_resource_group,
                subscription_id=subscription_id,
                region=region,
                deleted=deleted_resource_group is not None,
            ),
            OperatorResultPart.shown_if(
                deleted_account_name,
                f"Deleted Azure state storage account {deleted_account_name} in region {region}",
                state_storage_account_deleted=deleted_account_name,
            ),
        ],
        output_format,
    )


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
    # Required: create the state storage account + container that hold the full
    # host record and per-agent records (so a deallocated VM's state is readable
    # offline). A missing storage permission or API failure raises (the bucket is
    # prepare's primary job; a network-only prepare would leave offline host state
    # unavailable).
    state_account_name, was_bucket_created = _ensure_state_bucket(_build_state_bucket(base, client))
    # Offline host_dir needs no managed identity: it is captured operator-side at
    # `mngr stop` (mngr reads host_dir off the box and uploads it with the
    # operator's own creds), so prepare sets up only the network + state account.
    _output_prepare_result(result, state_account_name, was_bucket_created, output_opts.output_format)


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
@optgroup.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Also delete the state storage account when it still holds offline host state left over from "
        "hosts that no longer exist as VMs (otherwise cleanup refuses to delete a non-empty account)."
    ),
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
        command_class=_AzureCleanupCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    try:
        client = _build_operator_client(base, opts.subscription_id, opts.region, opts.resource_group, ())
    except AzureError as e:
        # Same credential wrapping as the prepare path; the AzureSubscriptionError
        # (an MngrError) propagates with its specific type.
        raise AzureProviderError(str(e)) from e

    # Refuse the whole cleanup (delete nothing) while any mngr-managed VM still
    # exists, BEFORE tearing down its storage account / identity / role
    # assignment -- a running VM must abort cleanup so its offline state and write
    # identity are never stranded.
    _refuse_cleanup_if_vms_exist(client)
    # No VMs remain: tear down the storage account while it holds no host state
    # (its own refusal mirrors the VM check, as defense in depth). The storage
    # account lives in the resource group, so it is deleted first (its own
    # delete, before the RG cascade).
    deleted_account_name = _perform_state_bucket_cleanup(_build_state_bucket(base, client), force=opts.force)
    # _perform_cleanup raises ManagedResourcesExistError when a VM still exists, and
    # delete_managed_resource_group raises VpsApiError when the group lacks the
    # managed-by=mngr tag. Both are MngrErrors, so they render as clean CLI
    # messages; let them propagate with their specific type.
    deleted_resource_group = _perform_cleanup(client)
    _output_cleanup_result(
        deleted_resource_group,
        client.subscription_id,
        client.region,
        deleted_account_name,
        output_opts.output_format,
    )
