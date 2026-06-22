"""Tests for listing agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.resource_guards.resource_guards import enforce_sdk_guard
from imbue.skitwright.expect import expect


def _record_subprocess_modal_usage() -> None:
    """Register Modal usage that happened inside an ``mngr`` subprocess with the resource guard.

    The e2e tests run ``mngr`` as a subprocess, and that is where the real Modal
    SDK calls happen: with remote providers enabled, ``mngr list`` (and friends)
    runs the full provider-discovery path, which makes an authenticated Modal SDK
    call to look up this installation's Modal environment. The resource guard's
    Modal SDK monkeypatch only observes *in-process* calls, so it cannot see the
    subprocess's Modal usage and would otherwise fail ``@pytest.mark.modal`` as
    "never invoked". Record the usage explicitly from the test process so the mark
    reflects reality -- the same approach the lima release test uses to satisfy its
    binary-only guard (see ``mngr_lima/.../test_lima_btrfs_release.py``).
    """
    enforce_sdk_guard("modal")


@pytest.mark.release
# `mngr list` runs the full provider-discovery path (an authenticated Modal lookup
# plus Docker/Vultr probes), which routinely takes more than the default 10s
# per-test timeout. The release CI lane overrides this globally to 90s, but set an
# explicit per-test timeout so the test also passes when run with the default ini
# timeout (e.g. locally) -- the same approach test_list_local_filter uses.
@pytest.mark.timeout(60)
def test_list_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all agents
        mngr list
    """)
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
# `mngr list --format json` runs the full provider-discovery path (an
# authenticated Modal lookup plus a local/Docker probe) on top of mngr's CLI
# startup, which together routinely exceed both the default 10s per-test timeout
# and the default 30s per-command timeout. The release CI lane overrides the
# pytest timeout globally to 90s; set both explicitly so the test is robust when
# run on its own.
@pytest.mark.timeout(120)
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output all objects as one big JSON array when complete  (useful for scripting)
        mngr list --format json
    """)
    result = e2e.run(
        "mngr list --format json",
        comment="output all objects as one big JSON array when complete  (useful for scripting)",
        timeout=90.0,
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []
    # `mngr list` makes its authenticated Modal lookup via the in-process SDK
    # inside the `mngr` subprocess, which the resource guard's monkeypatch cannot
    # observe across the process boundary. Record it explicitly so the
    # @pytest.mark.modal guard reflects the Modal usage that actually happened
    # (mirrors test_list_local_filter).
    _record_subprocess_modal_usage()


# No @pytest.mark.modal: `mngr ls` is the short alias for `mngr list`, and in a
# fresh environment the Modal environment does not exist yet, so the Modal
# provider raises ProviderEmptyError and is skipped before it ever shells out to
# the `modal` CLI -- the only Modal usage the resource guard can observe across
# the e2e subprocess boundary. Marking this @pytest.mark.modal would therefore
# trip the guard's "marked but never invoked" check (the same reasoning as
# test_list_active_filter / test_list_stopped_filter).
@pytest.mark.release
def test_list_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr ls
    """)
    result = e2e.run("mngr ls", comment="short form")
    expect(result).to_succeed()
    # `mngr ls` is the alias for `mngr list`, so it must actually perform a
    # listing rather than just exit 0. In this fresh environment that means the
    # same empty-listing output `mngr list` produces (see test_list_with_no_agents).
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
# This test creates two command agents and then runs exec, stop, and a
# local-scoped `mngr list` -- five commands, each well past the default 10s
# per-test timeout. Like the other agent-creating list tests
# (test_list_remote_filter, test_list_limit), it needs an explicit override.
@pytest.mark.timeout(180)
def test_list_running_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only running agents
        mngr list --running
    """)
    # Give the --running filter something to include and something to exclude.
    # Pin a unique sleep value per agent so leaked processes trace back to the
    # specific create call.
    for name, sleep_seconds in [("running-agent", 100201), ("stopped-agent", 100202)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"create {name}",
            )
        ).to_succeed()
    # A freshly created command agent sits in WAITING: its process is alive but
    # there is no "active" marker, which real agent integrations create while
    # doing work. Create the marker through the public exec interface so mngr
    # reports the agent as RUNNING -- the exact state the --running filter keeps.
    expect(
        e2e.run(
            "mngr exec running-agent 'touch \"$MNGR_AGENT_STATE_DIR/active\"'",
            comment="mark running-agent as actively running",
        )
    ).to_succeed()
    expect(e2e.run("mngr stop stopped-agent", comment="stop the other agent")).to_succeed()

    # show only running agents. Scope discovery to the local provider: the test's
    # agents are local, so this still exercises the --running filter exactly, while
    # avoiding an all-provider listing that would abort on any registered-but-
    # unconfigured remote backend (e.g. AWS without credentials -- the monorepo
    # registers every backend via `uv sync --all-packages`). That abort is
    # environment-dependent noise unrelated to what this test verifies. Same
    # local-scoping rationale as test_list_pipe_stdin's helper lookups.
    result = e2e.run(
        "mngr list --running --provider local --format json",
        comment="show only running agents",
    )
    expect(result).to_succeed()
    running_names = [agent["name"] for agent in json.loads(result.stdout)["agents"]]
    assert "running-agent" in running_names, running_names
    assert "stopped-agent" not in running_names, running_names

    # Confirm the --running filter actually discriminates, rather than the stopped
    # agent simply having vanished: an unfiltered local listing must still include
    # *both* agents, and stopped-agent must be reported in a non-RUNNING state. This
    # proves the agent above was filtered out by --running, not destroyed by stop.
    unfiltered = e2e.run("mngr list --provider local --format json", comment="all local agents, unfiltered")
    expect(unfiltered).to_succeed()
    by_name = {agent["name"]: agent for agent in json.loads(unfiltered.stdout)["agents"]}
    assert "running-agent" in by_name, by_name
    assert "stopped-agent" in by_name, by_name
    assert by_name["running-agent"]["state"] == "RUNNING", by_name["running-agent"]
    assert by_name["stopped-agent"]["state"] != "RUNNING", by_name["stopped-agent"]


@pytest.mark.release
# `mngr list` runs the full provider-discovery path (an authenticated Modal lookup
# plus Docker/Vultr probes), which routinely takes ~10s -- past the default 10s
# per-test timeout. The release CI lane already overrides this globally to 90s.
@pytest.mark.timeout(60)
def test_list_stopped_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only stopped agents (not running, still exists and can be restarted)
        mngr list --stopped
    """)
    # Intentionally NOT marked @pytest.mark.modal: in an isolated, empty
    # environment `mngr list --stopped` discovers via the provider SDKs (Modal
    # gRPC) and never shells out to the `modal` CLI binary, which is the only
    # Modal usage the resource guard can observe across the mngr subprocess
    # boundary. With the mark, the guard flags it as a never-invoked resource.
    result = e2e.run("mngr list --stopped", comment="show only stopped agents")
    expect(result).to_succeed()
    # The fresh test environment has no agents, so the --stopped filter must
    # produce an empty listing rather than just exiting cleanly.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
# This test runs several `mngr list` calls (each running the full provider
# discovery path, including an authenticated Modal lookup) plus an agent
# creation, which together take well past the default 10s per-test timeout. The
# release CI lane overrides the timeout globally (offload runs with
# --timeout=900), but the default applies when the test is run directly, so pin
# a generous per-test budget. Note timeout_func_only is on, so this covers only
# the test body, not fixture setup/teardown.
@pytest.mark.timeout(180)
def test_list_archived_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only archived agents (stopped, cannot necessarily be restarted, but data can be inspected)
        mngr list --archived
    """)
    result = e2e.run("mngr list --archived", comment="show only archived agents")
    expect(result).to_succeed()
    # In a fresh environment nothing has been archived, so the filter must
    # return an empty set rather than every agent or an error.
    expect(result.stdout).to_contain("No agents found")

    # Verify the underlying `has(labels.archived_at)` CEL filter actually
    # compiles and applies cleanly (not just that the command exits 0): a
    # well-formed JSON listing with an empty agents array (no agent is archived
    # in a fresh environment). We do not assert on `errors`, which can carry
    # benign per-provider discovery notes (e.g. an unavailable Docker daemon)
    # that depend on the machine running the test.
    json_result = e2e.run("mngr list --archived --format json", comment="show only archived agents (JSON)")
    expect(json_result).to_succeed()
    parsed = json.loads(json_result.stdout)
    assert parsed["agents"] == []

    # Discrimination check: prove the filter actually *excludes* non-archived
    # agents rather than always returning an empty set (which the empty-env
    # assertions above cannot distinguish). Create a cheap local command agent;
    # it has no `archived_at` label, so it must be absent from --archived while
    # remaining visible in an unfiltered listing. Pin a unique sleep value so a
    # leaked process traces back to this create.
    expect(
        e2e.run(
            "mngr create archived-probe --transfer=none --type command --no-connect --no-ensure-clean -- sleep 100204",
            comment="create a non-archived agent to verify --archived excludes it",
        )
    ).to_succeed()

    archived_after = e2e.run(
        "mngr list --archived --format json",
        comment="a freshly created (non-archived) agent is still excluded by --archived",
    )
    expect(archived_after).to_succeed()
    archived_names = [agent["name"] for agent in json.loads(archived_after.stdout)["agents"]]
    assert archived_names == [], archived_names

    # Sanity: the same agent *is* present in an unfiltered listing, confirming it
    # was actually created and that --archived (not an empty environment) is what
    # filters it out.
    all_after = e2e.run("mngr list --format json", comment="the new agent is visible without the --archived filter")
    expect(all_after).to_succeed()
    all_names = [agent["name"] for agent in json.loads(all_after.stdout)["agents"]]
    assert "archived-probe" in all_names, all_names


@pytest.mark.release
# `mngr list` runs the full provider-discovery path (Docker/Vultr probes plus a
# local scan), which routinely takes longer than the default 10s per-test
# timeout. The release CI lane already overrides this globally to 90s, but the
# explicit mark keeps the test robust when run on its own.
@pytest.mark.timeout(60)
def test_list_active_filter(e2e: E2eSession) -> None:
    # No @pytest.mark.modal: in a fresh environment there are no agents and the
    # Modal environment does not exist yet, so `mngr list` deliberately skips the
    # modal provider (ProviderEmptyError) instead of creating an environment. It
    # therefore never invokes the `modal` CLI -- the only Modal usage the resource
    # guard can observe across the e2e subprocess boundary -- so marking the test
    # @pytest.mark.modal would trip the guard's "marked but never invoked" check.
    e2e.write_tutorial_block("""
        # show only active agents (anything not archived/destroyed/crashed/failed)
        mngr list --active
    """)
    result = e2e.run("mngr list --active", comment="show only active agents")
    expect(result).to_succeed()
    # With no agents, the active filter should report an empty list rather than
    # surfacing any agents (verifies the command actually ran the filter, not just
    # that it exited 0).
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
# No @pytest.mark.modal: in a fresh environment there are no agents and the Modal
# environment does not exist yet, so the `MNGR__COMMANDS__LIST__ACTIVE=false mngr
# list` below deliberately skips the modal provider (ProviderEmptyError) instead of
# creating an environment. It therefore never invokes the `modal` CLI -- the only
# Modal usage the resource guard can observe across the e2e subprocess boundary --
# so marking the test @pytest.mark.modal would trip the guard's "marked but never
# invoked" check (matching test_list_active_filter / test_list_stopped_filter).
#
# That `mngr list` still runs the full provider-discovery path (an authenticated
# Modal lookup plus Docker probes) and routinely takes ~10s -- past the default 10s
# per-test timeout. The release CI lane already overrides this globally to 90s.
@pytest.mark.timeout(60)
def test_config_set_list_active_default(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # you can make any of those filters the default for "mngr list" by setting it in your config.
        # for example, to hide agents from dead/destroyed hosts by default:
        mngr config set commands.list.active true
        # to opt out for a single call, override the env var: MNGR__COMMANDS__LIST__ACTIVE=false mngr list
    """)
    # The final `mngr list` below loads the project settings.toml that `config set`
    # writes here. Under pytest, every loaded config file must opt in via
    # is_allowed_in_pytest = true, so seed the project file with that opt-in up
    # front. `config set` loads the existing file with tomlkit and preserves its
    # keys, so the opt-in survives the write (and is_allowed_in_pytest is a
    # test-only field that real users never set, so this does not alter the
    # user-facing behavior under test).
    settings_path = project_config_dir / "settings.toml"
    settings_path.write_text("is_allowed_in_pytest = true\n")

    expect(
        e2e.run(
            "mngr config set commands.list.active true",
            comment="make active filter the default for mngr list",
        )
    ).to_succeed()
    # Verify the set actually persisted the default into the project config,
    # which is the whole point of the tutorial block -- not merely that the set
    # command exited 0. Read the project settings file back the way a human would
    # when debugging (the value lands under the [commands.list] table as
    # active = true).
    settings = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml",
        comment="confirm the active default was written to the project config",
    )
    expect(settings).to_succeed()
    expect(settings.stdout).to_contain("active = true")
    expect(
        e2e.run(
            "MNGR__COMMANDS__LIST__ACTIVE=false mngr list",
            comment="opt out for a single call via env var override",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
# `mngr list` runs the full provider-discovery path (an authenticated Modal lookup
# plus Docker/Vultr probes), which routinely takes ~10s -- past the default 10s
# per-test timeout. The release CI lane already overrides this globally to 90s.
@pytest.mark.timeout(60)
def test_list_local_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only agents running locally
        mngr list --local
    """)
    result = e2e.run("mngr list --local", comment="show only agents running locally")
    expect(result).to_succeed()
    # No agents exist in the fresh environment, and --local restricts the output
    # to local-provider agents, so nothing should be listed.
    expect(result.stdout).to_contain("No agents found")
    _record_subprocess_modal_usage()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_remote_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only agents running remotely
        mngr list --remote
    """)
    # Create a cheap local agent (a `sleep` command, no remote provider) so we
    # can verify that --remote actually discriminates: a local agent must never
    # appear in the remote-only listing.
    expect(
        e2e.run(
            "mngr create local-task --transfer=none --type command --no-ensure-clean -- sleep 100129",
            comment="create a local agent to verify the --remote filter excludes it",
        )
    ).to_succeed()

    # The tutorial command: show only agents running remotely.
    remote_result = e2e.run("mngr list --remote", comment="show only agents running remotely")
    expect(remote_result).to_succeed()

    # The local agent must be filtered out of the remote-only listing.
    remote_json = e2e.run("mngr list --remote --format json", comment="remote-only listing as JSON")
    expect(remote_json).to_succeed()
    remote_agents = json.loads(remote_json.stdout)["agents"]
    assert all(agent["name"] != "local-task" for agent in remote_agents), (
        f"--remote should exclude the local agent, but it appeared: {remote_agents}"
    )

    # Sanity check: the same agent *is* visible under --local, confirming the
    # filter discriminates by host provider rather than just hiding everything.
    local_json = e2e.run("mngr list --local --format json", comment="local-only listing as JSON")
    expect(local_json).to_succeed()
    local_agents = json.loads(local_json.stdout)["agents"]
    assert any(agent["name"] == "local-task" for agent in local_agents), (
        f"--local should include the local agent, but it was missing: {local_agents}"
    )


@pytest.mark.release
# `mngr list --provider modal` runs the full provider-discovery path, including an
# authenticated Modal lookup, which routinely takes longer than the default 10s
# per-test timeout. The release CI lane already overrides this globally to 90s.
@pytest.mark.timeout(60)
def test_list_provider_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by provider
        mngr list --provider modal
    """)
    result = e2e.run("mngr list --provider modal", comment="filter by provider")
    expect(result).to_succeed()
    # In a fresh environment there are no agents, and the Modal backend is
    # skipped entirely when its per-user environment does not exist yet, so the
    # provider-filtered listing comes back empty rather than erroring.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
# An unknown provider name matches no providers, so `list_provider_names_to_load`
# returns nothing and there is zero discovery work -- but the single `mngr`
# invocation still pays the full process startup cost (~10s), which alone exceeds
# the 10s default per-test timeout. The release CI lane overrides this globally to
# 90s; set an explicit marker so the test also passes when run on its own (the
# same approach `test_list_local_filter` documents).
@pytest.mark.timeout(60)
def test_list_unknown_provider_filter(e2e: E2eSession) -> None:
    # Shares the `mngr list --provider <name>` tutorial block above, exercising
    # the unhappy path: an unknown provider name matches no configured providers
    # or backends, so the listing succeeds with an empty result instead of
    # raising an error.
    result = e2e.run(
        "mngr list --provider does-not-exist",
        comment="filtering by an unknown provider yields an empty listing",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")

    # Verify the actual contract directly: an unknown provider name is not an
    # error condition, it simply selects no providers. The JSON listing must
    # therefore carry both an empty agents list AND an empty errors list -- not
    # just an empty human-readable table. (A bug that turned an unknown provider
    # into a per-provider failure would still print "No agents found" while
    # populating errors, so the human-output check alone cannot catch it.)
    json_result = e2e.run(
        "mngr list --provider does-not-exist --format json",
        comment="an unknown provider is not an error: empty agents and no errors",
    )
    expect(json_result).to_succeed()
    parsed = json.loads(json_result.stdout)
    assert parsed["agents"] == [], parsed
    assert parsed["errors"] == [], parsed


@pytest.mark.release
# `mngr list` runs the full provider-discovery path (an authenticated Modal
# lookup), which routinely takes ~10s -- past the default 10s per-test timeout.
# The release CI lane already overrides this globally to 90s; the explicit mark
# keeps the test green when it is run with the default timeout as well.
@pytest.mark.timeout(60)
def test_list_project_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by project
        mngr list --project my-project
    """)
    result = e2e.run("mngr list --project my-project", comment="filter by project")
    expect(result).to_succeed()
    # No agents exist in this fresh environment, so the filtered listing must be
    # empty. Asserting on the rendered output (not just the exit code) confirms
    # the --project filter parsed and executed cleanly rather than erroring or
    # printing a traceback while still exiting 0.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_project_filter_matches(e2e: E2eSession) -> None:
    # Shares the `mngr list --project` tutorial block above, exercising the happy
    # path: an agent created with an explicit project label is included by
    # `--project <that label>` and excluded by `--project <other label>`. This
    # proves the filter discriminates by labels.project rather than matching every
    # agent (or none), which the empty-environment test above cannot show.
    e2e.write_tutorial_block("""
        # filter by project
        mngr list --project my-project
    """)
    # Create a cheap local agent (a `sleep` command, --transfer=none so no rsync)
    # tagged with project=my-project. --project sets the agent's `project` label
    # directly, overriding the git-remote/folder-name derivation. The pinned sleep
    # value lets any leaked process be traced back to this call.
    expect(
        e2e.run(
            "mngr create project-agent --project my-project --transfer=none --type command"
            " --no-ensure-clean --no-connect -- sleep 100622",
            comment="create a local agent tagged with project=my-project",
        )
    ).to_succeed()

    # The matching project includes the agent.
    matching = e2e.run(
        "mngr list --project my-project --format json",
        comment="filter by the agent's project",
    )
    expect(matching).to_succeed()
    matching_agents = json.loads(matching.stdout)["agents"]
    matching_names = [agent["name"] for agent in matching_agents]
    assert "project-agent" in matching_names, matching_names
    # The included agent really carries the project label we filtered on.
    matched = next(agent for agent in matching_agents if agent["name"] == "project-agent")
    assert matched["labels"]["project"] == "my-project", matched["labels"]

    # A different project excludes it, confirming the filter discriminates by the
    # project label rather than listing everything.
    other = e2e.run(
        "mngr list --project other-project --format json",
        comment="filter by a different project",
    )
    expect(other).to_succeed()
    other_names = [agent["name"] for agent in json.loads(other.stdout)["agents"]]
    assert "project-agent" not in other_names, other_names


@pytest.mark.release
# A `--label` filter does not restrict the provider, so `mngr list` runs the full
# provider-discovery path (an authenticated Modal lookup plus the other enabled
# backends), which routinely takes well over the default 10s per-test timeout.
# The release CI lane already overrides this globally to 90s; set it explicitly
# here so the test is robust when run with the default timeout too.
@pytest.mark.timeout(60)
def test_list_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    result = e2e.run("mngr list --label TEAM=backend", comment="filter by agent label")
    expect(result).to_succeed()
    # No agents exist, so the label filter matches nothing and lists no agents.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_label_filter_discriminates(e2e: E2eSession) -> None:
    # Shares the `mngr list --label TEAM=backend` tutorial block, exercising the
    # populated happy path: with real agents present, the --label shorthand must
    # keep only the agent whose label matches and drop the rest. (The CEL
    # `--include` form is covered separately in test_labels.py; this verifies the
    # distinct --label KEY=VALUE shorthand actually filters rather than just
    # parsing cleanly on an empty environment.)
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    # Two local command agents with different TEAM labels give the filter
    # something to both match and exclude. These never invoke the Modal CLI
    # (local provider, no Modal state), so the test must not carry
    # @pytest.mark.modal. Pin a unique sleep value per agent so any leaked
    # process traces back to the specific create call.
    expect(
        e2e.run(
            "mngr create label-backend --type command --no-ensure-clean --no-connect --label TEAM=backend -- sleep 100204",
            comment="create an agent labeled TEAM=backend",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create label-frontend --type command --no-ensure-clean --no-connect --label TEAM=frontend -- sleep 100205",
            comment="create an agent labeled TEAM=frontend",
        )
    ).to_succeed()

    # The tutorial command: filter by agent label.
    result = e2e.run("mngr list --label TEAM=backend", comment="filter by agent label")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("label-backend")
    expect(result.stdout).not_to_contain("label-frontend")

    # Verify the discrimination directly on structured output: exactly the
    # backend-labeled agent survives the filter.
    json_result = e2e.run("mngr list --label TEAM=backend --format json", comment="filter by agent label (JSON)")
    expect(json_result).to_succeed()
    matched_names = [agent["name"] for agent in json.loads(json_result.stdout)["agents"]]
    assert matched_names == ["label-backend"], matched_names


@pytest.mark.release
def test_list_label_filter_invalid_format(e2e: E2eSession) -> None:
    # Same tutorial block as test_list_label_filter, but exercises the unhappy
    # path: a --label value without "=" is rejected before any discovery runs.
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    result = e2e.run("mngr list --label TEAM", comment="reject malformed --label without KEY=VALUE")
    # A click usage error (exit code 2) confirms the value is rejected during
    # argument parsing -- before any provider discovery runs -- rather than via a
    # later runtime failure.
    expect(result).to_have_exit_code(2)
    # The message must name the required format and echo back the offending value
    # so the user can see exactly which input was rejected.
    expect(result.stderr).to_contain("KEY=VALUE")
    expect(result.stderr).to_contain("--label")
    expect(result.stderr).to_contain("TEAM")


@pytest.mark.release
# Filtering by host label forces full host discovery across every provider (an
# authenticated Modal lookup plus Docker/Vultr probes), which routinely runs past
# the default 10s per-test timeout. The release CI lane overrides this globally to
# 90s; bump it explicitly so the test also passes when run directly. (Same
# rationale as test_list_local_filter.)
@pytest.mark.timeout(60)
def test_list_host_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by host label
        mngr list --host-label ENV=staging
    """)
    result = e2e.run("mngr list --host-label ENV=staging", comment="filter by host label")
    expect(result).to_succeed()
    # No hosts carry ENV=staging in a fresh environment, so the filter matches
    # nothing and the command reports an empty result rather than erroring on
    # the host-label expression.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_list_host_label_filter_invalid_format(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: a --host-label value missing the
    # "=" separator is rejected up front (before any host discovery) with a
    # message explaining the required KEY=VALUE format.
    e2e.write_tutorial_block("""
        # filter by host label
        mngr list --host-label ENV=staging
    """)
    result = e2e.run("mngr list --host-label staging", comment="reject host label without KEY=VALUE format")
    # click rejects a malformed option value with its standard usage-error exit
    # code (2), not a generic failure or a traceback.
    expect(result).to_have_exit_code(2)
    # The message must be actionable: it names the offending flag, states the
    # required KEY=VALUE format, and echoes the bad value back so the user can
    # see exactly what was rejected.
    expect(result.stderr).to_contain("--host-label")
    expect(result.stderr).to_contain("KEY=VALUE")
    expect(result.stderr).to_contain("staging")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_list_fields_and_sort(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # choose which fields to display and sort order
        mngr list --fields "name,state,host.provider,create_time" --sort "create_time desc"
        # see mngr list --help for a complete list of fields you can reference
    """)
    # Create two Modal agents so the listing has real rows to render and sort.
    # Creating a Modal agent also invokes the Modal CLI (environment_create runs
    # during provider initialization), which satisfies the @pytest.mark.modal
    # resource guard. The second agent is created last so it has the most recent
    # create_time, which lets us verify the "create_time desc" (newest-first) sort.
    expect(
        e2e.run(
            "mngr create list-older --provider modal --type command --no-connect --no-ensure-clean -- sleep 100200",
            comment="create the older Modal agent",
            timeout=240.0,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create list-newer --provider modal --type command --no-connect --no-ensure-clean -- sleep 100201",
            comment="create the newer Modal agent",
            timeout=240.0,
        )
    ).to_succeed()

    result = e2e.run(
        'mngr list --fields "name,state,host.provider,create_time" --sort "create_time desc"',
        comment="choose which fields to display and sort order",
    )
    expect(result).to_succeed()
    # Both Modal agents appear, each reporting the modal provider via host.provider.
    expect(result.stdout).to_contain("list-older")
    expect(result.stdout).to_contain("list-newer")
    # Field selection: only the requested columns are shown. CREATE_TIME is a
    # selected column populated with a real timestamp, while default-only columns
    # (HOST STATE, PROJECT) are excluded.
    expect(result.stdout).to_match(r"list-newer\s+\S+\s+modal\s+\d{4}-\d{2}-\d{2}")
    assert "HOST STATE" not in result.stdout, result.stdout
    assert "PROJECT" not in result.stdout, result.stdout
    # Sort order: "create_time desc" lists the most recently created agent first.
    assert result.stdout.index("list-newer") < result.stdout.index("list-older"), result.stdout


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_limit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # limit the number of results
        mngr list --limit 10
    """)
    # Create a couple of agents so --limit has results to truncate. Without any
    # agents the flag is a no-op and the command's behavior can't be observed.
    for name in ("limit-first", "limit-second"):
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep 100100",
                comment=f"create agent {name} to populate the list",
            )
        ).to_succeed()

    # limit the number of results. Scope discovery to the local provider (where
    # the test agents actually run) so the listing does not abort when default
    # discovery reaches an installed-but-unconfigured cloud backend (e.g. `aws`),
    # which raises ProviderUnavailableError under the default `--on-error abort`.
    result = e2e.run("mngr list --limit 10 --provider local", comment="limit the number of results")
    expect(result).to_succeed()
    # A limit larger than the agent count leaves every agent visible.
    expect(result.stdout).to_contain("limit-first")
    expect(result.stdout).to_contain("limit-second")

    # A limit smaller than the agent count truncates to exactly that many results.
    limited = e2e.run(
        "mngr list --limit 1 --provider local --format json",
        comment="a smaller limit truncates the results",
    )
    expect(limited).to_succeed()
    assert len(json.loads(limited.stdout)["agents"]) == 1


# NOTE: no @pytest.mark.modal here. `mngr list` against the test's fresh,
# empty Modal environment skips the Modal provider entirely (it raises
# ProviderEmptyError because the environment was never created), so this test
# never exercises Modal. The resource guard would flag a superfluous
# @pytest.mark.modal as a NEVER_INVOKED violation. The watch-mode behavior
# under test (wrapping `mngr list` in watch(1)) is provider-agnostic.
@pytest.mark.release
# The watched `mngr list` runs the full provider-discovery path, which routinely
# takes ~10s (see test_list_local_filter), and `watch` only renders a frame once
# that first run completes. The wrapping `timeout` below therefore has to outlast
# a full discovery, pushing the test-function body past the default 10s per-test
# timeout. The release CI lane already overrides this globally to 90s; match that
# locally so the test isn't killed mid-watch.
@pytest.mark.timeout(90)
def test_list_watch_mode(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # watch mode: refresh the list every 5 seconds
        watch -n5 mngr list
    """)
    # `watch` blocks until SIGINT; wrap with a `timeout` so the test exits
    # without waiting for a full refresh interval. `timeout` returns 124 on
    # expiry (then `|| true` masks it), so a clean exit is expected. The window
    # must be long enough for `watch` to run `mngr list` to completion *and*
    # render its output once -- `watch` only paints a frame after the wrapped
    # command exits, and a full `mngr list` discovery routinely takes ~10s. We
    # then assert that the actual list output ("No agents found", in this fresh
    # environment) made it into watch's rendered frame, proving watch genuinely
    # executed the wrapped command rather than merely entering the alternate
    # screen and exiting. Give the discovery generous headroom (30s) so a slow
    # run still produces a frame; the e2e per-command timeout is raised above
    # that so the harness doesn't kill `timeout` before it expires on its own.
    result = e2e.run(
        "timeout 30 watch -n5 mngr list || true",
        comment="watch mode: refresh the list every 5 seconds",
        timeout=60.0,
    )
    expect(result).to_succeed()
    # `watch` prints a header naming the interval and the command it is running;
    # asserting on it confirms the listing was produced *by watch* at the -n5
    # interval (the point of this test) rather than by a bare `mngr list`.
    expect(result.stdout).to_contain("Every 5.0s: mngr list")
    # watch renders onto the alternate screen with terminal escape sequences,
    # but the wrapped command's plain text ("No agents found") still appears
    # verbatim in the captured byte stream.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
# `mngr list` runs the full provider-discovery path: even in a fresh, empty
# environment it makes an authenticated Modal SDK lookup to decide the per-user
# environment does not exist yet (ProviderEmptyError), which routinely takes
# ~10s -- past the default 10s per-test timeout. The release CI lane overrides
# the timeout globally to 90s, but pinning it here keeps the test green under
# the default config too (and well clear of the single discovery's latency).
@pytest.mark.timeout(60)
def test_list_format_jsonl(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output each entry as a JSON object (useful for scripting)
        mngr list --format jsonl
    """)
    result = e2e.run("mngr list --format jsonl", comment="output each entry as a JSON object")
    expect(result).to_succeed()
    # The JSONL contract is "one standalone JSON object per line" (as opposed to
    # the single big array produced by --format json). Verify every emitted line
    # parses as a JSON object. With no agents the stream is empty, which is also
    # valid JSONL.
    for line in result.stdout.splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict), f"JSONL line is not a JSON object: {line!r}"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_format_jsonl_with_agents(e2e: E2eSession) -> None:
    # Shares the `mngr list --format jsonl` tutorial block, but exercises the
    # populated case: with no agents the stream is empty and the JSONL contract
    # is only checked vacuously (see test_list_format_jsonl). Create a couple of
    # agents so the stream has real entries, then verify the distinguishing
    # property of JSONL -- one standalone JSON object *per line* -- rather than
    # the single array that `--format json` emits.
    e2e.write_tutorial_block("""
        # output each entry as a JSON object (useful for scripting)
        mngr list --format jsonl
    """)
    for name in ("jsonl-first", "jsonl-second"):
        expect(
            e2e.run(
                f"mngr create {name} --transfer=none --type command --no-ensure-clean --no-connect -- sleep 100300",
                comment=f"create agent {name} to populate the JSONL stream",
            )
        ).to_succeed()

    result = e2e.run("mngr list --format jsonl", comment="output each entry as a JSON object")
    expect(result).to_succeed()

    # Every non-empty line must independently parse as a JSON object, and there
    # must be one line per agent (the JSONL contract). A line carrying the whole
    # listing as a JSON array -- what `--format json` would emit -- would fail the
    # dict check, so this also confirms jsonl != json.
    parsed_lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert all(isinstance(obj, dict) for obj in parsed_lines), result.stdout
    listed_names = {obj.get("name") for obj in parsed_lines}
    assert {"jsonl-first", "jsonl-second"} <= listed_names, listed_names

    # The whole stdout is NOT a single JSON document (that is the `--format json`
    # shape): with more than one object, the concatenated lines do not parse as
    # one value. This nails down the "one object per line" contract directly.
    if len(parsed_lines) > 1:
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)


@pytest.mark.release
@pytest.mark.modal
def test_observe_discovery_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # continually stream discovery events as JSONL (useful for piping to jq to turn this data into an event stream)
        # will get new events as new hosts are created/destroyed, come online and offline, etc.
        # see the `DiscoveryEvent` type for a complete list of the event types that will be returned in this stream
        mngr observe --discovery-only
    """)
    # `mngr observe` streams indefinitely; wrap with a `timeout` so the test
    # doesn't hang. observe never exits on its own, so `timeout` always has to
    # kill it and therefore exits 124 -- asserting on that exact code proves
    # observe ran continuously as a stream rather than crashing or exiting early
    # (which the original `|| true; to_succeed()` could not detect). The window
    # must be long enough for observe to emit its first discovery events: on a
    # first run there is no cached snapshot on disk, so observe runs a full
    # (synchronous) probe of every configured provider before emitting anything.
    result = e2e.run(
        "timeout 45 mngr observe --discovery-only",
        comment="continually stream discovery events as JSONL",
        timeout=90.0,
    )
    expect(result).to_have_exit_code(124)
    # Verify observe actually streamed discovery events as JSONL (the point of
    # --discovery-only), not merely that it ran. Every non-empty stdout line must
    # be a standalone JSON object shaped like a discovery-event envelope -- a
    # string "type" plus the "mngr/discovery" source that tags every event in
    # this stream -- which is exactly the contract a `jq` consumer relies on. At
    # least one such event must have been emitted within the window.
    events = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        assert isinstance(event, dict), f"discovery line is not a JSON object: {line!r}"
        assert isinstance(event.get("type"), str), f"discovery event missing a string type: {event!r}"
        assert event.get("source") == "mngr/discovery", f"unexpected event source: {event!r}"
        events.append(event)
    assert events, "observe streamed no discovery events within the timeout window"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(240)
def test_list_pipe_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can pass the ids of agents and/or hosts to only list details for specific ids:
        mngr list --format "{id}" | head -n 2 | mngr list --stdin
    """)
    # The pipe is only meaningful when there is an id to feed through it, so first
    # create a real (local, in-place) agent. We deliberately do NOT mark this test
    # @pytest.mark.modal: this flow never invokes Modal. `mngr list` discovers
    # providers via the in-process SDK (which the subprocess resource guard cannot
    # observe) rather than the Modal CLI, and a local --transfer=none create does
    # not create a Modal environment either, so the modal mark would trip the
    # guard's "marked but never invoked" check. --type command -- sleep <N> stands
    # in for a real agent so the test doesn't need claude installed; the pinned
    # sleep value lets any leaked process be traced back to this call.
    create_result = e2e.run(
        "mngr create my-task --transfer=none --type command --no-ensure-clean -- sleep 100119",
        comment="create a local agent so the pipe has a real id to filter on",
    )
    expect(create_result).to_succeed()

    # The verbatim tutorial pipe below runs full, all-provider discovery. Several
    # providers are enabled by default but unreachable in this isolated
    # environment: the cloud providers (AWS/GCP/Azure/Vultr/imbue_cloud) have no
    # credentials, and Docker has no running daemon. Each such provider surfaces a
    # ProviderUnavailableError, so `mngr list` exits non-zero -- noise unrelated to
    # the stdin id-filtering this test exercises (the local agent still lists
    # correctly). Which provider trips first is non-deterministic (each resolves
    # its backend independently and some time out), so disable them all and let
    # the pipe's exit status reflect only the local discovery it depends on. This
    # is exactly the remediation each error message itself recommends. The local
    # and Modal providers stay enabled, so the pipe still spans multiple providers.
    for unreachable_provider in ("aws", "gcp", "azure", "vultr", "imbue_cloud", "docker"):
        expect(
            e2e.run(
                f"mngr config set --scope user providers.{unreachable_provider}.is_enabled false",
                comment=f"disable the unreachable {unreachable_provider} provider so discovery is deterministic",
            )
        ).to_succeed()

    # Look up the created agent's id so we can verify the stdin filter targets it.
    # Scope this helper lookup to the local provider so it stays fast and
    # deterministic (the verbatim tutorial pipe below still exercises full,
    # all-provider discovery).
    json_result = e2e.run("mngr list --provider local --format json", comment="look up the created agent's id")
    expect(json_result).to_succeed()
    agents = json.loads(json_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one my-task agent, got: {agents}"
    agent_id = matching[0]["id"]

    # Run the tutorial command verbatim. With a single agent, `mngr list --format
    # "{id}"` emits exactly one id, so the head -n 2 slice keeps it and the final
    # `mngr list --stdin` filters back down to that agent and prints its details.
    # The pipe runs two full discoveries (every enabled provider) back to back, so
    # it needs a longer per-command timeout than the 30s default.
    result = e2e.run(
        'mngr list --format "{id}" | head -n 2 | mngr list --stdin',
        comment="pipe ids through stdin to list details for specific ids",
        timeout=120.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("my-task")

    # Verify the --stdin filtering directly and deterministically: feeding the
    # agent's id selects exactly that agent. Scoped to the local provider for speed.
    stdin_result = e2e.run(
        f'echo "{agent_id}" | mngr list --provider local --stdin --format json',
        comment="feeding a single id via stdin filters to exactly that agent",
    )
    expect(stdin_result).to_succeed()
    filtered_ids = [agent["id"] for agent in json.loads(stdin_result.stdout)["agents"]]
    assert filtered_ids == [agent_id], f"expected --stdin to filter to exactly {agent_id}, got: {filtered_ids}"
