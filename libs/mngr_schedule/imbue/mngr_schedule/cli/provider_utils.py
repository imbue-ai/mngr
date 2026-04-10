import click

from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_modal.instance import ModalProviderInstance


def load_schedule_provider(
    provider_name: str,
    mngr_ctx: MngrContext,
) -> LocalProviderInstance | ModalProviderInstance:
    """Load and validate a provider instance for schedule commands.

    Raises click.ClickException if the provider cannot be loaded or is
    not a supported schedule provider (local or modal).
    """
    try:
        provider = get_provider_instance(ProviderInstanceName(provider_name), mngr_ctx)
    except MngrError as e:
        raise click.ClickException(f"Failed to load provider '{provider_name}': {e}") from e

    if not isinstance(provider, (LocalProviderInstance, ModalProviderInstance)):
        raise click.ClickException(
            f"Provider '{provider_name}' (type {type(provider).__name__}) is not supported for schedules. "
            "Supported providers: local, modal."
        )
    return provider
