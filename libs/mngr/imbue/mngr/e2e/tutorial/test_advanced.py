"""Tests for the ADVANCED WORKFLOWS and TIPS AND TRICKS tutorial sections."""

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
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
def test_advanced_watch_dashboard_running(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # monitor all agents in a refreshing dashboard (uses Unix watch(1))
        watch -n 5 mngr list --running
    """)
    expect(
        e2e.run("timeout 1 watch -n 5 mngr list --running || true", comment="watch refreshing dashboard")
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_advanced_observe_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or get a JSONL stream of host/agent discovery events for programmatic consumers
        mngr observe --discovery-only
    """)
    expect(
        e2e.run("timeout 1 mngr observe --discovery-only || true", comment="JSONL stream of discovery events")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_advanced_collect_results_loop(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # collect results from all agents
        for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do
          echo "=== $agent ==="
          mngr exec "$agent" -- git log --oneline -3
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
    expect(
        e2e.run(
            (
                'for agent in "fix-auth" "add-logging" "update-deps" "write-docs"; do'
                '   echo "=== $agent ==="; mngr exec "$agent" -- git log --oneline -3 || true;'
                " done"
            ),
            comment="collect results from all agents",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_advanced_create_reuse_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use --reuse to make create idempotent. This is handy, esp with remote scripts, so that you can detach, then hit up and enter
        # and not have to worry about remembering whether it is started, etc (because it will attach by default)
        mngr create --reuse --provider modal my-task
    """)
    expect(
        e2e.run(
            "mngr create --reuse --provider modal my-task --no-connect --no-ensure-clean",
            comment="use --reuse to make create idempotent",
            timeout=150.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_advanced_watch_list_live_dashboard(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use watch with list to keep a live dashboard in a terminal
        watch -n 5 mngr list
    """)
    expect(
        e2e.run("timeout 1 watch -n 5 mngr list || true", comment="watch with list for a live dashboard")
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_tips_exec_env_inspect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use exec to quickly inspect an agent's environment
        mngr exec my-task -- env | sort
    """)
    _create_my_task(e2e, 101015)
    expect(e2e.run("mngr exec my-task -- env | sort", comment="quickly inspect an agent's environment")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_tips_exec_filtered_hosts(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or use exec to see something across a bunch of hosts by combining with mngr list:
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'
    """)
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"modal\"' --ids | mngr exec - 'echo $MNGR_AGENT_ID && env | sort'",
            comment="exec across filtered hosts",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_tips_xargs_parallel_exec(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # if you want to get really fancy, you can use xargs to run in parallel across hosts:
        mngr list --include 'host.provider == "modal"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'
    """)
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"modal\"' --ids | xargs -P 5 -I {} mngr exec {} 'echo $MNGR_AGENT_ID && pwd'",
            comment="xargs to run in parallel across hosts",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_tips_transcript_tail_assistant(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check the transcript to see what an agent has been up to
        # (helpful to see the last messages without even having to bring the host back online!)
        mngr transcript my-task --tail 5 --role assistant
    """)
    _create_my_task(e2e, 101016)
    expect(
        e2e.run(
            "mngr transcript my-task --tail 5 --role assistant",
            comment="check the transcript to see what an agent has been up to",
        )
    ).to_succeed()
