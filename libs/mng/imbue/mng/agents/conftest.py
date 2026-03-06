from pathlib import Path
from typing import Generator

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.utils.testing import make_mng_ctx


@pytest.fixture
def interactive_mng_ctx(
    temp_config: MngConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngContext, None, None]:
    """Create an interactive MngContext with a temporary host directory.

    Use this fixture when testing code paths that require is_interactive=True.
    """
    cg = ConcurrencyGroup(name="test-interactive")
    with cg:
        yield make_mng_ctx(temp_config, plugin_manager, temp_profile_dir, is_interactive=True, concurrency_group=cg)
