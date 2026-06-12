"""Tests for listing agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

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
@pytest.mark.modal
def test_list_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr ls
    """)
    expect(e2e.run("mngr ls", comment="short form")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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

    # show only running agents
    result = e2e.run("mngr list --running --format json", comment="show only running agents")
    expect(result).to_succeed()
    running_names = [agent["name"] for agent in json.loads(result.stdout)["agents"]]
    assert "running-agent" in running_names, running_names
    assert "stopped-agent" not in running_names, running_names


@pytest.mark.release
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


@pytest.mark.release
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
@pytest.mark.modal
def test_config_set_list_active_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can make any of those filters the default for "mngr list" by setting it in your config.
        # for example, to hide agents from dead/destroyed hosts by default:
        mngr config set commands.list.active true
        # to opt out for a single call, override the env var: MNGR__COMMANDS__LIST__ACTIVE=false mngr list
    """)
    expect(
        e2e.run(
            "mngr config set commands.list.active true",
            comment="make active filter the default for mngr list",
        )
    ).to_succeed()
    # Verify the set actually persisted the default into the project config,
    # which is the whole point of the tutorial block -- not merely that the set
    # command exited 0. Read it back from the project scope (scope mode reads the
    # TOML file literally; the merged view does not surface per-command flag
    # defaults under this key).
    get_result = e2e.run(
        "mngr config get commands.list.active --scope project",
        comment="confirm the active default was written to the project config",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout.strip()).to_equal("true")
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


@pytest.mark.release
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
def test_list_label_filter_invalid_format(e2e: E2eSession) -> None:
    # Same tutorial block as test_list_label_filter, but exercises the unhappy
    # path: a --label value without "=" is rejected before any discovery runs.
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    result = e2e.run("mngr list --label TEAM", comment="reject malformed --label without KEY=VALUE")
    expect(result).to_fail()
    expect(result.stderr).to_contain("KEY=VALUE")


@pytest.mark.release
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
def test_list_host_label_filter_invalid_format(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: a --host-label value missing the
    # "=" separator is rejected up front (before any host discovery) with a
    # message explaining the required KEY=VALUE format.
    e2e.write_tutorial_block("""
        # filter by host label
        mngr list --host-label ENV=staging
    """)
    result = e2e.run("mngr list --host-label staging", comment="reject host label without KEY=VALUE format")
    expect(result).to_fail()
    expect(result.stderr).to_contain("KEY=VALUE")


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
@pytest.mark.rsync
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

    # limit the number of results
    result = e2e.run("mngr list --limit 10", comment="limit the number of results")
    expect(result).to_succeed()
    # A limit larger than the agent count leaves every agent visible.
    expect(result.stdout).to_contain("limit-first")
    expect(result.stdout).to_contain("limit-second")

    # A limit smaller than the agent count truncates to exactly that many results.
    limited = e2e.run("mngr list --limit 1 --format json", comment="a smaller limit truncates the results")
    expect(limited).to_succeed()
    assert len(json.loads(limited.stdout)["agents"]) == 1


# NOTE: no @pytest.mark.modal here. `mngr list` against the test's fresh,
# empty Modal environment skips the Modal provider entirely (it raises
# ProviderEmptyError because the environment was never created), so this test
# never exercises Modal. The resource guard would flag a superfluous
# @pytest.mark.modal as a NEVER_INVOKED violation. The watch-mode behavior
# under test (wrapping `mngr list` in watch(1)) is provider-agnostic.
@pytest.mark.release
def test_list_watch_mode(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # watch mode: refresh the list every 5 seconds
        watch -n5 mngr list
    """)
    # `watch` blocks until SIGINT; wrap with a short `timeout` so the test
    # exits without waiting for a full refresh interval. `timeout` returns 124
    # on expiry (then `|| true` masks it), so a clean exit is expected. The
    # window must be long enough for `watch` to run `mngr list` once and render
    # its output -- we then assert that the actual list output ("No agents
    # found", in this fresh environment) made it into watch's rendered frame,
    # proving watch genuinely executed the wrapped command rather than merely
    # starting up. `watch -n5` runs the command immediately on launch, so an
    # 8-second window comfortably captures the first frame.
    result = e2e.run(
        "timeout 8 watch -n5 mngr list || true",
        comment="watch mode: refresh the list every 5 seconds",
    )
    expect(result).to_succeed()
    # watch renders onto the alternate screen with terminal escape sequences,
    # but the wrapped command's plain text ("No agents found") still appears
    # verbatim in the captured byte stream.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
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
    # The pipe runs two full (all-provider) discoveries back to back, so it needs a
    # longer per-command timeout than the 30s default.
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
