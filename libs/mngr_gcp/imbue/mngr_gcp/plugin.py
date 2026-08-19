"""GCP provider plugin entry point.

This module is the ``gcp`` setuptools entry point, so it is imported for every
``mngr`` invocation while plugins are loaded. It must therefore stay free of the
heavy Google Cloud SDK: the backend implementation (``imbue.mngr_gcp.backend``,
which pulls ``google.cloud.compute``) and the operator CLI (``imbue.mngr_gcp.cli``)
are imported lazily -- only when a GCP provider is actually operated -- via the
``LazyProviderBackend`` loader and ``LazyProviderCliGroup`` below.
"""

from collections.abc import Sequence
from typing import Final

import click

from imbue.mngr.interfaces.provider_backend import LazyProviderBackend
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.click_utils import LazyProviderCliGroup
from imbue.mngr_gcp import hookimpl
from imbue.mngr_gcp.config import GcpProviderConfig

GCP_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("gcp")

GCP_BUILD_ARGS_HELP: Final[str] = (
    "GCE-specific args (consumed by provider, not passed to docker):\n"
    "  --gcp-zone=ZONE          GCE zone, e.g. us-west1-a (GCE VMs are zonal; must equal\n"
    "                           the provider's configured zone; defaults to the config's\n"
    "                           default_zone, the active gcloud compute/zone, or us-west1-a)\n"
    "  --gcp-machine-type=TYPE  GCE machine type (default: e2-small)\n"
    "  --gcp-image=IMAGE        GCE boot-disk source image for this host, overriding the\n"
    "                           config's default_source_image (a full image / family URL)\n"
    "  --gcp-spot               Run on GCE Spot capacity (presence-only flag; preemptible).\n"
    "  --git-depth=N            Shallow-clone build context to depth N before upload\n"
    "\n"
    "When --gcp-image is omitted the VM image is taken from the provider config\n"
    "(default_source_image).\n"
    "\n"
    "All other build args are passed to 'docker build' on the GCE instance.\n"
    "Example: -b --gcp-machine-type=e2-medium -b --file=Dockerfile -b .\n"
)

GCP_START_ARGS_HELP: Final[str] = (
    "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."
)


def _load_gcp_backend() -> type[ProviderBackendInterface]:
    """Import and return the GCP provider backend class (pulls the Google Cloud SDK)."""
    from imbue.mngr_gcp.backend import GcpProviderBackend

    return GcpProviderBackend


def _load_gcp_cli_group() -> click.Group:
    """Import and return the ``mngr gcp`` operator command group (pulls the Google Cloud SDK)."""
    from imbue.mngr_gcp.cli import gcp_cli_group

    return gcp_cli_group


@hookimpl
def register_provider_backend() -> LazyProviderBackend:
    """Register the GCP provider backend lazily so startup skips the Google Cloud SDK."""
    return LazyProviderBackend(
        name=GCP_BACKEND_NAME,
        config_class=GcpProviderConfig,
        load=_load_gcp_backend,
        build_args_help=GCP_BUILD_ARGS_HELP,
        start_args_help=GCP_START_ARGS_HELP,
    )


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr gcp ...`` operator command group lazily."""
    return [
        LazyProviderCliGroup(
            name="gcp",
            load=_load_gcp_cli_group,
            help="GCP (Google Compute Engine) provider operator commands.",
        )
    ]
