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

import click
from botocore.exceptions import BotoCoreError
from click_option_group import optgroup

from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.client import SecurityGroupPrepareResult
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_aws.state_bucket import S3StateBucket
from imbue.mngr_aws.state_bucket import S3StateBucketError
from imbue.mngr_vps.cli_helpers import refuse_if_managed_resources_exist
from imbue.mngr_vps.cli_helpers import resolve_provider_config


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

    The operator commands need to land their SG / SG-deletion in the same region
    and VPC the runtime ``mngr create --provider <provider_name>`` path will later
    use. Thin wrapper over the shared ``resolve_provider_config`` (see it for the
    {configured / wrong-backend / missing} contract).
    """
    return resolve_provider_config(
        mngr_ctx,
        provider_name,
        config_cls=AwsProviderConfig,
        default_factory=AwsProviderConfig,
        cloud_label="an AWS backend",
        override_hint=(
            "Pass --region / --vpc-id / --sg-name to override, or point --provider at an AWS-backed block."
        ),
    )


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


def _ensure_state_bucket(base: AwsProviderConfig, region: str | None) -> tuple[str, bool]:
    """Create (idempotently) the required S3 state bucket, returning ``(bucket_name, was_created)``.

    The bucket is required infrastructure, so this is the primary job of ``mngr
    aws prepare``: an unresolvable bucket name, a missing S3/STS permission, or any
    API failure raises (a security-group-only prepare would be misleading -- a
    stopped host could not be listed or resumed). Errors surface as an actionable
    ``click.ClickException``.
    """
    try:
        bucket = _build_state_bucket(base, region)
    except (ValueError, BotoCoreError) as e:
        raise click.ClickException(f"Could not resolve credentials for the required S3 state bucket: {e}") from e
    if bucket is None:
        raise click.ClickException(
            "Could not resolve a state-bucket name (AWS account id unavailable via sts:GetCallerIdentity). "
            "The S3 state bucket is required; re-run `mngr aws prepare` with credentials that can resolve "
            "the account id and manage S3."
        )
    try:
        was_created = bucket.ensure_bucket()
    except S3StateBucketError as e:
        raise click.ClickException(
            f"Failed to create the required S3 state bucket {bucket.bucket_name!r} "
            f"(check S3 permissions, then re-run `mngr aws prepare`): {e}"
        ) from e
    return bucket.bucket_name, was_created


def _refuse_cleanup_if_instances_exist(client: AwsVpsClient) -> None:
    """Raise ``ManagedResourcesExistError`` when any mngr-managed instance still exists.

    Run first by ``mngr aws cleanup``, before any teardown, so a still-running
    instance aborts the whole cleanup (bucket + identity + SG) and strands
    nothing. Split out so the refusal is unit-testable against a stubbed client.
    """
    instances = client.list_mngr_managed_instances()
    refuse_if_managed_resources_exist(
        [str(i["id"]) for i in instances],
        summary=", ".join(f"{i['id']} ({i['state']})" for i in instances),
        resource_noun="instance",
        scope_description=f"region {client.region}",
        cleanup_command="mngr aws cleanup",
    )


def _perform_cleanup(client: AwsVpsClient) -> str | None:
    """Core of ``mngr aws cleanup``: refuse if any instance exists, else delete the SG.

    Returns the deleted security-group id, or ``None`` when it was already absent
    (idempotent). Raises ``ManagedResourcesExistError`` (a ``MngrError``) when any
    mngr-managed instance still exists in the region, so cleanup never strands a
    running agent. Split from the click callback so the refuse/delete decision is
    unit-testable against a stubbed client, without the click runtime or real
    credentials.
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
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws prepare`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` (SG) and
    ``state_bucket_name`` / ``state_bucket_created`` so a caller can tell a
    first-run create from an idempotent no-op.
    """
    bucket_verb = "Created" if was_bucket_created else "Reused existing"
    emit_operator_result(
        "prepared",
        [
            OperatorResultPart.shown(
                f"Prepared AWS security group {result.security_group_id} in region {region}",
                security_group_id=result.security_group_id,
                region=region,
                created=result.was_created,
            ),
            OperatorResultPart.shown_if(
                state_bucket_name,
                f"{bucket_verb} S3 state bucket {state_bucket_name} in region {region}",
                state_bucket_name=state_bucket_name,
                state_bucket_created=was_bucket_created,
            ),
        ],
        output_format,
    )


def _output_cleanup_result(
    deleted_sg_id: str | None,
    region: str,
    deleted_bucket_name: str | None,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws cleanup`` in the requested format.

    HUMAN: one (or more) result lines to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the security group was
    already absent; ``state_bucket_deleted`` carries the deleted bucket name (or
    None when no bucket existed).
    """
    emit_operator_result(
        "cleaned_up",
        [
            OperatorResultPart.shown(
                f"Cleaned up AWS security group {deleted_sg_id} in region {region}"
                if deleted_sg_id is not None
                else f"Nothing to clean up: no mngr-managed security group in region {region}.",
                security_group_id=deleted_sg_id,
                region=region,
                deleted=deleted_sg_id is not None,
            ),
            OperatorResultPart.shown_if(
                deleted_bucket_name,
                f"Deleted S3 state bucket {deleted_bucket_name} in region {region}",
                state_bucket_deleted=deleted_bucket_name,
            ),
        ],
        output_format,
    )


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
    # Required bucket setup: a missing S3/STS permission or API failure raises
    # (the bucket is prepare's primary job; a SG-only prepare would leave offline
    # host state unavailable).
    state_bucket_name, was_bucket_created = _ensure_state_bucket(base, opts.region)
    # Offline host_dir needs no IAM identity: it is captured operator-side at
    # `mngr stop` (mngr reads host_dir off the box and uploads it with the
    # operator's creds), so prepare sets up only the security group + bucket.
    _output_prepare_result(result, client.region, state_bucket_name, was_bucket_created, output_opts.output_format)


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
    deleted_sg_id = _perform_cleanup(client)
    _output_cleanup_result(deleted_sg_id, client.region, deleted_bucket_name, output_opts.output_format)


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
