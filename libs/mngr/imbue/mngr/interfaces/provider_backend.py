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
        """Create a configured provider instance from this backend.

        This call is always treated as a read-only-or-existing-host construction:
        backends must not silently create one-time backend-side resources here.
        If a backend has such resources (e.g. Modal's per-user environment) and
        the construction would otherwise fail because they are missing, raise
        ``ProviderEmptyError`` so read paths (``mngr list`` / ``mngr gc`` /
        discovery) can skip this provider entirely.

        Backends that need to bootstrap one-time resources for the create-host
        path override ``bootstrap_for_host_creation`` (default no-op). The
        ``mngr create`` flow calls that method before ``build_provider_instance``;
        no other call path triggers a bootstrap.
        """
        ...

    @staticmethod
    def bootstrap_for_host_creation(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> None:
        """Ensure any one-time backend resources exist before the first host create.

        Default: no-op. Most backends have nothing to bootstrap -- their
        per-host resources are created on demand inside ``build_provider_instance``
        or the provider's create-host path.

        Backends with one-time per-user resources (the Modal per-user
        environment is the motivating example) override this to create them
        if missing. Must be idempotent; the create-host path calls it
        unconditionally before building the provider instance.
        """
        # Default no-op; overrides exist on backends with one-time resources.
        del name, config, mngr_ctx
