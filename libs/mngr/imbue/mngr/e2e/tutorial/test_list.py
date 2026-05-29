"""Tests for listing agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Timeout for a single remote (Modal) agent creation. Mirrors the value used in
# test_create_modal.py: fresh Modal hosts take ~30-90s to boot and provision.
_REMOTE_TIMEOUT = 120.0


# No @pytest.mark.modal: in a fresh environment the Modal environment does not
# exist yet, so the provider raises ProviderEmptyError and is skipped. `mngr list`
# only does an in-process SDK lookup (never shelling out to the `modal` CLI binary
# that the e2e resource guard tracks), so marking this test as using modal would
# trip the guard's superfluous-mark check. This is intended behavior: `mngr list`
# must never bootstrap a Modal environment.
@pytest.mark.release
def test_list_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all agents
        mngr list
    """)
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output all objects as one big JSON array when complete  (useful for scripting)
        mngr list --format json
    """)
    result = e2e.run(
        "mngr list --format json",
        comment="output all objects as one big JSON array when complete  (useful for scripting)",
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []


@pytest.mark.release
def test_list_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr ls
    """)
    # `mngr ls` is the short alias for `mngr list`; verify it not only succeeds
    # but actually performs a listing (same "No agents found" output as the long
    # form in the fresh, agent-free environment).
    result = e2e.run("mngr ls", comment="short form")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_list_running_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only running agents
        mngr list --running
    """)
    # Create a real modal-backed agent so the modal provider is genuinely
    # exercised (this test is marked @pytest.mark.modal). Running `mngr list` in
    # an empty environment does not reliably make a modal API call, so without a
    # real modal resource the @pytest.mark.modal guard flakily reports the mark
    # as never-invoked. --no-connect keeps the agent headless (no local tmux).
    # A `--type command -- sleep` agent settles into the WAITING state (it is
    # alive but not actively producing output), which lets us verify the
    # contract of --running: it must filter by lifecycle state, not just dump
    # every agent. (A RUNNING lifecycle state requires the agent to mark itself
    # active, which only an interactive agent mid-turn does -- not reproducible
    # deterministically here -- so we verify the filter via its exclusion of a
    # known non-running agent.)
    expect(
        e2e.run(
            "mngr create idle-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 101020",
            comment="create a modal agent to exercise the --running filter",
            timeout=150.0,
        )
    ).to_succeed()

    # Sanity check: the agent exists and is discoverable in an unfiltered list,
    # and is not in the RUNNING state (so excluding it below is meaningful).
    unfiltered = e2e.run("mngr list --format json", comment="confirm the agent is discoverable")
    expect(unfiltered).to_succeed()
    unfiltered_agents = {agent["name"]: agent for agent in json.loads(unfiltered.stdout)["agents"]}
    assert "idle-task" in unfiltered_agents, f"Expected idle-task in unfiltered list, got: {unfiltered_agents}"
    assert unfiltered_agents["idle-task"]["state"] != "RUNNING", (
        f"Expected the sleep agent to not be RUNNING, got: {unfiltered_agents['idle-task']['state']}"
    )

    # The tutorial command: --running must exclude the non-running agent.
    result = e2e.run("mngr list --running", comment="show only running agents")
    expect(result).to_succeed()
    assert "idle-task" not in result.stdout, f"--running must exclude the non-running agent, got: {result.stdout!r}"

    # Verify via JSON that every agent surfaced by --running is actually RUNNING.
    running_json = e2e.run("mngr list --running --format json", comment="machine-readable running-only listing")
    expect(running_json).to_succeed()
    running_agents = json.loads(running_json.stdout)["agents"]
    assert all(agent["state"] == "RUNNING" for agent in running_agents), (
        f"--running surfaced a non-RUNNING agent: {running_agents}"
    )
    assert all(agent["name"] != "idle-task" for agent in running_agents), (
        f"--running must not surface idle-task: {running_agents}"
    )


@pytest.mark.release
# NOTE: no @pytest.mark.modal here. `mngr list --stopped` against the empty test
# environment performs provider discovery via the Modal Python SDK *inside the mngr
# subprocess*, which the in-process resource guard cannot observe, and it never
# shells out to the `modal` CLI (the environment is created lazily on agent
# creation, not on list -- teardown confirms it is never created). A @pytest.mark.modal
# here would therefore always fail the guard's NEVER_INVOKED check. See test_create_modal.py
# for the contrast: `mngr create --provider modal` does invoke the Modal CLI via
# environment_create during provider init, so those tests legitimately carry the mark.
@pytest.mark.timeout(120)
def test_list_stopped_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only stopped agents (not running, still exists and can be restarted)
        mngr list --stopped
    """)
    result = e2e.run("mngr list --stopped", comment="show only stopped agents")
    expect(result).to_succeed()
    # No agents have been created in this fresh environment, so the filter
    # produces an empty list rather than erroring.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# No @pytest.mark.modal: the agent is created on the local provider, which never
# invokes the Modal CLI (the Modal environment is not created -- teardown confirms
# it is never found), so the resource guard's NEVER_INVOKED check could not be
# satisfied. The local agent does use tmux (its session) and rsync (repo sync).
@pytest.mark.timeout(120)
def test_list_stopped_filter_shows_stopped_agent(e2e: E2eSession) -> None:
    """Behavior test for the same `mngr list --stopped` block.

    Unlike test_list_stopped_filter (which only smoke-tests the command against
    an empty environment), this exercises the filter against real data: a stopped
    agent must appear under --stopped while a running one must not.
    """
    e2e.write_tutorial_block("""
        # show only stopped agents (not running, still exists and can be restarted)
        mngr list --stopped
    """)
    # Create a local command agent; it starts out running.
    expect(
        e2e.run(
            "mngr create stopped-task --type command --no-ensure-clean --no-connect -- sleep 100600",
            comment="create a running agent to exercise the --stopped filter",
        )
    ).to_succeed()
    # While running, the agent must be excluded from --stopped.
    running_list = e2e.run("mngr list --stopped", comment="a running agent should not appear under --stopped")
    expect(running_list).to_succeed()
    expect(running_list.stdout).not_to_contain("stopped-task")
    # Stop the agent, then it must appear under --stopped.
    expect(e2e.run("mngr stop stopped-task", comment="stop the agent")).to_succeed()
    stopped_list = e2e.run("mngr list --stopped", comment="a stopped agent should appear under --stopped")
    expect(stopped_list).to_succeed()
    expect(stopped_list.stdout).to_contain("stopped-task")


@pytest.mark.release
def test_list_archived_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only archived agents (stopped, cannot necessarily be restarted, but data can be inspected)
        mngr list --archived
    """)
    result = e2e.run("mngr list --archived", comment="show only archived agents")
    expect(result).to_succeed()
    # No agents exist in a fresh environment, so the archived filter must
    # produce an empty listing rather than merely exiting cleanly.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_list_active_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only active agents (anything not archived/destroyed/crashed/failed)
        mngr list --active
    """)
    # Create a real agent first. A freshly-created agent is RUNNING/WAITING
    # (hence active), so the --active filter has something to return -- letting
    # us verify the filter actually surfaces a live agent rather than merely
    # exiting cleanly on an empty list.
    expect(
        e2e.run(
            "mngr create my-task --no-connect --type command --no-ensure-clean -- sleep 100100",
            comment="create a running (active) agent",
        )
    ).to_succeed()
    # The running agent must appear in the active listing.
    result = e2e.run("mngr list --active --format json", comment="show only active agents")
    expect(result).to_succeed()
    agents = json.loads(result.stdout)["agents"]
    assert len(agents) == 1, f"expected exactly the running agent, got {agents}"
    assert agents[0]["name"] == "my-task"
    # A freshly-created command agent is active: RUNNING (or WAITING) rather
    # than any of the archived/destroyed/crashed/failed states --active hides.
    assert agents[0]["state"] in ("RUNNING", "WAITING")


# `mngr list` discovers Modal hosts over the network, and that gRPC round-trip
# can occasionally run long enough to exceed the default 10s per-test timeout, so
# this test is marked flaky (offload retries it), gets a generous @timeout, and
# the list call below is given a matching explicit command timeout.
@pytest.mark.flaky
@pytest.mark.release
@pytest.mark.timeout(120)
def test_config_set_list_active_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can make any of those filters the default for "mngr list" by setting it in your config.
        # for example, to hide agents from dead/destroyed hosts by default:
        mngr config set commands.list.active true
        # to opt out for a single call, override the env var: MNGR__COMMANDS__LIST__ACTIVE=false mngr list
    """)
    set_result = e2e.run(
        "mngr config set commands.list.active true",
        comment="make active filter the default for mngr list",
    )
    expect(set_result).to_succeed()
    # config set echoes back the key and value it persisted.
    expect(set_result.stdout).to_contain("commands.list.active")
    expect(set_result.stdout).to_contain("true")

    # Verify the actual effect, not just the exit code: the value was written to
    # the project-scope config (where config set defaults to writing), so it
    # persists as the default that applies to every `mngr list` call.
    persisted = e2e.run(
        "mngr config get commands.list.active --scope project",
        comment="the active default was persisted to the project config",
    )
    expect(persisted).to_succeed()
    expect(persisted.stdout.strip()).to_equal("true")

    expect(
        e2e.run(
            "MNGR__COMMANDS__LIST__ACTIVE=false mngr list",
            comment="opt out for a single call via env var override",
            timeout=110.0,
        )
    ).to_succeed()


@pytest.mark.release
def test_list_local_filter(e2e: E2eSession) -> None:
    # `--local` restricts discovery to the local provider, so Modal is never
    # queried -- hence no @pytest.mark.modal (the resource guard would fail the
    # test for a superfluous mark).
    e2e.write_tutorial_block("""
        # show only agents running locally
        mngr list --local
    """)
    result = e2e.run("mngr list --local", comment="show only agents running locally")
    expect(result).to_succeed()
    # With no agents created in this isolated environment, the local filter
    # should report an empty list rather than erroring or hanging on remote
    # provider discovery.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_list_remote_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only agents running remotely
        mngr list --remote
    """)
    # Create a remote (Modal) agent so the --remote filter has something to show.
    # This also actually exercises Modal (the create path invokes the Modal CLI to
    # create the environment and deploy), which is what @pytest.mark.modal asserts:
    # a bare `mngr list --remote` against an empty environment only queries Modal
    # via the in-subprocess SDK, which the resource guard cannot observe.
    expect(
        e2e.run(
            "mngr create remote-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100129",
            comment="create a remote Modal agent so --remote has an agent to list",
            timeout=240.0,
        )
    ).to_succeed()
    # show only agents running remotely
    remote_result = e2e.run("mngr list --remote", comment="show only agents running remotely", timeout=60.0)
    expect(remote_result).to_succeed()
    # the remote agent must appear when filtering for remote-only agents
    expect(remote_result.stdout).to_contain("remote-task")
    # and it must NOT appear when filtering for local-only agents, confirming the
    # filter classifies the Modal agent as remote rather than local
    local_result = e2e.run("mngr list --local", comment="local filter must exclude the remote agent", timeout=60.0)
    expect(local_result).to_succeed()
    expect(local_result.stdout).not_to_contain("remote-task")


# NOTE: no @pytest.mark.modal here. Since `mngr list` stopped auto-creating the
# Modal environment for read-only commands (commit 3813de1e8), `mngr list
# --provider modal` in a fresh environment never invokes the `modal` CLI: the
# Modal backend raises ProviderUnavailableError and is skipped. The Modal Python
# SDK is only ever exercised inside the `mngr` subprocess, where the resource
# guard's SDK monkeypatch cannot see it, so the guard would fail the test with
# "marked with @pytest.mark.modal but never invoked modal".
@pytest.mark.release
def test_list_provider_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by provider
        mngr list --provider modal
    """)
    result = e2e.run("mngr list --provider modal", comment="filter by provider")
    expect(result).to_succeed()
    # In a fresh, isolated environment there are no agents on any provider, so
    # filtering by the modal provider returns an empty listing rather than an
    # error -- the Modal backend simply reports itself unavailable and is
    # skipped (the same way Docker is when its daemon is offline).
    expect(result.stdout).to_contain("No agents found")
    # The JSON form confirms the filter ran and returned a clean empty result.
    json_result = e2e.run("mngr list --provider modal --format json", comment="filter by provider (JSON)")
    expect(json_result).to_succeed()
    parsed = json.loads(json_result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []


@pytest.mark.release
@pytest.mark.modal
def test_list_project_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by project
        mngr list --project my-project
    """)
    expect(e2e.run("mngr list --project my-project", comment="filter by project")).to_succeed()


@pytest.mark.release
def test_list_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    result = e2e.run("mngr list --label TEAM=backend", comment="filter by agent label")
    expect(result).to_succeed()
    # No agent carries the TEAM=backend label in this fresh environment, so the
    # filter must match nothing rather than fall back to listing everything.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_label_filter_invalid_format(e2e: E2eSession) -> None:
    # Shares the `mngr list --label` tutorial block, but exercises the unhappy
    # path: a label spec missing the `=` separator must be rejected up front.
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    result = e2e.run("mngr list --label TEAM", comment="reject malformed agent label (missing '=')")
    expect(result).to_fail()
    expect(result.stderr).to_contain("KEY=VALUE")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_list_host_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by host label
        mngr list --host-label ENV=staging
    """)
    # Create a Modal agent whose host carries the ENV=staging label so the
    # filter has a real host to match against. --no-connect keeps the test
    # fast (no tmux session); the host label is applied to the Modal host
    # regardless. This also exercises the full Modal discovery path that the
    # @pytest.mark.modal guard expects.
    expect(
        e2e.run(
            "mngr create staging-agent --provider modal --type command --no-ensure-clean"
            " --no-connect --host-label ENV=staging -- sleep 100077",
            comment="create a Modal agent whose host is labeled ENV=staging",
            timeout=180.0,
        )
    ).to_succeed()

    # filter by host label
    matching = e2e.run("mngr list --host-label ENV=staging", comment="filter by host label")
    expect(matching).to_succeed()
    expect(matching.stdout).to_contain("staging-agent")

    # A non-matching host label must exclude the agent (unhappy path).
    non_matching = e2e.run(
        "mngr list --host-label ENV=production",
        comment="a different host label value must not match the staging host",
    )
    expect(non_matching).to_succeed()
    assert "staging-agent" not in non_matching.stdout, non_matching.stdout


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_list_fields_and_sort(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # choose which fields to display and sort order
        mngr list --fields "name,state,host.provider,create_time" --sort "create_time desc"
        # see mngr list --help for a complete list of fields you can reference
    """)
    # On an empty environment `mngr list` short-circuits the modal provider
    # (the modal environment does not exist yet) and produces no rows, so the
    # --fields/--sort behavior cannot actually be observed. Create two agents on
    # a shared Modal host -- created at distinct times -- so the list below has
    # real rows to format and sort. Creating the first host is what invokes the
    # `modal` CLI (environment_create) that satisfies @pytest.mark.modal.
    # Use lightweight `--type command` (sleep) agents rather than claude agents
    # so the test stays fast and has no API-key dependency; the agent type does
    # not affect how `mngr list` renders fields or sorts rows.
    expect(
        e2e.run(
            "mngr create alpha@shared-host.modal --provider modal --new-host --type command"
            " --no-connect --no-ensure-clean -- sleep 100991",
            comment="create the older agent on a new Modal host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create beta@shared-host.modal --type command --no-connect --no-ensure-clean -- sleep 100992",
            comment="create the newer agent on the same Modal host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    result = e2e.run(
        'mngr list --fields "name,state,host.provider,create_time" --sort "create_time desc"',
        comment="choose which fields to display and sort order",
    )
    expect(result).to_succeed()
    # Both agents must appear, and the host.provider field must render "modal"
    # (proving the dotted field path resolved rather than being dropped).
    expect(result.stdout).to_contain("alpha")
    expect(result.stdout).to_contain("beta")
    expect(result.stdout).to_contain("modal")
    # The create_time column must render a real (non-empty) timestamp rather
    # than an empty cell, so an ISO-style date (YYYY-MM-DD) appears in the output.
    expect(result.stdout).to_match(r"\d{4}-\d{2}-\d{2}")
    # "create_time desc" is newest-first, and beta was created after alpha, so
    # beta must appear above alpha in the output.
    assert result.stdout.index("beta") < result.stdout.index("alpha"), (
        f"Expected beta (newer) to sort before alpha (older) with --sort 'create_time desc'.\n"
        f"  Stdout:\n{result.stdout}"
    )


@pytest.mark.release
def test_list_limit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # limit the number of results
        mngr list --limit 10
    """)
    # No @pytest.mark.modal: in a fresh environment the Modal per-user environment
    # does not exist yet, so the Modal backend raises ProviderEmptyError at
    # construction and `mngr list` deliberately skips the Modal provider without
    # ever invoking the `modal` CLI (see ModalProviderBackend.build_provider_instance
    # and list_provider_names_to_load). The resource guard therefore correctly
    # reports that modal was never invoked.
    result = e2e.run("mngr list --limit 10", comment="limit the number of results")
    expect(result).to_succeed()
    # With no agents in this isolated environment, the limit has nothing to cap and
    # the command reports an empty listing rather than erroring.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_watch_mode(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # watch mode: refresh the list every 5 seconds
        watch -n5 mngr list
    """)
    # `watch` loops until killed, so wrap it in `timeout`. The timeout is long
    # enough for `watch` to run `mngr list` to completion at least once (a cold
    # `mngr list` takes ~15s), so we can assert that the periodic refresh
    # actually rendered the agent list -- not just that `watch` launched.
    # `timeout` exits 124 when it terminates the still-running `watch`; the
    # trailing `echo` captures that code while keeping the overall exit status 0.
    result = e2e.run(
        'timeout 30 watch -n5 mngr list; echo "watch-exit-code=$?"',
        comment="watch mode: refresh the list every 5 seconds",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # `timeout` killed a still-looping `watch`, confirming it launched and stayed
    # alive rather than crashing immediately (which would yield a different code).
    expect(result.stdout).to_contain("watch-exit-code=124")
    # `watch` ran `mngr list` to completion and rendered its result: with no
    # agents in the isolated environment, the list reports "No agents found".
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_format_jsonl(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output each entry as a JSON object (useful for scripting)
        mngr list --format jsonl
    """)
    result = e2e.run("mngr list --format jsonl", comment="output each entry as a JSON object")
    expect(result).to_succeed()
    # Unlike --format json (one array), jsonl emits one standalone JSON object
    # per line. Verify the contract holds: every non-empty line parses as a JSON
    # object on its own. With no agents the stream is empty (no array brackets).
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines == [], f"expected no agents in a fresh environment, got: {lines}"
    for line in lines:
        assert isinstance(json.loads(line), dict), f"jsonl line is not a JSON object: {line!r}"


@pytest.mark.release
@pytest.mark.modal
def test_observe_discovery_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # continually stream discovery events as JSONL (useful for piping to jq to turn this data into an event stream)
        # will get new events as new hosts are created/destroyed, come online and offline, etc.
        # see the `DiscoveryEvent` type for a complete list of the event types that will be returned in this stream
        mngr observe --discovery-only
    """)
    # `mngr observe` streams indefinitely; wrap with a short `timeout` so the
    # test doesn't hang. `timeout` exits 124 on expiry.
    result = e2e.run(
        "timeout 1 mngr observe --discovery-only || true",
        comment="continually stream discovery events as JSONL",
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_pipe_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can pass the ids of agents and/or hosts to only list details for specific ids:
        mngr list --format "{id}" | head -n 2 | mngr list --stdin
    """)
    expect(
        e2e.run(
            'mngr list --format "{id}" | head -n 2 | mngr list --stdin',
            comment="pipe ids through stdin to list details for specific ids",
        )
    ).to_succeed()
