"""Tests for listing agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# `mngr list` initializes the Modal provider, which discovers remote hosts over
# the network (gRPC). That routinely runs longer than the global 10s per-test
# timeout, so every test here overrides it.
#
# These tests deliberately do NOT carry @pytest.mark.modal. The Modal resource
# guard only tracks the `modal` CLI binary (via a PATH wrapper), but `mngr list`
# runs in a subprocess and reaches Modal exclusively through the Python SDK's
# gRPC client -- it never shells out to the `modal` CLI (that only happens on the
# create-host path, via `modal environment create`). With the mark, the guard
# fails the test with "marked with @pytest.mark.modal but never invoked modal".
# The @pytest.mark.release mark already restricts these to the release CI (which
# has Modal credentials), so the modal mark is redundant as well as harmful. See
# the discussion in test_create_modal.py for the create-path counterpart.
_LIST_TIMEOUT = 120.0


# No @pytest.mark.modal here: with no agents, the Modal environment does not
# exist yet and `mngr list` is not allowed to create it, so the Modal provider
# load short-circuits via the SDK (app_lookup raises ProviderEmptyError) without
# ever shelling out to the `modal` CLI binary. The resource guard tracks the CLI
# binary via a PATH wrapper, so adding the mark would trip its "marked with
# modal but never invoked modal" check. (Contrast test_create_modal.py, where
# `mngr create --provider modal` does invoke the Modal CLI to create the env.)
@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all agents
        mngr list
    """)
    result = e2e.run("mngr list", comment="List agents in a fresh environment", timeout=_LIST_TIMEOUT)
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # output all objects as one big JSON array when complete  (useful for scripting)
    mngr list --format json
    """)
    result = e2e.run(
        "mngr list --format json",
        comment="output all objects as one big JSON array when complete  (useful for scripting)",
        timeout=_LIST_TIMEOUT,
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []
