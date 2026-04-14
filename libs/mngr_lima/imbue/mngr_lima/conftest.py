from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.instance import LimaProviderInstance


@pytest.fixture
def lima_provider_config() -> LimaProviderConfig:
    """Create a default LimaProviderConfig for testing."""
    return LimaProviderConfig(
        host_dir=Path("/mngr"),
        default_idle_timeout=60,
    )


@pytest.fixture
def lima_provider(
    temp_mngr_ctx: MngrContext,
    lima_provider_config: LimaProviderConfig,
) -> LimaProviderInstance:
    """Create a LimaProviderInstance for unit testing.

    This does NOT check for limactl installation, so unit tests
    can run without Lima installed.
    """
    return LimaProviderInstance(
        name=ProviderInstanceName("lima-test"),
        host_dir=lima_provider_config.host_dir or Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=lima_provider_config,
    )
