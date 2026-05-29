"""Tests for the CLEANING UP RESOURCES tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

_REMOTE_TIMEOUT = 120.0


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_gc_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect all unused resources
        mngr gc
    """)
    # Create a live Modal agent before running gc. This gives gc a real remote
    # provider with actual resources (a host, work directory, etc.) to scan,
    # rather than running against an empty environment where there is nothing
    # to collect. Creating a Modal agent also invokes the `modal` CLI (for
    # `modal environment create`/`modal deploy`), which is what satisfies the
    # @pytest.mark.modal resource guard: a bare `mngr gc` only reaches Modal via
    # the in-process SDK, and that path is invisible to the guard across the
    # subprocess boundary.
    expect(
        e2e.run(
            "mngr create gc-keep --provider modal --type command --no-connect --no-ensure-clean -- sleep 100600",
            comment="create a live Modal agent so gc has real resources to scan",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # garbage collect all unused resources
    gc_result = e2e.run("mngr gc", comment="garbage collect all unused resources")
    expect(gc_result).to_succeed()
    expect(gc_result.stdout).to_contain("Garbage Collection Results")

    # gc must only collect *unused* resources -- the live agent (and its Modal
    # host) must still be present afterwards.
    list_result = e2e.run("mngr list", comment="verify the live agent survived gc")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("gc-keep")


@pytest.mark.release
@pytest.mark.modal
def test_gc_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to see what would be cleaned before actually running garbage collection
        mngr gc --dry-run
    """)
    expect(e2e.run("mngr gc --dry-run", comment="dry-run before actually running gc")).to_succeed()


# gc scoped to Modal only does meaningful work once that provider has state,
# so first create a Modal agent. Creating on Modal also trackably invokes the
# Modal CLI (via environment_create during provider init), which satisfies the
# @pytest.mark.modal / @pytest.mark.rsync resource guards -- a bare
# `mngr gc --provider modal` against an empty environment skips Modal entirely
# and would never exercise it. The remote round trip (create + gc) needs more
# than the global 10s timeout, hence the explicit timeouts below.
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_gc_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # garbage collect for a specific provider only (repeatable if you want multiple providers)
        mngr gc --provider modal
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100648",
            comment="create a Modal agent so gc has a provider with state to scan",
            timeout=180.0,
        )
    ).to_succeed()
    expect(
        e2e.run("mngr gc --provider modal", comment="garbage collect for a specific provider only", timeout=90.0)
    ).to_succeed()
    # gc must leave the freshly-created, still-running agent untouched: it has a
    # live agent and is well under the minimum host age, so it is not orphaned.
    list_result = e2e.run("mngr list", comment="verify the running agent survived gc", timeout=90.0)
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_gc_background_watch(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # if you wanted, you could disable automatic garbage collection on destroy by setting the appropriate setting:
        mngr config set commands.destroy.gc false
        # then make sure you constantly run gc in the background (this runs it once every 60 seconds)
        watch -n60 mngr gc
        # this would have the effect of making your calls to "mngr destroy" somewhat faster, at the cost of needing to have this background process running
    """)
    # `mngr config set` defaults to project scope, writing to settings.toml.
    # Every config file loaded during a pytest run must opt in via
    # is_allowed_in_pytest, so pre-seed that file with the opt-in (the e2e
    # fixture only seeds settings.local.toml). Without this, the subsequent
    # `mngr gc` would refuse to load the freshly written project config.
    project_settings_path = project_config_dir / "settings.toml"
    project_settings_path.write_text("is_allowed_in_pytest = true\n")
    set_result = e2e.run(
        "mngr config set commands.destroy.gc false",
        comment="disable automatic gc on destroy",
    )
    expect(set_result).to_succeed()
    expect(set_result.stdout).to_contain("commands.destroy.gc = false")
    # Confirm the setting actually landed on disk in the project config.
    project_settings = tomllib.loads(project_settings_path.read_text())
    assert project_settings["commands"]["destroy"]["gc"] is False, (
        f"Expected commands.destroy.gc = false in {project_settings_path}, got {project_settings}"
    )
    # `watch -n60 mngr gc` would otherwise block indefinitely, so cap it with
    # `timeout`. The window must be long enough for the first `mngr gc`
    # iteration (which `watch` runs immediately) to complete a real garbage
    # collection pass: that pass is what actually exercises the provider, which
    # both reflects the tutorial's intent and satisfies the @pytest.mark.modal
    # guard that requires modal to be invoked.
    watch_result = e2e.run(
        "timeout 15 watch -n60 mngr gc || true",
        comment="run gc in the background via watch",
        timeout=45.0,
    )
    expect(watch_result).to_succeed()
