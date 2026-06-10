"""Shared test fixtures for the mngr_claude plugin."""

from pathlib import Path
from typing import Generator

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.testing import make_mngr_ctx

# NB: mngr_claude inherits its test fixtures via register_plugin_test_fixtures
# (see the project-level conftest.py), whose plugin-facing plugin_manager loads
# every mngr entry point. There is deliberately no enabled_plugins override
# here: unlike mngr-core's blocking plugin_manager, the plugin-facing one does
# not consult enabled_plugins, so the claude / code_guardian / fixme_fairy /
# headless_claude hooks are already all loaded.


@pytest.fixture
def interactive_mngr_ctx(
    temp_config: MngrConfig, temp_profile_dir: Path, plugin_manager: pluggy.PluginManager
) -> Generator[MngrContext, None, None]:
    """Create an interactive MngrContext with a temporary host directory.

    Use this fixture when testing code paths that require is_interactive=True.
    """
    cg = ConcurrencyGroup(name="test-interactive")
    with cg:
        yield make_mngr_ctx(temp_config, plugin_manager, temp_profile_dir, is_interactive=True, concurrency_group=cg)
