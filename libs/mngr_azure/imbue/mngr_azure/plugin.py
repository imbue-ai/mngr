"""Azure provider plugin entry point.

This module is the ``azure`` setuptools entry point, so it is imported for every
``mngr`` invocation while plugins are loaded. It must therefore stay free of the
heavy Azure management SDK: the backend implementation (``imbue.mngr_azure.backend``,
which pulls ``azure.mgmt.compute`` / ``azure.mgmt.network`` / ``azure.mgmt.resource``
via ``imbue.mngr_azure.client``) and the operator CLI (``imbue.mngr_azure.cli``) are
imported lazily -- only when an Azure provider is actually operated -- via the
``LazyProviderBackend`` loader and ``LazyProviderCliGroup`` below.
"""

from collections.abc import Sequence
from typing import Final

import click

from imbue.mngr.interfaces.provider_backend import LazyProviderBackend
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.click_utils import LazyProviderCliGroup
from imbue.mngr_azure import hookimpl
from imbue.mngr_azure.config import AzureProviderConfig

AZURE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("azure")

AZURE_BUILD_ARGS_HELP: Final[str] = (
    "Azure-specific args (consumed by provider, not passed to docker):\n"
    "  --azure-region=REGION       Azure region / location (default: westus)\n"
    "  --azure-vm-size=SIZE        Azure VM size (default: Standard_B2s)\n"
    "  --azure-spot                Run on Azure Spot capacity (presence-only flag).\n"
    "                              Azure may reclaim on capacity pressure; the host is\n"
    "                              deleted, not stopped, on eviction. Opt-in only.\n"
    "  --git-depth=N               Shallow-clone build context to depth N before upload\n"
    "\n"
    "All other build args are passed to 'docker build' on the VM.\n"
    "Example: -b --azure-vm-size=Standard_D2s_v5 -b --file=Dockerfile -b .\n"
)

AZURE_START_ARGS_HELP: Final[str] = (
    "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."
)


def _load_azure_backend() -> type[ProviderBackendInterface]:
    """Import and return the Azure provider backend class (pulls the Azure management SDK)."""
    from imbue.mngr_azure.backend import AzureProviderBackend

    return AzureProviderBackend


def _load_azure_cli_group() -> click.Group:
    """Import and return the ``mngr azure`` operator command group (pulls the Azure SDK)."""
    from imbue.mngr_azure.cli import azure_cli_group

    return azure_cli_group


@hookimpl
def register_provider_backend() -> LazyProviderBackend:
    """Register the Azure provider backend lazily so startup skips the Azure management SDK."""
    return LazyProviderBackend(
        name=AZURE_BACKEND_NAME,
        config_class=AzureProviderConfig,
        load=_load_azure_backend,
        build_args_help=AZURE_BUILD_ARGS_HELP,
        start_args_help=AZURE_START_ARGS_HELP,
    )


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the ``mngr azure ...`` operator command group lazily."""
    return [
        LazyProviderCliGroup(
            name="azure",
            load=_load_azure_cli_group,
            help="Azure (Virtual Machines) provider operator commands.",
        )
    ]
