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


def _perform_cleanup(client: AwsVpsClient) -> str | None:
    """Core of ``mngr aws cleanup``: refuse if instances exist, else delete the SG.

    Returns the deleted security group id, or ``None`` when there was nothing
    to delete. Raises ``click.ClickException`` when any mngr-managed instance
    still exists in the region, so cleanup never strands a running agent. Split
    from the click callback so the refuse/delete decision is unit-testable
    against a stubbed client, without the click runtime or real credentials.
    """
    instances = client.list_mngr_managed_instances()
    if instances:
        summary = ", ".join(f"{i['id']} ({i['state']})" for i in instances)
        raise click.ClickException(
            f"Refusing to clean up region {client.region}: {len(instances)} mngr-managed "
            f"instance(s) still exist: {summary}. Destroy them first with `mngr destroy "
            "<agent>` (or terminate them), then re-run `mngr aws cleanup`."
        )
    return client.delete_security_group()


def _output_prepare_result(
    result: SecurityGroupPrepareResult,
    region: str,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws prepare`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``prepared`` event. The structured forms carry ``created`` so a caller can
    tell a first-run create from an idempotent no-op.
    """
    data = {
        "security_group_id": result.security_group_id,
        "region": region,
        "created": result.was_created,
    }
    match output_format:
        case OutputFormat.JSON:
            write_json_line(data)
        case OutputFormat.JSONL:
            emit_event("prepared", data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("Prepared AWS security group {} in region {}", result.security_group_id, region)
        case _ as unreachable:
            assert_never(unreachable)


def _output_cleanup_result(
    deleted_sg_id: str | None,
    region: str,
    output_format: OutputFormat,
) -> None:
    """Emit the result of ``mngr aws cleanup`` in the requested format.

    HUMAN: one result line to stdout. JSON: a single object. JSONL: a
    ``cleaned_up`` event. ``deleted`` is False when the security group was
    already absent (idempotent no-op).
    """
    data = {
        "security_group_id": deleted_sg_id,
        "region": region,
        "deleted": deleted_sg_id is not None,
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
        case _ as unreachable:
            assert_never(unreachable)


@click.group(name="aws")
def aws_cli_group() -> None:
    """AWS-provider operator commands (one-time setup, future AMI tooling)."""


@aws_cli_group.command(name="prepare")
@click.option(
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
@click.option(
    "--region",
    "region",
    default=None,
    help="AWS region. Defaults to the resolved provider config's default_region.",
)
@click.option(
    "--sg-name",
    "sg_name",
    default=None,
    help="Security group name to create / reuse. Defaults to the provider config's SG name.",
)
@click.option(
    "--vpc-id",
    "vpc_id",
    default=None,
    help="VPC id to scope the SG lookup. Without this, multi-VPC name collisions raise.",
)
@click.option(
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
    """Create (or reuse) the AWS security group for mngr-managed instances.

    Idempotent: re-running re-authorizes any missing ingress rules but does
    not duplicate. Needs ec2:DescribeSecurityGroups + ec2:CreateSecurityGroup
    + ec2:AuthorizeSecurityGroupIngress. After this succeeds, `mngr create
    --provider aws` only needs RunInstances + DescribeSecurityGroups +
    DescribeInstances + ImportKeyPair etc. (no SG-management permissions).
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
    _output_prepare_result(result, client.region, output_opts.output_format)


@aws_cli_group.command(name="cleanup")
@click.option(
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
@click.option(
    "--region",
    "region",
    default=None,
    help="AWS region. Defaults to the resolved provider config's default_region.",
)
@click.option(
    "--sg-name",
    "sg_name",
    default=None,
    help="Security group name to delete. Defaults to the provider config's SG name.",
)
@click.option(
    "--vpc-id",
    "vpc_id",
    default=None,
    help="VPC id to scope the SG lookup. Without this, multi-VPC name collisions raise.",
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **_kwargs: Any) -> None:
    """Undo `mngr aws prepare`: delete the mngr-managed security group.

    The safe inverse of `prepare`. Refuses (non-zero exit, deletes nothing) if
    any mngr-managed instance still exists in the region -- destroy those first
    with `mngr destroy <agent>` so a running agent is never stranded. With no
    instances present, deletes the auto-created security group. The name comes
    from `--sg-name` if supplied; otherwise from the resolved
    `[providers.<--provider>]` block's `security_group.name` when that block
    configures an `AutoCreateSecurityGroup`; otherwise (block missing, non-AWS,
    or configured with an `ExistingSecurityGroup` -- which carries an `id`
    rather than a name) the default `mngr-aws` is used. Idempotent: a no-op
    (exit 0) when the security group is already gone.

    Needs ec2:DescribeInstances + ec2:DescribeSecurityGroups +
    ec2:DeleteSecurityGroup. Does not touch per-host keypairs -- those are
    created and deleted by the create/destroy lifecycle, not by `prepare`
    or `cleanup`.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="aws cleanup",
        command_class=_AwsOperatorCliOptions,
    )
    base = _resolve_provider_config(mngr_ctx, opts.provider)
    try:
        client = _build_operator_client(base, opts.region, opts.sg_name, opts.vpc_id, ())
    except (ValueError, BotoCoreError) as e:
        # Same credential / environment errors as the prepare path.
        raise click.ClickException(str(e)) from e

    deleted_sg_id = _perform_cleanup(client)
    _output_cleanup_result(deleted_sg_id, client.region, output_opts.output_format)


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
