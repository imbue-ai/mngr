"""Shared test backend that counts its instantiations.

Used to assert that code paths don't eagerly instantiate unrelated provider
backends. Lives in a mock_*_test.py file so other test modules can import it
without running into the ratchet against inline imports.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import ClassVar

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.config.provider_config_registry import _provider_config_registry
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.mock_provider_test import MockProviderInstance
from imbue.mngr.providers.registry import _backend_registry

TRACKING_BACKEND_NAME = ProviderBackendName("test-tracking-backend")


class TrackingBackend(ProviderBackendInterface):
    """Backend whose build_provider_instance increments a class-level counter."""

    build_count: ClassVar[int] = 0

    @staticmethod
    def get_name() -> ProviderBackendName:
        return TRACKING_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Counts instantiations"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ProviderInstanceConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def get_start_args_help() -> str:
        return "No arguments supported."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        TrackingBackend.build_count += 1
        return MockProviderInstance(
            name=name,
            host_dir=mngr_ctx.config.default_host_dir,
            mngr_ctx=mngr_ctx,
        )


@contextmanager
def tracking_backend_registered() -> Generator[None, None, None]:
    """Register the tracking backend and reset its counter."""
    _backend_registry[TRACKING_BACKEND_NAME] = TrackingBackend
    _provider_config_registry[TRACKING_BACKEND_NAME] = ProviderInstanceConfig
    TrackingBackend.build_count = 0
    try:
        yield
    finally:
        del _backend_registry[TRACKING_BACKEND_NAME]
        del _provider_config_registry[TRACKING_BACKEND_NAME]
