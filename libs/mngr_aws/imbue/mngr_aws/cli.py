"""``mngr aws ...`` operator commands.

`mngr aws prepare` does the one-time privileged setup (security group
creation + SSH ingress authorization) so the regular `mngr create` path
can run with restricted IAM (RunInstances + DescribeSecurityGroups, no
CreateSecurityGroup / AuthorizeSecurityGroupIngress). Conventional split:
admin runs prepare once; developers run create with limited creds.

`mngr aws cleanup` is the inverse of prepare: it deletes the mngr-managed
security group so a region returns to its pre-prepare state. It refuses
while any mngr-managed instance still exists, so it cannot strand a
running agent.

`mngr aws ami` is a `[future]` placeholder for a build-and-register
command that produces a Debian + Docker + deps-baked AMI to skip the
~60-90s cloud-init bootstrap on every create.
"""

from typing import Any
from typing import assert_never

import click
from botocore.exceptions import BotoCoreError
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
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.client import SecurityGroupPrepareResult
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_aws.state_bucket import S3StateHostIdentity
from imbue.mngr_aws.state_bucket import S3StateHostIdentityError


class _AwsOperatorCliOptions(CommonCliOptions):
    """Shared option shape for ``mngr aws prepare`` and ``mngr aws cleanup``.

    Both commands take the same provider-selection + region/SG/VPC overrides;
    the only delta is that ``prepare`` also accepts ``--allowed-ssh-cidr`` for
    the auto-created SG's ingress rules (``cleanup`` deletes the SG and so
    needs no ingress information).
    """

    provider: str
    region: str | None
    sg_name: str | None
    vpc_id: str | None


class _AwsPrepareCliOptions(_AwsOperatorCliOptions):
    allowed_ssh_cidrs: tuple[str, ...]


class _AwsCleanupCliOptions(_AwsOperatorCliOptions):
    force: bool


def _resolve_provider_config(mngr_ctx: MngrContext, provider_name: str) -> AwsProviderConfig:
    """Return the user's ``[providers.<provider_name>]`` block, or class defaults.

    The operator commands need to land their SG / SG-deletion in the same
    region and VPC the runtime ``mngr create --provider <provider_name>`` path
    will later use. Class defaults (``AwsProviderConfig()``) are a fallback for
    the first-run case where the user has not yet pinned a provider block; if
    their settings.toml *does* configure the named provider, we honor it.

    When the looked-up config is not an ``AwsProviderConfig`` (e.g. the user
    pointed ``[providers.aws]`` at a non-AWS backend), fall back to class
    defaults rather than erroring -- the operator command's CLI options
    (``--region`` / ``--vpc-id`` / ``--sg-name``) can still drive an
    AWS-targeted run. A warning is emitted in this case so the user notices
    their ``--provider`` selection did not have the intended effect (a silent
    fallback to class defaults would otherwise land the SG in
    ``default_region`` / no VPC with no visible signal). The missing-block
    case is silent because that is the expected first-run shape.
    """
    config = mngr_ctx.config.providers.get(ProviderInstanceName(provider_name))
    if isinstance(config, AwsProviderConfig):
        return config
    if config is not None:
        logger.warning(
            "Provider {!r} is configured but is not an AWS backend (got {}); "
            "falling back to AwsProviderConfig class defaults. Pass "
            "--region / --vpc-id / --sg-name to override, or point --provider "
            "at an AWS-backed block.",
            provider_name,
            type(config).__name__,
        )
    return AwsProviderConfig()


def _build_operator_client(
    base: AwsProviderConfig,
    region: str | None,
    sg_name: str | None,
    vpc_id: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> AwsVpsClient:
    """Construct an ``AwsVpsClient`` for the ``mngr aws`` operator commands.

    Bridges click options into the client constructor. ``region`` /
    ``vpc_id`` fall back to the matching field on ``base`` (the user's
    resolved ``AwsProviderConfig`` for the selected provider, or class
    defaults when the user has not pinned one) so the SG lands in the
    region/VPC the runtime create path will use.

    The ``sg_name`` fallback is narrower: when ``base.security_group`` is
    an ``AutoCreateSecurityGroup``, its ``name`` is used; when it is an
    ``ExistingSecurityGroup`` (the user has wired in an externally-managed
    SG), the helper substitutes ``AutoCreateSecurityGroup()`` defaults
    (name ``mngr-aws``) because prepare/cleanup only operate on
    auto-created SGs -- an externally-managed SG is not theirs to create
    or delete.

    ``prepare`` passes the effective ingress CIDRs; ``cleanup`` passes
    ``()`` (it only deletes, never authorizes ingress, so the value is
    unused on that path).
    """
    effective_region = region or base.default_region
    if sg_name is not None:
        effective_sg = AutoCreateSecurityGroup(name=sg_name)
    elif isinstance(base.security_group, AutoCreateSecurityGroup):
        effective_sg = base.security_group
    else:
        effective_sg = AutoCreateSecurityGroup()
    effective_vpc_id = vpc_id if vpc_id is not None else base.vpc_id
    session = base.get_session()
    # ``ami_id`` is unused by the SG-management methods (ensure / delete) and
    # by ``list_mngr_managed_instances``, so leave the field empty -- no
    # operator path (``prepare`` / ``cleanup``) calls ``create_instance``.
    return AwsVpsClient(
        session=session,
        region=effective_region,
        security_group=effective_sg,
        vpc_id=effective_vpc_id,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        container_ssh_port=base.container_ssh_port,
    )


def _build_state_bucket(base: AwsProviderConfig, region: str | None) -> S3StateBucket | None:
    """Build the S3 state bucket for the operator commands, or None when unresolvable.

    Uses the resolved provider config's bucket name (or the derived
    ``mngr-state-<account_id>-<region>``). Returns None when the account id
    cannot be resolved (e.g. missing ``sts:GetCallerIdentity`` permission), so
    the operator command degrades gracefully.
    """
    session = base.get_session()
    effective_region = region or base.default_region
    bucket_name = base.resolve_state_bucket_name(session, effective_region)
    if bucket_name is None:
        return None
    return S3StateBucket(session=session, region=effective_region, bucket_name=bucket_name)


def _ensure_state_bucket_best_effort(base: AwsProviderConfig, region: str | None) -> tuple[str | None, bool]:
    """Ensure the state bucket exists, returning ``(bucket_name, was_created)``.

    Best-effort for ``mngr aws prepare``: a missing-permission / API failure (or
    an unresolvable bucket name) is logged at WARNING and surfaces as
    ``(None, False)`` so the security-group prepare still succeeds even when the
    operator's key cannot manage S3.
    """
    try:
        bucket = _build_state_bucket(base, region)
    except (ValueError, BotoCoreError) as e:
        logger.warning("Could not resolve credentials for the S3 state bucket; skipping bucket setup: {}", e)
        return None, False
    if bucket is None:
        logger.warning(
            "Could not resolve a state-bucket name (AWS account id unavailable); skipping bucket setup. "
            "Offline host state will fall back to the EC2 tag mirror."
        )
        return None, False
    try:
        was_created = bucket.ensure_bucket()
    except S3StateBucketError as e:
        logger.warning(
            "Failed to create the S3 state bucket {!r} (offline host state will fall back to the EC2 tag mirror): {}",
            bucket.bucket_name,
            e,
        )
        return None, False
    return bucket.bucket_name, was_created


def _build_host_identity(base: AwsProviderConfig, region: str | None) -> S3StateHostIdentity | None:
    """Build the bucket-write IAM host identity for the operator commands, or None when unresolvable."""
    session = base.get_session()
    effective_region = region or base.default_region
    bucket_name = base.resolve_state_bucket_name(session, effective_region)
    if bucket_name is None:
        return None
    return S3StateHostIdentity(session=session, region=effective_region, bucket_name=bucket_name)


def _provision_host_identity(identity: S3StateHostIdentity) -> str | None:
    """Provision the bucket-write IAM host identity best-effort, returning its name or None.

    A permission/API failure degrades to a WARNING so the security-group + bucket
    prepare still succeed; offline host_dir just won't work until prepare is re-run
    with sufficient IAM.
    """
    try:
        return identity.ensure_host_identity()
    except S3StateHostIdentityError as e:
        logger.warning(
            "Failed to provision the host-dir IAM identity {!r} (offline host_dir reads will be "
            "unavailable until prepare is re-run with sufficient IAM): {}",
            identity.identity_name,
            e,
        )
        return None


def _resolve_and_provision_host_identity(
    base: AwsProviderConfig, region: str | None, *, state_bucket_name: str | None
) -> str | None:
    """Resolve credentials, build the host identity for the (already-resolved) bucket, then provision it.

    The identity's inline policy is scoped to the bucket, so provisioning it is
    only meaningful once the bucket exists. ``state_bucket_name`` is the name
    ``_ensure_state_bucket_best_effort`` resolved (None when bucket setup was
    skipped/failed). When None, or when credentials cannot be resolved, this
    degrades to a WARNING and returns None. Called only when
    ``is_offline_host_dir_enabled`` is set.
    """
    if state_bucket_name is None:
        logger.warning(
            "Cannot provision the host-dir IAM identity: the S3 state bucket could not be set up "
            "(its inline policy is scoped to that bucket). Re-run with sufficient S3/STS permissions."
        )
        return None
    try:
        session = base.get_session()
    except (ValueError, BotoCoreError) as e:
        logger.warning("Could not resolve credentials for the host-dir IAM identity; skipping it: {}", e)
        return None
    identity = S3StateHostIdentity(
        session=session, region=region or base.default_region, bucket_name=state_bucket_name
    )
    return _provision_host_identity(identity)


def _perform_host_identity_cleanup(identity: S3StateHostIdentity | None) -> str | None:
    """Delete the bucket-write IAM host identity, best-effort. Returns its name or None.

    Idempotent: a missing role/instance-profile is a no-op. A permission/API
    failure is logged at WARNING and swallowed so it never blocks the rest of
    ``mngr aws cleanup`` (the SG + bucket teardown still proceed).
    """
    if identity is None:
        return None
    if not identity.host_identity_exists():
        return None
    try:
        identity.delete_host_identity()
    except S3StateHostIdentityError as e:
        logger.warning("Failed to delete the host-dir IAM identity {!r}; skipping it: {}", identity.identity_name, e)
        return None
    return identity.identity_name


def _refuse_cleanup_if_instances_exist(client: AwsVpsClient) -> None:
    """Raise ``click.ClickException`` when any mngr-managed instance still exists.

    Run first by ``mngr aws cleanup``, before any teardown, so a still-running
    instance aborts the whole cleanup (bucket + identity + SG) and strands
    nothing. Split out so the refusal is unit-testable against a stubbed client.
    """
    instances = client.list_mngr_managed_instances()
    if instances:
        summary = ", ".join(f"{i['id']} ({i['state']})" for i in instances)
        raise click.ClickException(
            f"Refusing to clean up region {client.region}: {len(instances)} mngr-managed "
            f"instance(s) still exist: {summary}. Destroy them first with `mngr destroy "
            "<agent>` (or terminate them), then re-run `mngr aws cleanup`."
        )


def _perform_cleanup(client: AwsVpsClient) -> str | None:
    """Core of ``mngr aws cleanup``: refuse if any instance exists, else delete the SG.

    Returns the deleted security-group id, or ``None`` when it was already absent
    (idempotent). Raises ``click.ClickException`` when any mngr-managed instance
    still exists in the region, so cleanup never strands a running agent. Split
    from the click callback so the refuse/delete decision is unit-testable
    against a stubbed client, without the click runtime or real credentials.
    """
    _refuse_cleanup_if_instances_exist(client)
    return client.delete_security_group()


def _perform_state_bucket_cleanup(bucket: S3StateBucket | None, *, force: bool) -> str | None:
    """Delete the state bucket, refusing while any managed-host state remains.

    Returns the deleted bucket name, or ``None`` when no bucket is configured /
    none existed. Unless ``force`` is set, raises ``click.ClickException``
    when the bucket still holds ``hosts/`` state. By the time this runs the
    instance-exists check has already passed, so any remaining state is
    *orphaned* offline state (a host whose instance is gone but whose
    ``delete_host_state`` never ran, or one terminated outside mngr) -- deleting
    it silently could drop offline records the operator still wants, so we refuse
    and let ``--force`` opt into deleting it. Split out so the
    refuse/delete decision is unit-testable.
    """
    if bucket is None:
        return None
    if not bucket.bucket_exists():
        return None
    if not force and bucket.has_any_host_state():
        raise click.ClickException(
            f"Refusing to delete S3 state bucket {bucket.bucket_name!r}: it still holds offline host "
            "state (from hosts that are no longer running instances). Re-run with `--force` to "
            "delete the bucket and the remaining state."
        )
    bucket.delete_bucket()
    return bucket.bucket_name


def _output_prepare_result(
    result: SecurityGroupPrepareResult,
    region: str,
    state_bucket_name: str | None,
    was_bucket_created: bool,
    host_identity_name: str | None,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws prepare`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` (SG),
    ``state_bucket_name`` / ``state_bucket_created`` (None when the bucket setup
    was skipped, e.g. missing S3/STS permissions), and ``host_identity_name``
    (None when the host-dir IAM identity was skipped or could not be
    provisioned) so a caller can tell a first-run create from an idempotent
    no-op.
    """
    data = {
        "security_group_id": result.security_group_id,
        "region": region,
        "created": result.was_created,
        "state_bucket_name": state_bucket_name,
        "state_bucket_created": was_bucket_created,
        "host_identity_name": host_identity_name,
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event("prepared", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Prepared AWS security group {} in region {}", result.security_group_id, region)
            if state_bucket_name is not None:
                write_human_line(
                    "{} S3 state bucket {} in region {}",
                    "Created" if was_bucket_created else "Reused existing",
                    state_bucket_name,
                    region,
                )
            if host_identity_name is not None:
                write_human_line("Provisioned host-dir IAM identity {}", host_identity_name)
        case _ as unreachable:
            assert_never(unreachable)


def _output_cleanup_result(
    deleted_sg_id: str | None,
    region: str,
    deleted_bucket_name: str | None,
    deleted_host_identity_name: str | None,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws cleanup`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the security group was
    already absent; ``state_bucket_deleted`` carries the deleted bucket name (or
    None when no bucket existed / setup was skipped); ``host_identity_deleted``
    carries the deleted IAM identity name (or None when none existed).
    """
    data = {
        "security_group_id": deleted_sg_id,
        "region": region,
        "deleted": deleted_sg_id is not None,
        "state_bucket_deleted": deleted_bucket_name,
        "host_identity_deleted": deleted_host_identity_name,
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event("cleaned_up", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if deleted_sg_id is None:
                write_human_line("Nothing to clean up: no mngr-managed security group in region {}.", region)
            else:
                write_human_line("Cleaned up AWS security group {} in region {}", deleted_sg_id, region)
            if deleted_bucket_name is not None:
                write_human_line("Deleted S3 state bucket {} in region {}", deleted_bucket_name, region)
            if deleted_host_identity_name is not None:
                write_human_line("Deleted host-dir IAM identity {}", deleted_host_identity_name)
        case _ as unreachable:
            assert_never(unreachable)


@click.group(name="aws")
def aws_cli_group() -> None:
    """AWS-provider operator commands (one-time setup, future AMI tooling)."""


@aws_cli_group.command(name="prepare")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="aws",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(default_region, vpc_id, security_group.name, allowed_ssh_cidrs). When the "
        "block does not exist, AwsProviderConfig class defaults are used as the "
        "fallback. CLI options below override either source."
    ),
)
@optgroup.option(
    "--region",
    "region",
    default=None,
    help="AWS region. Defaults to the resolved provider config's default_region.",
)
@optgroup.option(
    "--sg-name",
    "sg_name",
    default=None,
    help="Security group name to create / reuse. Defaults to the provider config's SG name.",
)
@optgroup.option(
    "--vpc-id",
    "vpc_id",
    default=None,
    help="VPC id to scope the SG lookup. Without this, multi-VPC name collisions raise.",
)
@optgroup.option(
    "--allowed-ssh-cidr",
    "allowed_ssh_cidrs",
    multiple=True,
    help=(
        "Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. "
        "Defaults to the provider config's allowed_ssh_cidrs. Tighten for production."
    ),
)
@add_common_options
@click.pass_context
def prepare(ctx: click.Context, **_kwargs: Any) -> None:
    """Provision the AWS security group for mngr-managed instances.

    Creates (or reuses) the `mngr-aws` security group (ingress on tcp/22 +
    tcp/<container_ssh_port> per allowed_ssh_cidrs). Idempotent: re-running
    re-authorizes any missing ingress rules without duplicating. Needs
    ec2:DescribeSecurityGroups + ec2:CreateSecurityGroup +
    ec2:AuthorizeSecurityGroupIngress. After this succeeds, `mngr create
    --provider aws` only needs RunInstances + DescribeSecurityGroups +
    DescribeInstances + ImportKeyPair etc. (no SG-management permissions, and no
    IAM at all -- idle self-stop powers the host off rather than calling the EC2
    API, so it needs no instance profile).
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="aws prepare",
        command_class=_AwsPrepareCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    # Empty tuple => no --allowed-ssh-cidr flags passed: fall back to the
    # resolved provider config's value. Non-empty tuple => caller supplied
    # explicit values, use them verbatim.
    effective_cidrs = opts.allowed_ssh_cidrs or base.allowed_ssh_cidrs
    try:
        client = _build_operator_client(base, opts.region, opts.sg_name, opts.vpc_id, effective_cidrs)
    except (ValueError, BotoCoreError) as e:
        # ``ValueError`` covers the no-credentials case raised by
        # ``AwsProviderConfig.get_session``; ``BotoCoreError`` covers
        # boto3-rejected environment shapes (e.g. ``ProfileNotFound`` from a
        # bad ``AWS_PROFILE``). Mirrors the pair caught by
        # ``AwsProviderBackend.build_provider_instance``.
        raise click.ClickException(str(e)) from e
    result = client.ensure_security_group()
    # Best-effort bucket setup: a missing S3/STS permission degrades to a
    # warning so the SG prepare still succeeds (offline state then falls back
    # to the EC2 tag mirror).
    state_bucket_name, was_bucket_created = _ensure_state_bucket_best_effort(base, opts.region)
    # Provision the bucket-write IAM identity (best-effort) when the offline
    # host_dir feature is enabled. The bucket-only steps above are unconditional,
    # so flipping is_offline_host_dir_enabled on and re-running prepare adds just
    # the identity.
    host_identity_name = None
    if base.is_offline_host_dir_enabled:
        host_identity_name = _resolve_and_provision_host_identity(
            base, opts.region, state_bucket_name=state_bucket_name
        )
    _output_prepare_result(
        result, client.region, state_bucket_name, was_bucket_created, host_identity_name, output_opts.output_format
    )


@aws_cli_group.command(name="cleanup")
@optgroup.group("Provider")
@optgroup.option(
    "--provider",
    "provider",
    default="aws",
    show_default=True,
    help=(
        "Name of the [providers.NAME] block in settings.toml to read defaults from "
        "(default_region, vpc_id, security_group.name). When the block does not exist, "
        "AwsProviderConfig class defaults are used as the fallback."
    ),
)
@optgroup.option(
    "--region",
    "region",
    default=None,
    help="AWS region. Defaults to the resolved provider config's default_region.",
)
@optgroup.option(
    "--sg-name",
    "sg_name",
    default=None,
    help="Security group name to delete. Defaults to the provider config's SG name.",
)
@optgroup.option(
    "--vpc-id",
    "vpc_id",
    default=None,
    help="VPC id to scope the SG lookup. Without this, multi-VPC name collisions raise.",
)
@optgroup.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Also delete the state bucket when it still holds offline host state left over from "
        "hosts that no longer exist as instances (otherwise cleanup refuses to delete a non-empty bucket)."
    ),
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **_kwargs: Any) -> None:
    """Undo `mngr aws prepare`: delete the mngr-managed security group.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed instance still exists in the region -- destroy those first
    with `mngr destroy <agent>` so a running agent is never stranded. With no
    instances present, deletes the auto-created security group. The SG name comes
    from `--sg-name` if supplied; otherwise from the resolved
    `[providers.<--provider>]` block's `security_group.name` when that block
    configures an `AutoCreateSecurityGroup`; otherwise (block missing, non-AWS,
    or configured with an `ExistingSecurityGroup` -- which carries an `id` rather
    than a name) the default `mngr-aws` is used. Idempotent: a no-op (exit 0)
    when it is already gone.

    Needs ec2:DescribeInstances + ec2:DescribeSecurityGroups +
    ec2:DeleteSecurityGroup. Does not touch per-host keypairs -- those are
    created and deleted by the create/destroy lifecycle, not by `prepare` or
    `cleanup`.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="aws cleanup",
        command_class=_AwsCleanupCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    try:
        client = _build_operator_client(base, opts.region, opts.sg_name, opts.vpc_id, ())
    except (ValueError, BotoCoreError) as e:
        # Same credential / environment errors as the prepare path.
        raise click.ClickException(str(e)) from e

    # Refuse the whole cleanup (delete nothing) while any mngr-managed instance
    # still exists, BEFORE tearing down its bucket / identity -- a running
    # instance must abort cleanup so its offline state and write identity are
    # never stranded.
    _refuse_cleanup_if_instances_exist(client)
    # No instances remain: tear down the bucket while it holds no host state
    # (its own refusal mirrors the instance check, as defense in depth).
    # ``_build_state_bucket`` may raise if S3 creds are unresolvable; surface
    # that to the operator rather than silently skipping.
    try:
        bucket = _build_state_bucket(base, opts.region)
    except (ValueError, BotoCoreError) as e:
        raise click.ClickException(str(e)) from e
    deleted_bucket_name = _perform_state_bucket_cleanup(bucket, force=opts.force)
    # Delete the bucket-write IAM identity after the bucket (best-effort,
    # idempotent). Build errors mirror the bucket-build credential errors.
    try:
        identity = _build_host_identity(base, opts.region)
    except (ValueError, BotoCoreError) as e:
        raise click.ClickException(str(e)) from e
    deleted_host_identity_name = _perform_host_identity_cleanup(identity)
    deleted_sg_id = _perform_cleanup(client)
    _output_cleanup_result(
        deleted_sg_id, client.region, deleted_bucket_name, deleted_host_identity_name, output_opts.output_format
    )


@aws_cli_group.command(name="ami")
def ami() -> None:
    """[future] Build and register an mngr-ready AMI (Debian + Docker + deps).

    Not yet implemented. Tracked in libs/mngr_aws/README.md under Future
    improvements. The intent is to skip the ~60-90s cloud-init bootstrap
    on every `mngr create` by baking Docker and the runtime deps into a
    custom AMI.
    """
    raise click.ClickException(
        "`mngr aws ami` is not yet implemented. See libs/mngr_aws/README.md "
        "(Future improvements) for the planned shape. For now, the per-create "
        "cloud-init path installs docker.io from Debian's repos (5-15s)."
    )
