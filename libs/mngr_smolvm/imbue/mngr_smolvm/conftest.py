from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.instance import SmolvmProviderInstance


@pytest.fixture
def smolvm_provider_config() -> SmolvmProviderConfig:
    """Create a default SmolvmProviderConfig for testing."""
    return SmolvmProviderConfig(
        host_dir=Path("/mngr"),
        default_idle_timeout=60,
    )


@pytest.fixture
def smolvm_provider(
    temp_mngr_ctx: MngrContext,
    smolvm_provider_config: SmolvmProviderConfig,
) -> SmolvmProviderInstance:
    """Create a SmolvmProviderInstance for unit testing.

    This does NOT check for smolvm installation, so unit tests
    can run without smolvm installed.
    """
    return SmolvmProviderInstance(
        name=ProviderInstanceName("smolvm-test"),
        host_dir=smolvm_provider_config.host_dir or Path("/mngr"),
        mngr_ctx=temp_mngr_ctx,
        config=smolvm_provider_config,
    )
