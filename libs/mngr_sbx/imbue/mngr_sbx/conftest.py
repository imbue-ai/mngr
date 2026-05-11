from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_sbx.config import SbxProviderConfig
from imbue.mngr_sbx.instance import SbxProviderInstance


@pytest.fixture
def sbx_provider_config() -> SbxProviderConfig:
    """Create a default SbxProviderConfig for testing."""
    return SbxProviderConfig(
        host_dir=Path("/mngr"),
        default_idle_timeout=60,
    )


@pytest.fixture
def sbx_provider(
    temp_mngr_ctx: MngrContext,
    sbx_provider_config: SbxProviderConfig,
) -> SbxProviderInstance:
    """Create an SbxProviderInstance for unit testing.

    Does NOT probe the sbx CLI, so unit tests can run without sbx installed.
    """
    return SbxProviderInstance(
        name=ProviderInstanceName("sbx-test"),
        host_dir=sbx_provider_config.host_dir or Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=sbx_provider_config,
    )
