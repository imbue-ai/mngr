import shutil
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.mock_provider_test import MockProviderInstance


@pytest.fixture
def gc_mock_provider(temp_host_dir: Path, temp_mngr_ctx: MngrContext) -> MockProviderInstance:
    """Create a MockProviderInstance for gc_machines tests."""
    return MockProviderInstance(
        name=ProviderInstanceName("test-provider"),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
    )


@pytest.fixture
def noop_binary() -> str:
    """A cross-platform path to a no-op binary that accepts any arguments.

    Use this as a fake mngr_binary for AgentObserver tests. On macOS /bin/true
    does not exist (it lives at /usr/bin/true), so shutil.which() finds the
    correct path on any platform.
    """
    path = shutil.which("true")
    assert path is not None, "Could not find 'true' binary on this system"
    return path
