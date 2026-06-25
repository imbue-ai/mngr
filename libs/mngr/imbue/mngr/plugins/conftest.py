import pluggy
import pytest

import imbue.mngr.main
from imbue.mngr.plugins.testing import LifecycleTracker


@pytest.fixture
def lifecycle_tracker(plugin_manager: pluggy.PluginManager) -> LifecycleTracker:
    """Register a lifecycle tracker plugin and install the plugin manager as the module singleton."""
    tracker = LifecycleTracker()
    plugin_manager.register(tracker)
    imbue.mngr.main._plugin_manager_container["pm"] = plugin_manager
    return tracker
