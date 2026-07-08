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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_gcp.client import FirewallPrepareResult
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.config import get_gcloud_compute_zone
from imbue.mngr_gcp.errors import GcpCredentialsError
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.errors import GcpStateBucketNotEmptyError
from imbue.mngr_gcp.errors import GcpStateBucketProvisioningError
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_gcp.state_bucket import GcsStateBucketError
from imbue.mngr_vps.cli_helpers import refuse_if_managed_resources_exist
from imbue.mngr_vps.cli_helpers import resolve_provider_config


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


class _GcpCleanupCliOptions(_GcpOperatorCliOptions):
    force: bool


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> GcpProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to land their firewall rule in the same project,
    network, and zone the runtime ``mngr create --provider <provider_name>`` path
    will later use. Thin wrapper over the shared ``resolve_provider_config`` (see
    it for the {configured / wrong-backend / missing} contract).
    """
    return resolve_provider_config(
        mngr_ctx,
        provider_name,
        config_cls=GcpProviderConfig,
        default_factory=GcpProviderConfig,
        cloud_label="a GCP backend",
        override_hint=(
            "Pass --project / --zone / --network / --firewall-name to override, or "
            "point --provider at a GCP-backed block."
        ),
    )


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


def _build_state_bucket(base: GcpProviderConfig, client: GcpVpsClient) -> GcsStateBucket:
    """Build the GCS state bucket for the operator commands.

    Uses the resolved provider config's bucket name (or the derived
    ``mngr-state-<project_id>``). The bucket lives in the same region as the
    GCE instances writing to it (the resolved zone's region).
    """
    return base.build_state_bucket(
        credentials=client.credentials,
        project_id=client.project_id,
        region=base.resolve_state_bucket_region(client.zone),
    )


def _ensure_state_bucket(base: GcpProviderConfig, client: GcpVpsClient) -> tuple[str, bool]:
    """Create (idempotently) the GCS state bucket, returning ``(bucket_name, was_created)``.

    The bucket backs offline ``host_dir`` reads (captured operator-side at ``mngr
    stop``), so a missing storage permission or API failure raises here rather
    than masquerading as a silent "no bucket". Errors surface as an actionable
    ``GcpStateBucketProvisioningError`` (a ``MngrError``). Mirrors
    ``mngr_aws.cli._ensure_state_bucket``.
    """
    bucket = _build_state_bucket(base, client)
    try:
        was_created = bucket.ensure_bucket()
    except GcsStateBucketError as e:
        raise GcpStateBucketProvisioningError(
            f"Failed to create the GCS state bucket {bucket.bucket_name!r} "
            f"(check storage permissions, then re-run `mngr gcp prepare`): {e}"
        ) from e
    return bucket.bucket_name, was_created


def _perform_state_bucket_cleanup(bucket: GcsStateBucket, *, force: bool) -> str | None:
    """Delete the GCS state bucket, refusing while any managed-host state remains.

    Returns the deleted bucket name, or ``None`` when no bucket existed. Unless
    ``force`` is set, raises ``GcpStateBucketNotEmptyError`` (a ``MngrError``)
    when the bucket still holds ``hosts/`` state. By the time this runs the
    instance-exists check has already passed, so any remaining state is
    *orphaned* offline state (a host whose instance is gone but whose
    ``delete_host_state`` never ran) -- deleting it silently could drop offline
    records the operator still wants, so we refuse and let ``--force`` opt into
    deleting it. Mirrors ``mngr_aws.cli._perform_state_bucket_cleanup``.
    """
    if not bucket.bucket_exists():
        return None
    if not force and bucket.has_any_host_state():
        raise GcpStateBucketNotEmptyError(
            f"Refusing to delete GCS state bucket {bucket.bucket_name!r}: it still holds offline host "
            "state (from hosts that are no longer running instances). Re-run with `--force` to "
            "delete the bucket and the remaining state."
        )
    bucket.delete_bucket()
    return bucket.bucket_name


def _refuse_cleanup_if_instances_exist(client: GcpVpsClient) -> None:
    """Raise ``ManagedResourcesExistError`` when any mngr-managed instance still exists.

    Run first by ``mngr gcp cleanup``, before any teardown, so a still-running
    instance aborts the whole cleanup (bucket + firewall) and strands nothing.
    Split out so the refusal is unit-testable against a stubbed client. Mirrors
    ``mngr_aws.cli._refuse_cleanup_if_instances_exist``.
    """
    instances = client.list_mngr_managed_instances()
    refuse_if_managed_resources_exist(
        [str(i["id"]) for i in instances],
        summary=", ".join(f"{i['id']} ({i['state']} in {i['zone']})" for i in instances),
        resource_noun="instance",
        scope_description=f"project {client.project_id}",
        cleanup_command="mngr gcp cleanup",
    )


def _perform_cleanup(client: GcpVpsClient) -> str | None:
    """Core of ``mngr gcp cleanup``: refuse if instances exist, else delete the rule.

    Returns the deleted firewall rule name, or ``None`` when there was nothing to
    delete. Raises ``ManagedResourcesExistError`` (a ``MngrError``) when any
    mngr-managed instance still exists in the project, so cleanup never strands a
    running agent's SSH access. Split from the click callback so the refuse/delete
    decision is unit-testable against a stubbed client, without the click runtime
    or real credentials. Mirrors ``mngr_aws.cli._perform_cleanup``.
    """
    _refuse_cleanup_if_instances_exist(client)
    return client.delete_firewall()


def _output_prepare_result(
    result: FirewallPrepareResult,
    firewall_name: str,
    project_id: str,
    state_bucket_name: str | None,
    was_bucket_created: bool,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr gcp prepare`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` (firewall) and
    ``state_bucket_name`` / ``state_bucket_created`` so a caller can tell a
    first-run create from an idempotent no-op.
    """
    bucket_verb = "Created" if was_bucket_created else "Reused existing"
    emit_operator_result(
        "prepared",
        [
            OperatorResultPart.shown(
                f"Prepared GCP firewall rule {firewall_name} (tag {result.target_tag}) in project {project_id}",
                firewall_name=firewall_name,
                target_tag=result.target_tag,
                project_id=project_id,
                created=result.was_created,
            ),
            OperatorResultPart.shown_if(
                state_bucket_name,
                f"{bucket_verb} GCS state bucket {state_bucket_name} in project {project_id}",
                state_bucket_name=state_bucket_name,
                state_bucket_created=was_bucket_created,
            ),
        ],
        output_format,
    )


def _output_cleanup_result(
    deleted_firewall: str | None,
    firewall_name: str,
    project_id: str,
    deleted_bucket_name: str | None,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr gcp cleanup`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the rule was already absent
    (idempotent no-op); ``state_bucket_deleted`` carries the deleted bucket name
    (or None when no bucket existed).
    """
    emit_operator_result(
        "cleaned_up",
        [
            OperatorResultPart.shown(
                f"Cleaned up GCP firewall rule {deleted_firewall} in project {project_id}"
                if deleted_firewall is not None
                else f"Nothing to clean up: no firewall rule {firewall_name} in project {project_id}.",
                firewall_name=firewall_name,
                project_id=project_id,
                deleted=deleted_firewall is not None,
            ),
            OperatorResultPart.shown_if(
                deleted_bucket_name,
                f"Deleted GCS state bucket {deleted_bucket_name} in project {project_id}",
                state_bucket_deleted=deleted_bucket_name,
            ),
        ],
        output_format,
    )


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
    """Create (or reuse) the GCP firewall rule and GCS state bucket for mngr-managed instances.

    Idempotent: re-running is a no-op when each resource already exists. Needs
    compute.firewalls.get + compute.firewalls.create + storage.buckets.get +
    storage.buckets.create. After this succeeds, ``mngr create --provider gcp``
    only needs instance create/get/list permissions (no firewall-management
    permissions); the runtime stop/start path additionally needs
    storage.objects.* on the state bucket when ``is_offline_host_dir_enabled``
    is on (see the README's "Required IAM permissions" section).
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
    # GCS state bucket: backs the offline ``host_dir`` mirror written at ``mngr
    # stop``. Created here so the runtime create/stop paths can attach to it with
    # no extra bucket-management permissions. A storage permission or API failure
    # surfaces as a GcpStateBucketProvisioningError (the bucket is the
    # offline-host_dir feature's only backing store; a firewall-only prepare would leave offline host_dir
    # unavailable). Offline host_dir is the operator's own write path (no managed
    # identity needed), so prepare sets up only the firewall + bucket.
    state_bucket_name, was_bucket_created = _ensure_state_bucket(base, client)
    _output_prepare_result(
        result,
        client.firewall_name,
        client.project_id,
        state_bucket_name,
        was_bucket_created,
        output_opts.output_format,
    )


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
@optgroup.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Also delete the GCS state bucket when it still holds offline host state left over from "
        "hosts that no longer exist as instances (otherwise cleanup refuses to delete a non-empty bucket)."
    ),
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **_kwargs: Any) -> None:
    """Undo `mngr gcp prepare`: delete the mngr-managed firewall rule and state bucket.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed instance still exists anywhere in the project -- destroy
    those first with `mngr destroy <agent>` so a running agent's SSH access is
    never stranded. With no instances present, deletes the `mngr-gcp-ssh`
    firewall rule and the GCS state bucket that `mngr gcp prepare` created.
    Idempotent: a no-op (exit 0) when each resource is already gone.

    Needs compute.instances.list (aggregated) + compute.firewalls.get +
    compute.firewalls.delete + storage.buckets.get + storage.buckets.delete +
    storage.objects.list + storage.objects.delete (the storage.objects.*
    permissions are required because the bucket is emptied before deletion).
    Does not touch per-host keys -- those are created and deleted by the
    create/destroy lifecycle, not by `prepare` or `cleanup`.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="gcp cleanup",
        command_class=_GcpCleanupCliOptions,
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
    # Refuse the whole cleanup (delete nothing) while any mngr-managed instance
    # still exists, BEFORE tearing down the bucket -- a running instance must
    # abort cleanup so its offline state is never stranded.
    _refuse_cleanup_if_instances_exist(client)
    # No instances remain: tear down the bucket while it holds no host state
    # (its own refusal mirrors the instance check, as defense in depth).
    bucket = _build_state_bucket(base, client)
    deleted_bucket_name = _perform_state_bucket_cleanup(bucket, force=opts.force)
    deleted_firewall = _perform_cleanup(client)
    _output_cleanup_result(
        deleted_firewall,
        client.firewall_name,
        client.project_id,
        deleted_bucket_name,
        output_opts.output_format,
    )
