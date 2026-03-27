from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName


class ProviderBackendInterface(MutableModel, ABC):
    """Interface for provider backends.

    Provider backends are stateless factories that create provider instances.
    All methods are static since backends have no instance state.
    """

    @staticmethod
    @abstractmethod
    def get_name() -> ProviderBackendName:
        """Return the unique name identifier for this provider backend."""
        ...

    @staticmethod
    @abstractmethod
    def get_description() -> str:
        """Return a human-readable description of what this provider backend does."""
        ...

    @staticmethod
    @abstractmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        """Return the configuration class for this provider backend."""
        ...

    @staticmethod
    @abstractmethod
    def get_build_args_help() -> str:
        """Return help text explaining what build arguments are supported."""
        ...

    @staticmethod
    @abstractmethod
    def get_start_args_help() -> str:
        """Return help text explaining what start arguments are supported."""
        ...

    @staticmethod
    @abstractmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Create a configured provider instance from this backend."""
        ...
