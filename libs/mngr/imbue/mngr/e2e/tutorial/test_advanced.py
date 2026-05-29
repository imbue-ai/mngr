"""Tests for the ADVANCED WORKFLOWS and TIPS AND TRICKS tutorial sections."""

import base64
import json
import re

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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_advanced_fan_out_create(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # fan-out pattern: create many agents from a list of tasks
        for task in "fix-auth" "add-logging" "update-deps" "write-docs"; do
          mngr create "$task"@.modal --no-connect --message "Work on: $task"
        done
    """)
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
        )
    ).to_succeed()

    # Verify the fan-out actually produced four independent agents, one per task
    # in the loop, each running its own command in its own worktree.
    list_result = e2e.run("mngr list --format json", comment="verify all fanned-out agents exist")
    expect(list_result).to_succeed()
    agents_by_name = {agent["name"]: agent for agent in json.loads(list_result.stdout)["agents"]}

    expected_tasks = ["fix-auth", "add-logging", "update-deps", "write-docs"]
    missing = [task for task in expected_tasks if task not in agents_by_name]
    assert not missing, f"fan-out did not create agents for: {missing} (got {sorted(agents_by_name)})"

    work_dirs = set()
    for task in expected_tasks:
        agent = agents_by_name[task]
        assert agent["type"] == "command", f"{task}: expected a command agent, got type {agent['type']!r}"
        assert agent["state"] in ("RUNNING", "WAITING"), (
            f"{task}: expected an alive agent, got state {agent['state']!r}"
        )
        assert "sleep 101010" in agent["command"], f"{task}: unexpected command {agent['command']!r}"
        work_dirs.add(agent["work_dir"])

    # Each fanned-out agent gets its own worktree so they don't conflict.
    assert len(work_dirs) == len(expected_tasks), f"expected distinct work dirs per agent, got {sorted(work_dirs)}"


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_advanced_watch_dashboard_running(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # monitor all agents in a refreshing dashboard (uses Unix watch(1))
        watch -n 5 mngr list --running
    """)
    # No @pytest.mark.modal: `mngr list --running` does not invoke modal in this
    # fresh environment. The modal backend raises ProviderEmptyError at
    # construction when its per-user environment does not exist yet (see
    # get_all_provider_instances), so list skips modal entirely and makes no
    # guarded gRPC call.
    #
    # Create an agent so the dashboard's data source actually has something in
    # it. A `sleep` command agent is idle (produces no output), so it settles
    # into the WAITING lifecycle state rather than RUNNING.
    _create_my_task(e2e, sleep_value=101015)
    # The dashboard is powered by `mngr list`; verify the created agent is
    # discoverable there.
    full_list = e2e.run("mngr list", comment="full agent list backing the dashboard")
    expect(full_list).to_succeed()
    expect(full_list.stdout).to_contain("my-task")
    # `--running` filters to RUNNING agents only (state == "RUNNING"). The idle
    # sleep agent is WAITING, so the dashboard's --running view excludes it --
    # this verifies the state filter that distinguishes this block from plain
    # `mngr list`.
    running_list = e2e.run("mngr list --running", comment="dashboard payload (running only)")
    expect(running_list).to_succeed()
    expect(running_list.stdout).not_to_contain("my-task")
    # Run the tutorial command itself. watch(1) runs the command immediately,
    # then every 5s; timeout kills watch after 1s so the test does not hang.
    expect(
        e2e.run("timeout 1 watch -n 5 mngr list --running || true", comment="watch refreshing dashboard")
    ).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(90)
def test_advanced_observe_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or get a JSONL stream of host/agent discovery events for programmatic consumers
        mngr observe --discovery-only
    """)
    # The stream runs until interrupted, so cap it with timeout and let the full
    # snapshot it writes accumulate on stdout. timeout exits non-zero when it
    # kills the stream, hence `|| true`; the real assertion is on the JSONL.
    result = e2e.run(
        "timeout 30 mngr observe --discovery-only || true",
        comment="JSONL stream of discovery events",
        timeout=60.0,
    )
    expect(result).to_succeed()
    # Every emitted line must be a JSON object carrying a discovery event "type",
    # and the stream must include the authoritative DISCOVERY_FULL snapshot that
    # programmatic consumers reconstruct host/agent state from.
    event_types = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        assert "type" in event, f"discovery event missing 'type': {line}"
        event_types.append(event["type"])
    assert "DISCOVERY_FULL" in event_types, f"expected a DISCOVERY_FULL snapshot, got types: {event_types}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_advanced_collect_results_loop(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # collect results from all agents (the command must be quoted--it's the last arg to mngr exec)
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
    result = e2e.run(
        (
            'for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do'
            '   echo "=== $agent ==="; mngr exec "$agent" "git log --oneline -3";'
            " done"
        ),
        comment="collect results from all agents",
    )
    expect(result).to_succeed()
    # The for-loop's exit code only reflects its last iteration, so verify each
    # agent individually: its header is printed and its exec reported success.
    # The repo's initial commit must also appear, proving the command actually
    # ran inside the agent's checkout rather than the loop merely exiting 0.
    for name in ("fix-auth", "add-logging", "update-deps", "write-docs"):
        expect(result.stdout).to_contain(f"=== {name} ===")
        expect(result.stdout).to_contain(f"Command succeeded on agent {name}")
    expect(result.stdout).to_contain("Initial commit")


def _modal_task_addrs(e2e: E2eSession, name: str) -> list[str]:
    """Return the addresses (name@host.provider) of modal agents with the given name."""
    result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --addrs",
        comment="list modal agent addresses to verify reuse idempotency",
        timeout=60.0,
    )
    expect(result).to_succeed()
    return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith(f"{name}@")]


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_advanced_create_reuse_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use --reuse to make create idempotent. This is handy, esp with remote scripts, so that you can detach, then hit up and enter
        # and not have to worry about remembering whether it is started, etc (because it will attach by default)
        mngr create --reuse --provider modal my-task
    """)
    # Use --type command + sleep to avoid the modal claude startup time; the
    # test verifies the --reuse idempotency behavior, not the agent runtime.
    create_command = (
        "mngr create --reuse --provider modal my-task --type command --no-connect --no-ensure-clean -- sleep 101017"
    )
    # First invocation: the agent does not exist yet, so --reuse creates it.
    expect(
        e2e.run(create_command, comment="use --reuse to make create idempotent", timeout=150.0)
    ).to_succeed()
    addrs_after_create = _modal_task_addrs(e2e, "my-task")
    assert len(addrs_after_create) == 1, f"expected exactly one my-task agent after create, got {addrs_after_create}"

    # Second invocation: the agent already exists, so --reuse must reuse it
    # (idempotency) rather than creating a duplicate. The address (which encodes
    # the host) must be unchanged, proving the same agent/host was reused.
    expect(
        e2e.run(create_command, comment="re-run create --reuse to confirm it is idempotent", timeout=150.0)
    ).to_succeed()
    addrs_after_reuse = _modal_task_addrs(e2e, "my-task")
    assert addrs_after_reuse == addrs_after_create, (
        "--reuse should reuse the existing agent on the same host, not create a duplicate: "
        f"before={addrs_after_create}, after={addrs_after_reuse}"
    )


@pytest.mark.release
def test_advanced_watch_list_live_dashboard(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use watch with list to keep a live dashboard in a terminal
        watch -n 5 mngr list
    """)
    # `watch -n 5 mngr list` just re-runs `mngr list` on an interval, so run it
    # directly first to verify the dashboard actually renders. Asserting only on
    # the watch command below is hollow: `timeout 1 ... || true` exits 0 even if
    # `mngr list` never produced any output within the one-second window.
    list_result = e2e.run("mngr list", comment="list agents for the live dashboard")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("No agents found")
    # Run the tutorial command itself: watch refreshing the list dashboard.
    # `timeout 1` stops watch after one second; `|| true` swallows timeout's
    # 124 exit code so the command still reports success.
    expect(
        e2e.run("timeout 1 watch -n 5 mngr list || true", comment="watch with list for a live dashboard")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_tips_exec_env_inspect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use exec to quickly inspect an agent's environment
        mngr exec my-task -- env | sort
    """)
    _create_my_task(e2e, 101015)
    result = e2e.run("mngr exec my-task -- env | sort", comment="quickly inspect an agent's environment")
    expect(result).to_succeed()
    # Verify exec actually ran `env` inside the agent: the output must include
    # mngr's injected per-agent variables, and `sort` must have ordered the
    # lines. Checking the concrete effect rather than just the exit code.
    expect(result.stdout).to_contain("MNGR_AGENT_NAME=my-task")
    expect(result.stdout).to_contain("MNGR_AGENT_ID=")
    env_lines = [line for line in result.stdout.splitlines() if re.match(r"^[A-Z][A-Z0-9_]*=", line)]
    assert env_lines == sorted(env_lines), f"env output is not sorted:\n{result.stdout}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_tips_exec_filtered_hosts(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or use exec to see something across a bunch of hosts by combining with mngr list:
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'
    """)
    # The tutorial command only does anything visible when at least one Modal
    # host exists, so first create a Modal agent for the filter to match. This
    # setup also satisfies @pytest.mark.modal: create shells out to the modal
    # CLI (environment create + function deploy), which the resource guard
    # tracks, whereas a bare `mngr list` with no Modal hosts never does.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 101020",
            comment="create a Modal agent for the filter to match",
            timeout=240.0,
        )
    ).to_succeed()
    result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'",
        comment="exec across filtered hosts",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # Verify the command actually ran on the Modal host rather than no-op'ing on
    # an empty host list: the sorted env contains MNGR_AGENT_ID (set on every
    # agent host) and the human-readable footer confirms the per-agent success.
    expect(result.stdout).to_contain("MNGR_AGENT_ID=")
    expect(result.stdout).to_contain("Command succeeded on agent")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_tips_xargs_parallel_exec(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to get really fancy, you can use xargs to run in parallel across hosts:
        mngr list --include 'host.provider == "modal"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'
    """)
    # Create two modal command agents so the xargs pipeline actually fans out
    # across multiple modal hosts (rather than running against an empty list).
    # Sleeping command agents start faster than the default claude agent and
    # stay RUNNING so exec can reach them. Creating real modal sandboxes is also
    # what genuinely invokes modal, satisfying the @pytest.mark.modal guard.
    for name, sleep_value in [("fanout-a", 101030), ("fanout-b", 101031)]:
        expect(
            e2e.run(
                f"mngr create {name} --provider modal --type command --no-connect --no-ensure-clean -- sleep {sleep_value}",
                comment=f"create modal command agent {name}",
                timeout=180.0,
            )
        ).to_succeed()

    # Capture the modal agent ids up front so we can assert the parallel exec
    # actually reached every host.
    ids_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids",
        comment="list ids of the modal agents",
    )
    expect(ids_result).to_succeed()
    agent_ids = ids_result.stdout.split()
    assert len(agent_ids) == 2, f"expected 2 modal agent ids, got {agent_ids!r}"

    result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'",
        comment="xargs to run in parallel across hosts",
        timeout=120.0,
    )
    expect(result).to_succeed()
    # Every modal host should have echoed its own MNGR_AGENT_ID...
    for agent_id in agent_ids:
        expect(result.stdout).to_contain(agent_id)
    # ...and printed an absolute path from `pwd` on the remote host.
    expect(result.stdout).to_match(r"(?m)^/")


# Synthetic common-transcript used to exercise `mngr transcript`. Six assistant
# messages (so --tail 5 must drop the oldest) interleaved with user messages and
# a tool result (so --role assistant must filter them out).
_SEED_TRANSCRIPT_EVENTS: tuple[dict[str, object], ...] = (
    {"type": "user_message", "role": "user", "content": "USER_MSG_1", "timestamp": "2026-01-01T00:00:00Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_1", "timestamp": "2026-01-01T00:00:01Z"},
    {
        "type": "tool_result",
        "role": "tool",
        "tool_name": "Bash",
        "output": "TOOL_OUT_1",
        "is_error": False,
        "timestamp": "2026-01-01T00:00:02Z",
    },
    {"type": "user_message", "role": "user", "content": "USER_MSG_2", "timestamp": "2026-01-01T00:00:03Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_2", "timestamp": "2026-01-01T00:00:04Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_3", "timestamp": "2026-01-01T00:00:05Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_4", "timestamp": "2026-01-01T00:00:06Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_5", "timestamp": "2026-01-01T00:00:07Z"},
    {"type": "assistant_message", "role": "assistant", "text": "ASSISTANT_MSG_6", "timestamp": "2026-01-01T00:00:08Z"},
)


def _setup_agent_with_transcript(e2e: E2eSession, agent_name: str, sleep_value: int) -> None:
    """Create an agent that looks (to ``mngr transcript``) like a real claude agent.

    The tutorial assumes ``my-task`` is a claude agent that has produced a
    transcript, but ``mngr transcript`` only renders transcripts for agent
    types that emit one. Provisioning a real claude agent requires the claude
    binary plus a trust dialog, so the rest of the e2e suite (and this test)
    use a lightweight ``--type command`` sleep agent instead.

    To reproduce the claude scenario on top of that cheap agent, we set the
    recorded agent type to ``claude`` and seed a synthetic common-transcript
    directly into its on-host events directory -- the same shape that
    ``create_agent_with_sample_transcript`` builds for the unit tests. The
    whole setup script is base64-encoded and piped to ``bash`` on the host
    (via ``mngr exec``) so the write is portable across local and remote hosts
    and free of shell-quoting hazards.
    """
    _create_my_task(e2e, sleep_value)
    jsonl = "".join(json.dumps(event) + "\n" for event in _SEED_TRANSCRIPT_EVENTS)
    setup_script = f"""\
set -e
events_dir="$MNGR_AGENT_STATE_DIR/events/claude/common_transcript"
mkdir -p "$events_dir"
cat > "$events_dir/events.jsonl" <<'JSONL'
{jsonl}JSONL
python3 - "$MNGR_AGENT_STATE_DIR/data.json" <<'PY'
import json
import sys

data_json_path = sys.argv[1]
with open(data_json_path) as handle:
    data = json.load(handle)
data["type"] = "claude"
with open(data_json_path, "w") as handle:
    json.dump(data, handle, indent=2)
PY
"""
    encoded = base64.b64encode(setup_script.encode("utf-8")).decode("ascii")
    # `mngr exec AGENT COMMAND` runs COMMAND as a single shell string on the
    # host, so pass the whole pipeline as one quoted argument (base64 keeps it
    # free of characters that would need escaping).
    expect(
        e2e.run(
            f"mngr exec {agent_name} 'echo {encoded} | base64 -d | bash'",
            comment="seed a synthetic claude common-transcript on the agent's host",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_tips_transcript_tail_assistant(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check the transcript to see what an agent has been up to
        # (helpful to see the last messages without even having to bring the host back online!)
        mngr transcript my-task --tail 5 --role assistant
    """)
    _setup_agent_with_transcript(e2e, "my-task", 101016)
    result = e2e.run(
        "mngr transcript my-task --tail 5 --role assistant",
        comment="check the transcript to see what an agent has been up to",
    )
    expect(result).to_succeed()
    # --role assistant keeps only assistant messages; --tail 5 keeps the last
    # five of those (dropping ASSISTANT_MSG_1).
    expect(result.stdout).to_contain("ASSISTANT_MSG_6")
    expect(result.stdout).to_contain("ASSISTANT_MSG_2")
    expect(result.stdout).not_to_contain("ASSISTANT_MSG_1")
    # user and tool messages must be filtered out by --role assistant.
    expect(result.stdout).not_to_contain("USER_MSG")
    expect(result.stdout).not_to_contain("TOOL_OUT")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_tips_transcript_unsupported_agent_type(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: `mngr transcript` only works for
    # agent types that produce a common transcript. A plain command agent does
    # not, so the command must fail with a clear, actionable error rather than
    # emitting empty output or hanging.
    e2e.write_tutorial_block("""
        # check the transcript to see what an agent has been up to
        # (helpful to see the last messages without even having to bring the host back online!)
        mngr transcript my-task --tail 5 --role assistant
    """)
    _create_my_task(e2e, 101017)
    result = e2e.run(
        "mngr transcript my-task --tail 5 --role assistant",
        comment="transcript fails for an agent type that does not produce one",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("does not produce a common transcript")
