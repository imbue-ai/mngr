"""``mngr gcp ...`` operator commands.

``mngr gcp prepare`` does the one-time privileged setup (network-scoped,
tag-targeted firewall rule creation) so the regular ``mngr create`` path can
run with restricted IAM (instance create/get/list, no
``compute.firewalls.create``). Conventional split: an admin runs prepare once;
developers run create with limited credentials. Mirrors ``mngr aws prepare``.

``mngr gcp cleanup`` is the inverse of prepare: it deletes the mngr-managed
firewall rule so a project returns to its pre-prepare state. It refuses while
any mngr-managed instance still exists in the project, so it cannot strand a
running agent's SSH access. Mirrors ``mngr aws cleanup``.

Both commands read defaults from the user's ``[providers.<name>]`` settings.toml
block (selected with ``--provider``) so the firewall rule lands in the same
project / network / zone the runtime ``mngr create`` path will use; CLI options
override the resolved config, which in turn overrides class defaults.
"""

from typing import Any

import click
from click_option_group import optgroup
from google.auth import exceptions as google_auth_exceptions
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp.client import FirewallPrepareResult
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.errors import GcpCredentialsError
from imbue.mngr_gcp.errors import GcpError
from imbue.mngr_gcp.errors import GcpProjectError


class _GcpOperatorCliOptions(CommonCliOptions):
    """Shared option shape for ``mngr gcp prepare`` and ``mngr gcp cleanup``.

    Both commands select a provider block (``--provider``) and can override the
    project / firewall name / network the operation targets. ``prepare``
    additionally accepts ``--zone`` (the client is zone-bound),
    ``--firewall-target-tag``, and ``--allowed-ssh-cidr`` (the rule's ingress);
    ``cleanup`` only deletes, so it needs none of those.
    """

    provider: str
    project_id: str | None
    firewall_name: str | None
    network: str | None


class _GcpPrepareCliOptions(_GcpOperatorCliOptions):
    zone: str | None
    firewall_target_tag: str | None
    allowed_ssh_cidrs: tuple[str, ...]


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> GcpProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to land their firewall rule in the same project,
    network, and zone the runtime ``mngr create --provider <provider_name>`` path
    will later use. Class defaults (``GcpProviderConfig()``) are a fallback for
    the first-run case where the user has not yet pinned a provider block; if
    their settings.toml *does* configure the named provider, we honor it.

    When the looked-up config is not a ``GcpProviderConfig`` (e.g. the user
    pointed ``[providers.gcp]`` at a non-GCP backend), fall back to class
    defaults rather than erroring -- the operator command's CLI options
    (``--project`` / ``--zone`` / ``--network`` / ``--firewall-name``) can still
    drive a GCP-targeted run. A warning is emitted in this case so the user
    notices their ``--provider`` selection did not have the intended effect (a
    silent fallback to class defaults would otherwise land the rule in the
    default zone / network with no visible signal). The missing-block case is
    silent because that is the expected first-run shape. Mirrors
    ``mngr_aws.cli._resolve_provider_config``.
    """
    config = mngr_ctx.config.providers.get(ProviderInstanceName(provider_name))
    if isinstance(config, GcpProviderConfig):
        return config
    if config is not None:
        logger.warning(
            "Provider {!r} is configured but is not a GCP backend (got {}); "
            "falling back to GcpProviderConfig class defaults. Pass "
            "--project / --zone / --network / --firewall-name to override, or point "
            "--provider at a GCP-backed block.",
            provider_name,
            type(config).__name__,
        )
    return GcpProviderConfig()


def _build_operator_client(
    base: GcpProviderConfig,
    project_id: str | None,
    zone: str | None,
    firewall_name: str | None,
    firewall_target_tag: str | None,
    network: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
    concurrency_group: ConcurrencyGroup,
) -> GcpVpsClient:
    """Construct a ``GcpVpsClient`` for the ``mngr gcp`` operator commands.

    Bridges click options into the client constructor: each option falls back to
    the matching field on ``base`` (the user's resolved ``GcpProviderConfig`` for
    the selected provider, or class defaults when none is pinned) so the firewall
    rule lands in the project / network / zone the runtime create path will use.

    ``project_id`` precedence is explicit ``--project`` > the resolved config's
    ``project_id`` > the project ADC resolved from the environment (the active
    ``gcloud config set project`` / ``GOOGLE_CLOUD_PROJECT``). When none of those
    resolve a project, a ``GcpProjectError`` is raised here, before the client is
    constructed, so the client always holds a real project (no empty-string
    placeholder threaded through every API call). ``image`` is left at its
    ``None`` default: the operator commands only touch firewall rules and never
    call ``create_instance`` (the sole consumer of ``image``). ``prepare`` passes
    the effective ingress CIDRs; ``cleanup`` passes ``()`` (it only deletes,
    never authorizes ingress, so the value is unused there).

    The zone is resolved with the same precedence as the runtime create path:
    explicit ``--zone`` > the config's ``default_zone`` > the active ``gcloud
    config get compute/zone`` (best-effort, via ``concurrency_group``) >
    ``DEFAULT_GCE_ZONE`` -- so the operator client binds to the same zone a
    later ``mngr create`` would use.
    """
    credentials, adc_project = base.get_credentials_and_resolved_project()
    resolved_project = project_id or base.project_id or adc_project
    if not resolved_project:
        raise GcpProjectError(
            "No GCP project resolved. Pass --project, set project_id on the selected "
            "[providers.<name>] block (or run 'mngr config set providers.gcp.project_id "
            "<your-project>'), set the GOOGLE_CLOUD_PROJECT environment variable, or run "
            "'gcloud config set project <your-project>' (the active gcloud project is used "
            "automatically when Application Default Credentials are present)."
        )
    if zone:
        resolved_zone = zone
    else:
        gcloud_zone = None if base.default_zone else get_gcloud_compute_zone(concurrency_group)
        resolved_zone, _region = base.resolve_zone_and_region(gcloud_zone)
    return GcpVpsClient(
        credentials=credentials,
        project_id=resolved_project,
        zone=resolved_zone,
        network=network or base.network,
        firewall_name=firewall_name or base.firewall_name,
        firewall_target_tag=firewall_target_tag or base.firewall_target_tag,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        container_ssh_port=base.container_ssh_port,
    )


def _perform_cleanup(client: GcpVpsClient) -> str | None:
    """Core of ``mngr gcp cleanup``: refuse if instances exist, else delete the rule.

    Returns the deleted firewall rule name, or ``None`` when there was nothing to
    delete. Raises ``GcpError`` when any mngr-managed instance still exists in the
    project, so cleanup never strands a running agent's SSH access. Split from the
    click callback so the refuse/delete decision is unit-testable against a
    stubbed client, without the click runtime or real credentials.
    """
    instances = client.list_mngr_managed_instances()
    if instances:
        summary = ", ".join(f"{i['id']} ({i['state']} in {i['zone']})" for i in instances)
        raise GcpError(
            f"Refusing to clean up project {client.project_id}: {len(instances)} mngr-managed "
            f"instance(s) still exist: {summary}. Destroy them first with `mngr destroy <agent>` "
            "(or delete them), then re-run `mngr gcp cleanup`."
        )
    return client.delete_firewall()


def _output_prepare_result(
    result: FirewallPrepareResult,
    firewall_name: str,
    project_id: str,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr gcp prepare`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` so a caller can
    tell a first-run create from an idempotent no-op.
    """
    data = {
        "firewall_name": firewall_name,
        "target_tag": result.target_tag,
        "project_id": project_id,
        "created": result.was_created,
    }

    def _human() -> None:
        write_human_line(
            "Prepared GCP firewall rule {} (tag {}) in project {}",
            firewall_name,
            result.target_tag,
            project_id,
        )

    emit_operator_result("prepared", data, output_format, _human)


def _output_cleanup_result(
    deleted_firewall: str | None,
    firewall_name: str,
    project_id: str,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr gcp cleanup`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the rule was already absent
    (idempotent no-op).
    """
    data = {
        "firewall_name": firewall_name,
        "project_id": project_id,
        "deleted": deleted_firewall is not None,
    }

    def _human() -> None:
        if deleted_firewall is None:
            write_human_line("Nothing to clean up: no firewall rule {} in project {}.", firewall_name, project_id)
        else:
            write_human_line("Cleaned up GCP firewall rule {} in project {}", deleted_firewall, project_id)

    emit_operator_result("cleaned_up", data, output_format, _human)


@click.group(name="gcp")
def gcp_cli_group() -> None:
    """GCP-provider operator commands (one-time setup)."""


@gcp_cli_group.command(name="prepare")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="gcp",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(project_id, default_zone, network, firewall_name, firewall_target_tag, "
        "allowed_ssh_cidrs). When the block does not exist, GcpProviderConfig class "
        "defaults are used as the fallback. CLI options below override either source."
    ),
)
@optgroup.option(
    "--project",
    "project_id",
    default=None,
    help="GCP project ID. Defaults to the resolved provider config's project_id (or the gcloud/ADC default).",
)
@optgroup.option(
    "--zone",
    "zone",
    default=None,
    help=(
        "GCE zone for the client. Firewall rules are global, but the client is zone-bound; "
        "defaults to the resolved provider config's default_zone."
    ),
)
@optgroup.option(
    "--firewall-name",
    "firewall_name",
    default=None,
    help="Firewall rule name to create / reuse. Defaults to the resolved provider config's firewall_name.",
)
@optgroup.option(
    "--firewall-target-tag",
    "firewall_target_tag",
    default=None,
    help=(
        "Network tag the rule targets (every instance is tagged with it). Defaults to the "
        "resolved provider config's firewall_target_tag."
    ),
)
@optgroup.option(
    "--network",
    "network",
    default=None,
    help="VPC network the rule applies to. Defaults to the resolved provider config's network.",
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
    """Create (or reuse) the GCP firewall rule for mngr-managed instances.

    Idempotent: re-running is a no-op when the rule already exists. Needs
    compute.firewalls.get + compute.firewalls.create. After this succeeds,
    ``mngr create --provider gcp`` only needs instance create/get/list
    permissions (no firewall-management permissions).
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="gcp prepare",
        command_class=_GcpPrepareCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    # Empty tuple => no --allowed-ssh-cidr flags passed: fall back to the
    # resolved provider config's value. Non-empty tuple => caller supplied
    # explicit values, use them verbatim. Mirrors ``mngr aws prepare``.
    effective_cidrs = opts.allowed_ssh_cidrs or base.allowed_ssh_cidrs
    try:
        client = _build_operator_client(
            base,
            opts.project_id,
            opts.zone,
            opts.firewall_name,
            opts.firewall_target_tag,
            opts.network,
            effective_cidrs,
            mngr_ctx.concurrency_group,
        )
    except google_auth_exceptions.GoogleAuthError as e:
        # GoogleAuthError is not an ``MngrError``, so wrap it for a clean CLI
        # message. The no-ADC / no-project cases (``GcpCredentialsError`` /
        # ``GcpProjectError`` from ``_build_operator_client``) are already
        # ``MngrError`` subclasses, so they propagate with their specific type
        # intact rather than being flattened.
        raise GcpCredentialsError(str(e)) from e
    result = client.ensure_firewall()
    _output_prepare_result(result, client.firewall_name, client.project_id, output_opts.output_format)


@gcp_cli_group.command(name="cleanup")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="gcp",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(project_id, network, firewall_name). When the block does not exist, "
        "GcpProviderConfig class defaults are used as the fallback."
    ),
)
@optgroup.option(
    "--project",
    "project_id",
    default=None,
    help="GCP project ID. Defaults to the resolved provider config's project_id (or the gcloud/ADC default).",
)
@optgroup.option(
    "--firewall-name",
    "firewall_name",
    default=None,
    help="Firewall rule name to delete. Defaults to the resolved provider config's firewall_name.",
)
@optgroup.option(
    "--network",
    "network",
    default=None,
    help="VPC network the rule applies to (part of its identity). Defaults to the resolved provider config's network.",
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **_kwargs: Any) -> None:
    """Undo `mngr gcp prepare`: delete the mngr-managed firewall rule.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed instance still exists anywhere in the project -- destroy
    those first with `mngr destroy <agent>` so a running agent's SSH access is
    never stranded. With no instances present, deletes the `mngr-gcp-ssh`
    firewall rule that `mngr gcp prepare` created. Idempotent: a no-op (exit 0)
    when the rule is already gone.

    Needs compute.instances.list (aggregated) + compute.firewalls.get +
    compute.firewalls.delete. Does not touch per-host keys -- those are created
    and deleted by the create/destroy lifecycle, not by `prepare`.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="gcp cleanup",
        command_class=_GcpOperatorCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    try:
        # firewall_target_tag and allowed_ssh_cidrs are irrelevant to delete;
        # pass None (falls back to the base tag) and an empty tuple.
        client = _build_operator_client(
            base, opts.project_id, None, opts.firewall_name, None, opts.network, (), mngr_ctx.concurrency_group
        )
    except google_auth_exceptions.GoogleAuthError as e:
        # Same credential wrapping as the prepare path; the GcpError ValueErrors
        # propagate with their specific type.
        raise GcpCredentialsError(str(e)) from e
    deleted_firewall = _perform_cleanup(client)
    _output_cleanup_result(deleted_firewall, client.firewall_name, client.project_id, output_opts.output_format)
