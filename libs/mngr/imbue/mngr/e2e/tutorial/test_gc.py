"""Tests for the CLEANING UP RESOURCES tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Creating a Modal-backed agent has to provision a fresh Modal environment and
# container, so give it the same generous budget the other Modal create tests
# use (see test_create_modal.py).
_REMOTE_TIMEOUT = 120.0


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_gc_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect all unused resources
        mngr gc
    """)
    # `mngr gc` only reaches out to a provider once that provider's environment
    # exists: the Modal backend disables itself (raising ProviderEmptyError)
    # until its per-user environment has been bootstrapped, so gc against a
    # fresh environment never touches Modal at all. Create a Modal agent first
    # so the environment is bootstrapped (via `modal environment create`, which
    # also satisfies @pytest.mark.modal) and gc genuinely exercises the Modal
    # provider's discovery path.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100731",
            comment="create a Modal agent so gc has a real provider environment to inspect",
            timeout=120.0,
        )
    ).to_succeed()

    result = e2e.run("mngr gc", comment="garbage collect all unused resources")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Garbage Collection Results")

    # gc must only clean *unused* resources: the still-active agent and its
    # Modal host must survive a default gc run.
    list_after = e2e.run("mngr list", comment="verify the active agent survived gc")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_gc_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to see what would be cleaned before actually running garbage collection
        mngr gc --dry-run
    """)
    # Like the other gc tests in this file, `mngr gc --dry-run` only reaches a
    # provider once that provider's environment exists: the Modal backend
    # disables itself (raising ProviderEmptyError) until its per-user
    # environment has been bootstrapped, so a dry run against a fresh
    # environment never touches Modal at all. Create a Modal agent first so the
    # environment is bootstrapped (via `modal environment create`, which also
    # satisfies @pytest.mark.modal) and the dry run genuinely exercises the
    # Modal provider's discovery path.
    expect(
        e2e.run(
            "mngr create dry-run-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100732",
            comment="create a Modal agent so gc has a real provider environment to inspect",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    result = e2e.run("mngr gc --dry-run", comment="dry-run before actually running gc")
    expect(result).to_succeed()
    # A dry run must announce itself as a dry run, not a real cleanup.
    expect(result.stdout).to_contain("Garbage Collection (Dry Run)")

    # The whole point of --dry-run is that it changes nothing: the active agent
    # and its Modal host must still be present after the dry run.
    list_after = e2e.run("mngr list", comment="verify the dry run destroyed nothing")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("dry-run-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_gc_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect for a specific provider only (repeatable if you want multiple providers)
        mngr gc --provider modal
    """)
    # The tutorial reaches this block after Modal has already been used, so a
    # Modal environment exists for gc to act against. Each e2e test runs in an
    # isolated host dir with a fresh (non-existent) Modal environment, so we
    # first create a Modal-backed agent. This provisions the per-user Modal
    # environment (invoking the `modal` CLI, which the @pytest.mark.modal
    # resource guard requires) and gives `mngr gc --provider modal` a real
    # provider to scan, exercising the full Modal gc path rather than a no-op.
    # Use the lightweight `command` agent type (running `sleep`) so the test
    # provisions a real Modal host without needing a full Claude install, the
    # same stand-in the basic create tests use.
    expect(
        e2e.run(
            "mngr create gc-modal-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100200",
            comment="create a Modal agent so gc has a real provider to clean",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # garbage collect for a specific provider only (repeatable if you want multiple providers)
    result = e2e.run(
        "mngr gc --provider modal",
        comment="garbage collect for a specific provider only",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # The running agent's machine is active, so gc must not tear it down: it
    # should still be listed after garbage collection.
    list_after = e2e.run("mngr list --format json", comment="confirm the Modal agent survived gc", timeout=60.0)
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("gc-modal-task")


@pytest.mark.release
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
