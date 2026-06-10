"""Tests for the ADVANCED WORKFLOWS and TIPS AND TRICKS tutorial sections."""

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_advanced_fan_out_create(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # fan-out pattern: create many agents from a list of tasks
        for task in "fix-auth" "add-logging" "update-deps" "write-docs"; do
          mngr create "$task"@.modal --no-connect --message "Work on: $task"
        done
    """)
    tasks = ("fix-auth", "add-logging", "update-deps", "write-docs")
    # Use --type command + sleep to avoid the modal claude startup time per
    # task; the test verifies the fan-out shell loop works.
    expect(
        e2e.run(
            (
                'for task in "fix-auth" "add-logging" "update-deps" "write-docs"; do'
                '   mngr create "$task" --type command --no-ensure-clean --no-connect --message "Work on: $task" -- sleep 101010 ;'
                " done"
            ),
            comment="fan-out pattern (substituted for local sleep)",
            # All four creates run in a single shell loop, so they share one
            # command budget; give them enough headroom for sequential starts.
            timeout=120.0,
        )
    ).to_succeed()
    # The loop exiting 0 only reflects the last iteration, so verify the fan-out
    # actually produced one agent per task. Scope discovery to the local
    # provider so the assertion does not depend on (or contact) Modal.
    addrs = e2e.run("mngr list --provider local --addrs", comment="verify one agent was created per task")
    expect(addrs).to_succeed()
    for task in tasks:
        expect(addrs.stdout).to_contain(f"{task}@")
    # Appearing in --addrs only proves the agent *state* was registered. Verify
    # that every fanned-out command actually launched and is alive by inspecting
    # each agent's lifecycle state. A command agent running `sleep` has a live
    # process but no user activity, so it reports WAITING rather than RUNNING;
    # both states mean the agent's own tmux pane and expected `sleep` process are
    # alive (see determine_lifecycle_state), whereas STOPPED/DONE/REPLACED would
    # mean the fanned-out command never launched or already exited. This covers
    # all four tasks, not just the single agent spot-checked via pgrep below.
    listing = e2e.run(
        "mngr list --provider local --format json",
        comment="verify every fanned-out command launched and is alive",
    )
    expect(listing).to_succeed()
    agents_by_name = {agent["name"]: agent for agent in json.loads(listing.stdout)["agents"]}
    for task in tasks:
        assert task in agents_by_name, f"{task} not in listed agents {sorted(agents_by_name)}"
        assert agents_by_name[task]["state"] in ("RUNNING", "WAITING"), agents_by_name[task]
        # The command field round-trips what the fan-out launched (`-- sleep 101010`),
        # confirming each agent is running its own task command rather than idling.
        assert "sleep 101010" in agents_by_name[task]["command"], agents_by_name[task]
    # Confirm the fan-out actually launched the task commands rather than only
    # registering agent state: exec onto an agent's (shared local) host and check
    # the sleep processes are alive. pgrep exits 0 only when it finds a match.
    proc_check = e2e.run(
        "mngr exec fix-auth \"pgrep -f 'sleep 101010'\"",
        comment="confirm the fan-out launched the task commands",
    )
    expect(proc_check).to_succeed()
    # At least one PID printed proves a matching sleep process is running.
    expect(proc_check.stdout).to_match(r"\d")


@pytest.mark.release
def test_advanced_watch_dashboard_running(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # monitor all agents in a refreshing dashboard (uses Unix watch(1))
        watch -n 5 mngr list --running
    """)
    # The e2e fixture leaves every remote provider (modal, docker) enabled, but
    # this release run has no docker daemon, so an unscoped `mngr list --running`
    # hard-fails during docker discovery. Scope the dashboard query to the
    # always-available local provider (the same approach as the fan-out test) so
    # the assertion is deterministic and does not depend on docker/modal being
    # reachable. No modal mark is needed: scoping to local never shells out to
    # the modal CLI binary that the resource guard watches.
    #
    # `watch` clears the screen and emits terminal escape codes, so for the
    # tutorial command itself we only assert that the one-shot dashboard refresh
    # exits cleanly under a 1s timeout.
    expect(
        e2e.run(
            "timeout 1 watch -n 5 mngr list --running --provider local || true",
            comment="watch refreshing dashboard",
        )
    ).to_succeed()
    # Verify the actual behavior `watch` re-runs each tick: the underlying
    # `mngr list --running` query succeeds and emits a well-formed dashboard (an
    # `agents` list -- empty here, since nothing is running).
    dashboard = e2e.run(
        "mngr list --running --provider local --format json",
        comment="dashboard query underlying the watch loop",
    )
    expect(dashboard).to_succeed()
    payload = json.loads(dashboard.stdout)
    assert payload["agents"] == [], dashboard.stdout
    # The dashboard must render cleanly: scoping to the reachable local provider
    # means there are no provider-discovery errors to report (an unreachable
    # provider would populate this list and is exactly what a live dashboard
    # surfaces to the operator).
    assert payload["errors"] == [], dashboard.stdout


@pytest.mark.release
@pytest.mark.timeout(60)
def test_advanced_observe_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or get a JSONL stream of host/agent discovery events for programmatic consumers
        mngr observe --discovery-only
    """)
    # No @pytest.mark.modal: --discovery-only only reads/lists, so it never shells
    # out to the `modal` CLI binary (the only modal usage the resource guard can
    # observe from this subprocess), which would make the mark a NEVER_INVOKED
    # violation. The discovery snapshot still reports every configured provider.
    # We also avoid creating an agent here (that would drag in rsync/tmux): the
    # stream emits a full discovery snapshot on its own, even in an empty
    # environment, which is enough to assert the documented JSONL contract.
    #
    # The stream emits an initial full discovery snapshot and then keeps running,
    # re-polling on a fixed interval, so we bound it with `timeout` (long enough to
    # observe more than one poll) and treat the resulting SIGTERM exit as success.
    result = e2e.run(
        "timeout 25 mngr observe --discovery-only || true",
        comment="JSONL stream of discovery events",
        timeout=45.0,
    )
    expect(result).to_succeed()
    # The output is a JSONL stream for programmatic consumers: every non-blank
    # line must parse as JSON, and at least one must be a full discovery
    # snapshot (the documented baseline event for state reconstruction).
    jsonl_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert jsonl_lines, f"expected JSONL discovery output but got none. stderr:\n{result.stderr}"
    events = [json.loads(line) for line in jsonl_lines]
    # Every event in the stream -- not just the snapshot -- must carry the standard
    # event envelope so programmatic consumers can attribute, order, and
    # de-duplicate events. The stream itself de-duplicates by event_id, so those
    # ids must be unique across the whole stream.
    for event in events:
        assert event["source"] == "mngr/discovery", event
        assert event["type"], event
        assert event["event_id"], event
        assert event["timestamp"], event
    event_ids = [event["event_id"] for event in events]
    assert len(event_ids) == len(set(event_ids)), f"duplicate event_ids in stream: {event_ids}"
    snapshots = [event for event in events if event.get("type") == "DISCOVERY_FULL"]
    assert snapshots, (
        f"expected a DISCOVERY_FULL snapshot in the stream, got types "
        f"{sorted({event.get('type') for event in events})}"
    )
    # `observe` is a *stream*, not a one-shot dump: it re-polls every
    # _DISCOVERY_STREAM_POLL_INTERVAL_SECONDS (10s) and emits a fresh snapshot each
    # time. Over the ~25s window we therefore expect it to re-emit at least one more
    # snapshot beyond the initial one -- this is what distinguishes the documented
    # streaming contract from a single discovery dump.
    assert len(snapshots) >= 2, (
        f"expected the stream to re-emit snapshots over time (streaming contract), "
        f"got only {len(snapshots)}"
    )
    # Verify the documented full-snapshot contract on the first snapshot: it is
    # the baseline event consumers use to reconstruct state, so it must carry the
    # discovery source plus the agents/hosts/providers collections.
    snapshot = snapshots[0]
    assert snapshot["source"] == "mngr/discovery", snapshot
    assert isinstance(snapshot["agents"], list), snapshot
    assert isinstance(snapshot["hosts"], list), snapshot
    assert isinstance(snapshot["providers"], list), snapshot
    # The snapshot reports every configured provider; the always-present local
    # provider must appear (the comment above explains why no remote markers are
    # needed). In this isolated, empty environment nothing has been created yet,
    # so the agent list is empty.
    provider_names = {provider["provider_name"] for provider in snapshot["providers"]}
    assert "local" in provider_names, provider_names
    assert snapshot["agents"] == [], snapshot


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_advanced_collect_results_loop(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # collect results from all agents
        for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do
          echo "=== $agent ==="
          mngr exec "$agent" "git log --oneline -3"
        done
    """)
    for name, sleep_value in [
        ("fix-auth", 101011),
        ("add-logging", 101012),
        ("update-deps", 101013),
        ("write-docs", 101014),
    ]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()
    agents = ("fix-auth", "add-logging", "update-deps", "write-docs")
    result = e2e.run(
        (
            'for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do'
            '   echo "=== $agent ==="; mngr exec "$agent" "git log --oneline -3";'
            " done"
        ),
        comment="collect results from all agents",
        timeout=180.0,
    )
    expect(result).to_succeed()
    # Each agent runs in a worktree of the shared test repo, so `git log` must
    # report the repo's real history (the README "Initial commit") and the exec
    # must succeed on every agent. Asserting on this exact behavior is what
    # distinguishes a working command from the previous broken invocation,
    # which printed an arg-parsing error and never ran git at all.
    combined = result.stdout + result.stderr
    for agent in agents:
        assert f"=== {agent} ===" in result.stdout, f"missing section header for {agent}: {result.stdout}"
        assert f"Command succeeded on agent {agent}" in combined, f"exec did not succeed on {agent}: {combined}"
    assert "Initial commit" in result.stdout, f"git log produced no commit history: {result.stdout}"
    assert "Not a valid agent name" not in combined, f"exec mis-parsed its command: {combined}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_advanced_collect_results_loop_missing_agent(e2e: E2eSession) -> None:
    # Unhappy path for the same collect-results tutorial block: when the agent
    # list references a name that was never created (a typo, or an agent that has
    # since been destroyed), the loop must still iterate -- echoing each section
    # header -- and surface a clear "agent not found" error for the missing one,
    # while the real agent still reports its git history. This is the failure mode
    # a user hits when one entry in their hand-maintained list goes stale.
    e2e.write_tutorial_block("""
        # collect results from all agents
        for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do
          echo "=== $agent ==="
          mngr exec "$agent" "git log --oneline -3"
        done
    """)
    expect(
        e2e.run(
            "mngr create fix-auth --type command --no-ensure-clean --no-connect -- sleep 101015",
            comment="create fix-auth",
        )
    ).to_succeed()
    # Run the collect loop over one real agent plus one that does not exist. The
    # loop's exit status reflects only its last iteration (the failing exec), so
    # we deliberately do not assert success here.
    result = e2e.run(
        (
            'for agent in "fix-auth" "ghost-agent"; do'
            '   echo "=== $agent ==="; mngr exec "$agent" "git log --oneline -3";'
            " done"
        ),
        comment="collect results when one agent in the list is missing",
        timeout=120.0,
    )
    combined = result.stdout + result.stderr
    # Both section headers print: each loop iteration echoes before running exec,
    # so a failure on one agent does not abort the loop.
    assert "=== fix-auth ===" in result.stdout, f"missing real-agent header: {result.stdout}"
    assert "=== ghost-agent ===" in result.stdout, f"missing ghost-agent header: {result.stdout}"
    # The real agent still reports its git history and a success line.
    assert "Initial commit" in result.stdout, f"real agent produced no git history: {result.stdout}"
    assert "Command succeeded on agent fix-auth" in combined, f"exec did not succeed on fix-auth: {combined}"
    # The missing agent produces a clear "not found" error rather than silently
    # succeeding, mis-parsing its command, or crashing the loop.
    assert "ghost-agent" in combined, f"error did not name the missing agent: {combined}"
    assert "Not a valid agent name" not in combined, f"exec mis-parsed its command: {combined}"
    assert "No agent" in combined or "not found" in combined.lower(), (
        f"missing agent did not produce an agent-not-found error: {combined}"
    )
    assert "Command succeeded on agent ghost-agent" not in combined, (
        f"exec must not report success for a nonexistent agent: {combined}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_advanced_create_reuse_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use --reuse to make create idempotent. This is handy, esp with remote scripts, so that you can detach, then hit up and enter
        # and not have to worry about remembering whether it is started, etc (because it will attach by default)
        mngr create --reuse --provider modal my-task
    """)
    # The tutorial relies on a default agent type; the isolated e2e environment
    # has none, so pin --type command (with a long sleep) to avoid modal claude
    # startup, matching the other modal tests in this file.
    create_cmd = (
        "mngr create --reuse --provider modal my-task --no-connect --no-ensure-clean --type command -- sleep 101020"
    )

    # First create: my-task does not exist yet, so --reuse creates it (and, since
    # the Modal environment does not exist yet, bootstraps the environment too).
    expect(e2e.run(create_cmd, comment="use --reuse to make create idempotent", timeout=150.0)).to_succeed()

    # Capture the agent ID of the newly created my-task agent. Scope the query to
    # the modal provider (where my-task lives): `mngr list` defaults to
    # --on-error abort, so an unrelated provider being unreachable (e.g. a docker
    # daemon that isn't running in this sandbox) would otherwise make this exit 1
    # even though it found the agent. This mirrors the provider-scoped discovery
    # in test_advanced_fan_out_create above.
    first_list = e2e.run(
        "mngr list --provider modal --include 'name == \"my-task\"' --ids",
        comment="record the my-task agent ID after first create",
    )
    expect(first_list).to_succeed()
    first_ids = first_list.stdout.split()
    assert len(first_ids) == 1, f"Expected exactly one my-task agent after create, got: {first_list.stdout!r}"

    # Second create with --reuse: my-task already exists, so it must be reused
    # (started/attached) rather than creating a duplicate -- this is the
    # idempotency the tutorial block is demonstrating.
    second_create = e2e.run(create_cmd, comment="re-running with --reuse reuses the existing agent", timeout=150.0)
    expect(second_create).to_succeed()
    # Verify the second create actually took the reuse code path rather than
    # provisioning a fresh agent: it must report "Reusing existing agent" and
    # must NOT re-create the host or bootstrap the Modal environment again.
    second_output = second_create.stdout + second_create.stderr
    assert "Reusing existing agent" in second_output, (
        f"Expected --reuse to report reusing the existing agent, got: {second_output}"
    )
    assert "Created Modal environment" not in second_output, (
        f"--reuse must not re-bootstrap the Modal environment: {second_output}"
    )
    assert "Creating host" not in second_output, f"--reuse must not create a new host: {second_output}"

    # The reuse must not have created a second agent: same single ID as before.
    second_list = e2e.run(
        "mngr list --provider modal --include 'name == \"my-task\"' --ids",
        comment="verify --reuse did not create a duplicate agent",
    )
    expect(second_list).to_succeed()
    second_ids = second_list.stdout.split()
    assert second_ids == first_ids, (
        f"Expected --reuse to reuse the same single agent; before={first_ids}, after={second_ids}"
    )


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_advanced_watch_list_live_dashboard(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use watch with list to keep a live dashboard in a terminal
        watch -n 5 mngr list
    """)
    # Create an agent so the live dashboard has something to display.
    _create_my_task(e2e, 101017)
    # `mngr list` is the content the dashboard refreshes. Run it directly (under
    # `watch` with a sub-second timeout it is killed during mngr's cold start
    # before producing output) so we can assert the agent actually shows up.
    # Scope to the local provider so the verification depends only on the local
    # agent we just created: with the default `--on-error abort`, an enabled-but-
    # unreachable remote provider (e.g. Docker not running) would abort the whole
    # listing with a non-zero exit even though the local row is printed. The
    # tutorial command below still runs the unscoped `mngr list` under `watch`.
    list_result = e2e.run("mngr list --provider local", comment="the live dashboard content")
    expect(list_result).to_succeed()
    # The dashboard row shows the agent name alongside its live state (RUNNING or
    # WAITING depending on timing for the sleep command agent).
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")
    # Now run the actual tutorial command: watch refreshes every 5s, and a short
    # timeout confirms the dashboard launches and exits cleanly.
    expect(
        e2e.run("timeout 1 watch -n 5 mngr list || true", comment="watch with list for a live dashboard")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_tips_exec_env_inspect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use exec to quickly inspect an agent's environment
        mngr exec my-task -- env | sort
    """)
    _create_my_task(e2e, 101015)
    # Capture the id mngr records for my-task so we can confirm exec ran inside
    # *that* agent's environment, not merely that some env was dumped. Only one
    # agent exists, so `mngr list --ids` prints exactly its id. Scope discovery
    # to the local provider so the cross-check does not depend on (or contact)
    # the remote providers left enabled in the e2e fixture -- this test is not
    # marked @pytest.mark.modal/docker, and an unreachable docker daemon would
    # otherwise make an unscoped `mngr list` exit non-zero.
    list_result = e2e.run(
        "mngr list --provider local --ids", comment="get the agent id to cross-check the exec env"
    )
    expect(list_result).to_succeed()
    agent_id = list_result.stdout.strip()
    assert agent_id, f"expected an agent id from `mngr list --ids`, got: {list_result.stdout!r}"
    result = e2e.run("mngr exec my-task -- env | sort", comment="quickly inspect an agent's environment")
    expect(result).to_succeed()
    # Verify exec actually ran inside the agent's environment, not just that the
    # pipeline exited cleanly: the `| sort` pipe means the shell exit code is
    # sort's, so a clean exit alone would mask an exec failure. mngr injects
    # these agent-identifying variables into every agent's environment, and the
    # injected id must match the one mngr records for my-task.
    expect(result.stdout).to_contain("MNGR_AGENT_NAME=my-task")
    expect(result.stdout).to_contain(f"MNGR_AGENT_ID={agent_id}")
    # The tutorial pipes env through `sort`; confirm the env block really is
    # sorted so the demonstrated pipeline behaves as shown.
    env_var_lines = [
        line.strip() for line in result.stdout.splitlines() if "=" in line and line.split("=", 1)[0].strip().isupper()
    ]
    assert env_var_lines == sorted(env_var_lines), "expected the env output to be sorted by `sort`"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_tips_exec_filtered_hosts(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or use exec to see something across a bunch of hosts by combining with mngr list:
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'
    """)
    # This example is modal-only, so disable the docker provider. Otherwise
    # discovery tries to reach a docker daemon and `mngr list` exits non-zero
    # when none is running (e.g. in CI, which executes release tests inside the
    # release image with no docker daemon). An unrelated provider being down
    # should not block exec'ing across modal hosts -- this mirrors the remedy
    # mngr prints for an unavailable provider.
    expect(
        e2e.run(
            "mngr config set --scope user providers.docker.is_enabled false",
            comment="disable the docker provider (unused by this modal-only example)",
            timeout=30.0,
        )
    ).to_succeed()
    # Create a modal command agent so the filtered list has a real modal host to
    # fan out across (the tutorial demonstrates exec'ing over modal hosts).
    expect(
        e2e.run(
            "mngr create my-task@.modal --type command --no-connect --no-ensure-clean -- sleep 101015",
            comment="create a modal command agent to exec across",
            timeout=180.0,
        )
    ).to_succeed()
    # The filtered list should return exactly the modal agent we just created.
    list_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids",
        comment="list ids of modal hosts",
        timeout=60.0,
    )
    expect(list_result).to_succeed()
    agent_id = list_result.stdout.strip()
    assert agent_id, "expected the filtered list to return the modal agent id"
    # Pipe the filtered ids into exec and confirm the command actually ran on the
    # modal host: it should echo that host's MNGR_AGENT_ID and dump its env.
    result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'",
        comment="exec across filtered hosts",
        timeout=120.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain(agent_id)
    expect(result.stdout).to_contain(f"MNGR_AGENT_ID={agent_id}")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_tips_xargs_parallel_exec(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to get really fancy, you can use xargs to run in parallel across hosts:
        mngr list --include 'host.provider == "modal"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'
    """)
    # The tutorial pipeline only ever talks to modal hosts, but `mngr list`
    # discovers across every enabled provider before applying the
    # `host.provider == "modal"` filter. If an unrelated provider (e.g. docker)
    # is not reachable, discovery records an error and `mngr list` exits 1 even
    # though the modal ids print correctly. Disable the docker provider in this
    # test's isolated user config so the modal-only tutorial does not hinge on a
    # provider it never uses. This is setup, not part of the demonstrated block.
    expect(
        e2e.run(
            "mngr config set --scope user providers.docker.is_enabled false",
            comment="scope discovery to modal: the tutorial never touches docker",
            timeout=60.0,
        )
    ).to_succeed()
    # Stand up a real modal host (a cheap `sleep` command agent) so the pipeline
    # has something to fan out across. Without a host, `mngr list` returns no ids
    # and xargs runs nothing, making the test a no-op. Creating the host also
    # invokes the `modal` CLI (environment create + deploy), which is what the
    # @pytest.mark.modal guard tracks.
    expect(
        e2e.run(
            "mngr create parallel-task --provider modal --type command --no-ensure-clean --no-connect -- sleep 101017",
            comment="create a modal command agent to fan out across",
            timeout=240.0,
        )
    ).to_succeed()
    # Sanity: the modal filter discovers the host we just created. Capture its id
    # so we can confirm the pipeline actually exec'd on it. `mngr list --ids`
    # prints `{id}`, which equals the `$MNGR_AGENT_ID` exported into the exec env.
    list_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids",
        comment="list modal host ids",
        timeout=60.0,
    )
    expect(list_result).to_succeed()
    agent_id = list_result.stdout.strip()
    assert agent_id, f"expected a modal host id from `mngr list --ids`, got: {list_result.stdout!r}"
    # The tutorial command: fan `mngr exec` out across the modal hosts in
    # parallel. `mngr list` discovery is a cold network round-trip, so this needs
    # a per-command timeout well above the default 10s pytest signal timeout
    # (overridden via @pytest.mark.timeout above).
    result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'",
        comment="xargs to run in parallel across hosts",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # Verify the exec actually ran on the modal host: its agent id (echoed from
    # $MNGR_AGENT_ID) and a `pwd` path both appear in the captured output. The
    # `pwd` line is the agent's absolute work_dir, so require a line that begins
    # with `/` rather than merely "contains a slash" (which a stray warning could
    # satisfy). With `xargs -P 5` the two echoed lines can interleave, so match
    # any line, not a fixed position.
    output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert agent_id in result.stdout, f"expected agent id {agent_id!r} in exec output, got: {result.stdout!r}"
    assert any(line.startswith("/") for line in output_lines), (
        f"expected an absolute `pwd` path line in exec output, got: {result.stdout!r}"
    )


def _seed_claude_transcript(host_dir: Path, events: list[dict[str, Any]]) -> None:
    """Re-type the lone local agent to ``claude`` and seed a common transcript.

    ``mngr transcript`` only accepts agent types that produce a common
    transcript (claude), and it reads the events the agent's transcript
    streamer would normally emit. Creating a real claude agent in CI is
    infeasible (it requires accepting Claude Code's interactive trust dialog),
    so -- mirroring the ``create_agent_with_sample_transcript`` unit fixture --
    we create a cheap command agent, mark its recorded type as ``claude`` in
    ``data.json``, and seed a known transcript. The transcript command never
    runs the agent; it only reads these files, so this faithfully exercises its
    --tail/--role behavior while staying deterministic.

    The local agent stores its data under ``$MNGR_HOST_DIR/agents/<id>/`` (see
    libs/mngr/docs/conventions.md), with the common transcript at
    ``events/claude/common_transcript/``.
    """
    agent_dirs = [p for p in (host_dir / "agents").iterdir() if (p / "data.json").exists()]
    assert len(agent_dirs) == 1, f"expected exactly one agent dir, found: {agent_dirs}"
    agent_dir = agent_dirs[0]
    data_path = agent_dir / "data.json"
    data = json.loads(data_path.read_text())
    data["type"] = "claude"
    data_path.write_text(json.dumps(data))
    transcript_dir = agent_dir / "events" / "claude" / "common_transcript"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_tips_transcript_tail_assistant(e2e: E2eSession, temp_host_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # check the transcript to see what an agent has been up to
        # (helpful to see the last messages without even having to bring the host back online!)
        mngr transcript my-task --tail 5 --role assistant
    """)
    _create_my_task(e2e, 101016)
    # Seven assistant turns interleaved with user turns; --tail 5 --role
    # assistant should surface only the last five assistant messages.
    events: list[dict[str, Any]] = []
    for i in range(1, 8):
        events.append(
            {
                "timestamp": f"2026-01-01T00:00:{2 * i - 1:02d}Z",
                "type": "user_message",
                "role": "user",
                "content": f"USER_MSG_{i}",
            }
        )
        events.append(
            {
                "timestamp": f"2026-01-01T00:00:{2 * i:02d}Z",
                "type": "assistant_message",
                "role": "assistant",
                "text": f"ASSISTANT_MSG_{i}",
                "tool_calls": [],
            }
        )
    _seed_claude_transcript(temp_host_dir, events)

    result = e2e.run(
        "mngr transcript my-task --tail 5 --role assistant",
        comment="check the transcript to see what an agent has been up to",
    )
    expect(result).to_succeed()
    # The role filter is applied *before* --tail, so the output is the last five
    # *assistant* messages (3..7), not the assistant messages among the last five
    # *events* (which would be only 5..7). Verify the full expected set is present.
    for i in (3, 4, 5, 6, 7):
        assert f"ASSISTANT_MSG_{i}" in result.stdout, result.stdout
    # The two oldest assistant messages fall outside the tail window.
    assert "ASSISTANT_MSG_1" not in result.stdout, result.stdout
    assert "ASSISTANT_MSG_2" not in result.stdout, result.stdout
    # The role filter excludes every user message.
    assert "USER_MSG" not in result.stdout, result.stdout
    # Messages are shown oldest-first (chronological), so the tail boundary
    # message precedes the most recent one in the rendered output.
    assert result.stdout.index("ASSISTANT_MSG_3") < result.stdout.index("ASSISTANT_MSG_7"), result.stdout
    # Confirm the exact count and that every surfaced event is an assistant
    # message by re-running with machine-readable output (mirrors how a human
    # would sanity-check the cap against the raw events).
    jsonl = e2e.run(
        "mngr transcript my-task --tail 5 --role assistant --format jsonl",
        comment="same query as JSONL to verify the exact event count and roles",
    )
    expect(jsonl).to_succeed()
    lines = [json.loads(line) for line in jsonl.stdout.splitlines() if line.strip()]
    assert len(lines) == 5, f"expected exactly 5 tailed assistant events, got {len(lines)}: {jsonl.stdout}"
    assert all(event["type"] == "assistant_message" for event in lines), jsonl.stdout
