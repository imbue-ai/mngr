"""AWS provider plugin entry point.

This module is the ``aws`` setuptools entry point, so it is imported for every
``mngr`` invocation while plugins are loaded. It must therefore stay free of the
heavy AWS SDK: the backend implementation (``imbue.mngr_aws.backend``, which
pulls ``boto3``/``botocore``) and the operator CLI (``imbue.mngr_aws.cli``) are
imported lazily -- only when an AWS provider is actually operated -- via the
``LazyProviderBackend`` loader and ``LazyProviderCliGroup`` below.
"""

from collections.abc import Sequence
from typing import Final

import click

from imbue.mngr.interfaces.provider_backend import LazyProviderBackend
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.click_utils import LazyProviderCliGroup
from imbue.mngr_aws import hookimpl
from imbue.mngr_aws.config import AwsProviderConfig

AWS_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("aws")

AWS_BUILD_ARGS_HELP: Final[str] = (
    "EC2-specific args (consumed by provider, not passed to docker):\n"
    "  --aws-region=REGION         Must match the provider config's default_region;\n"
    "                              the client is bound to one region at construction\n"
    "                              and refuses cross-region creates. To target multiple\n"
    "                              regions, define one [providers.aws-<region>] block\n"
    "                              per region (see mngr_aws README 'Multiple regions').\n"
    "  --aws-instance-type=TYPE    EC2 instance type (default: t3.small)\n"
    "  --aws-ami=AMI-ID            Override the per-host AMI for this create only\n"
    "                              (default: provider config's default_ami_id, or the\n"
    "                              pinned per-region default for the chosen region)\n"
    "  --aws-spot                  Run on EC2 spot capacity (presence-only flag).\n"
    "                              AWS may reclaim with ~2 min notice; the host is\n"
    "                              terminated, not stopped, on reclaim. Opt-in only.\n"
    "  --git-depth=N               Shallow-clone build context to depth N before upload\n"
    "\n"
    "All other build args are passed to 'docker build' on the EC2 instance.\n"
    "Example: -b --aws-instance-type=t3.medium -b --file=Dockerfile -b .\n"
)

AWS_START_ARGS_HELP: Final[str] = (
    "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."
)


def _load_aws_backend() -> type[ProviderBackendInterface]:
    """Import and return the AWS provider backend class (pulls the AWS SDK)."""
    from imbue.mngr_aws.backend import AwsProviderBackend

    return AwsProviderBackend


def _load_aws_cli_group() -> click.Group:
    """Import and return the ``mngr aws`` operator command group (pulls the AWS SDK)."""
    from imbue.mngr_aws.cli import aws_cli_group

    return aws_cli_group


@hookimpl
def register_provider_backend() -> LazyProviderBackend:
    """Register the AWS provider backend lazily so startup skips the AWS SDK."""
    return LazyProviderBackend(
        name=AWS_BACKEND_NAME,
        config_class=AwsProviderConfig,
        load=_load_aws_backend,
        build_args_help=AWS_BUILD_ARGS_HELP,
        start_args_help=AWS_START_ARGS_HELP,
    )


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr aws ...`` operator command group lazily."""
    return [
        LazyProviderCliGroup(
            name="aws",
            load=_load_aws_cli_group,
            help="AWS (EC2) provider operator commands.",
        )
    ]
