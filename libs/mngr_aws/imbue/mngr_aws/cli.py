"""``mngr aws ...`` operator commands.

`mngr aws prepare` does the one-time privileged setup (security group
creation + SSH ingress authorization) so the regular `mngr create` path
can run with restricted IAM (RunInstances + DescribeSecurityGroups, no
CreateSecurityGroup / AuthorizeSecurityGroupIngress). Conventional split:
admin runs prepare once; developers run create with limited creds.

`mngr aws ami` is a `[future]` placeholder for a build-and-register
command that produces a Debian + Docker + deps-baked AMI to skip the
~60-90s cloud-init bootstrap on every create.
"""

import click
from botocore.exceptions import BotoCoreError
from loguru import logger

from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import AutoCreateSecurityGroup
from imbue.mngr_aws.config import AwsProviderConfig


def _build_prepare_client(
    region: str | None,
    sg_name: str | None,
    vpc_id: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> AwsVpsClient:
    """Construct an ``AwsVpsClient`` for the ``mngr aws prepare`` command.

    Bridges click options into the client constructor: each option falls
    back to the corresponding ``AwsProviderConfig`` default when not
    supplied. Pulled out of the click callback purely for readability --
    the defaults-and-fallback construction would otherwise crowd the
    callback body alongside the credential-error handling.
    """
    base = AwsProviderConfig()
    effective_region = region or base.default_region
    effective_sg = AutoCreateSecurityGroup(name=sg_name) if sg_name else AutoCreateSecurityGroup()
    effective_vpc_id = vpc_id if vpc_id is not None else base.vpc_id
    session = base.get_session()
    # ``ami_id`` is unused by ensure_security_group, but the AwsVpsClient
    # constructor requires it; a placeholder is fine because the prepare
    # path never calls create_instance.
    return AwsVpsClient(
        session=session,
        region=effective_region,
        ami_id="ami-placeholder",
        security_group=effective_sg,
        vpc_id=effective_vpc_id,
        allowed_ssh_cidrs=allowed_ssh_cidrs,
        container_ssh_port=base.container_ssh_port,
    )


@click.group(name="aws")
def aws_cli_group() -> None:
    """AWS-provider operator commands (one-time setup, future AMI tooling)."""


@aws_cli_group.command(name="prepare")
@click.option(
    "--region",
    "region",
    default=None,
    help="AWS region. Defaults to the provider config's default_region (us-east-1 if unset).",
)
@click.option(
    "--sg-name",
    "sg_name",
    default=None,
    help="Security group name to create / reuse. Defaults to 'mngr-aws'.",
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
        "Defaults to ('0.0.0.0/0',) matching the provider config default. Tighten for production."
    ),
)
def prepare(
    region: str | None,
    sg_name: str | None,
    vpc_id: str | None,
    allowed_ssh_cidrs: tuple[str, ...],
) -> None:
    """Create (or reuse) the AWS security group for mngr-managed instances.

    Idempotent: re-running re-authorizes any missing ingress rules but does
    not duplicate. Needs ec2:DescribeSecurityGroups + ec2:CreateSecurityGroup
    + ec2:AuthorizeSecurityGroupIngress. After this succeeds, `mngr create
    --provider aws` only needs RunInstances + DescribeSecurityGroups +
    DescribeInstances + ImportKeyPair etc. (no SG-management permissions).
    """
    base_defaults = AwsProviderConfig()
    # Empty tuple => no --allowed-ssh-cidr flags passed: fall back to the
    # provider config default. Non-empty tuple => caller supplied explicit
    # values, use them verbatim.
    effective_cidrs = allowed_ssh_cidrs or base_defaults.allowed_ssh_cidrs
    try:
        client = _build_prepare_client(region, sg_name, vpc_id, effective_cidrs)
    except (ValueError, BotoCoreError) as e:
        # ``ValueError`` covers the no-credentials case raised by
        # ``AwsProviderConfig.get_session``; ``BotoCoreError`` covers
        # boto3-rejected environment shapes (e.g. ``ProfileNotFound`` from a
        # bad ``AWS_PROFILE``). Mirrors the pair caught by
        # ``AwsProviderBackend.build_provider_instance``.
        raise click.ClickException(str(e)) from e
    sg_id = client.ensure_security_group()
    logger.info("Prepared AWS security group {} in region {}", sg_id, client.region)
    write_human_line(sg_id)


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
