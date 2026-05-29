"""Tests for the plugin management commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all available plugins
        mngr plugin list
    """)
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("claude")
