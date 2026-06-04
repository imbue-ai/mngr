"""Tests for the CLEANING UP RESOURCES tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.modal
def test_gc_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect all unused resources
        mngr gc
    """)
    expect(e2e.run("mngr gc", comment="garbage collect all unused resources")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_gc_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to see what would be cleaned before actually running garbage collection
        mngr gc --dry-run
    """)
    expect(e2e.run("mngr gc --dry-run", comment="dry-run before actually running gc")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_gc_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect for a specific provider only (repeatable if you want multiple providers)
        mngr gc --provider modal
    """)
    expect(e2e.run("mngr gc --provider modal", comment="garbage collect for a specific provider only")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_gc_background_watch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you wanted, you could disable automatic garbage collection on destroy by setting the appropriate setting:
        mngr config set commands.destroy.gc false
        # then make sure you constantly run gc in the background (this runs it once every 60 seconds)
        watch -n60 mngr gc
        # this would have the effect of making your calls to "mngr destroy" somewhat faster, at the cost of needing to have this background process running
    """)
    expect(
        e2e.run(
            "mngr config set commands.destroy.gc false",
            comment="disable automatic gc on destroy",
        )
    ).to_succeed()
    # `watch -n60 mngr gc` would block indefinitely; cap it with `timeout 1`
    # so the test only confirms watch can start.
    expect(
        e2e.run(
            "timeout 1 watch -n60 mngr gc || true",
            comment="run gc in the background via watch",
        )
    ).to_succeed()
