"""Shared fixtures for mngr_vps_docker tests.

The MngrContext-building helpers mirror the pattern used by other plugin
packages (e.g. mngr_schedule); they exist so that unit tests targeting
VpsDockerProvider machinery can construct a real provider instance
without booting the full mngr CLI surface.
"""

from collections.abc import Generator
from pathlib import Path

import pluggy
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture()
def temp_mngr_ctx(
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> Generator[MngrContext, None, None]:
    """A real MngrContext rooted in tmp_path. Owns its own ConcurrencyGroup."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    config = MngrConfig(default_host_dir=tmp_path / ".mngr")
    cg = ConcurrencyGroup(name="test")
    with cg:
        yield MngrContext(
            config=config,
            pm=plugin_manager,
            profile_dir=profile_dir,
            concurrency_group=cg,
        )
