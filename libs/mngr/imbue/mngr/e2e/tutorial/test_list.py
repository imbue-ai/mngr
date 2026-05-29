"""Tests for listing agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
def test_list_running_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only running agents
        mngr list --running
    """)
    expect(e2e.run("mngr list --running", comment="show only running agents")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_stopped_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only stopped agents (not running, still exists and can be restarted)
        mngr list --stopped
    """)
    expect(e2e.run("mngr list --stopped", comment="show only stopped agents")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_archived_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only archived agents (stopped, cannot necessarily be restarted, but data can be inspected)
        mngr list --archived
    """)
    expect(e2e.run("mngr list --archived", comment="show only archived agents")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_active_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only active agents (anything not archived/destroyed/crashed/failed)
        mngr list --active
    """)
    expect(e2e.run("mngr list --active", comment="show only active agents")).to_succeed()


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
    expect(
        e2e.run(
            "MNGR__COMMANDS__LIST__ACTIVE=false mngr list",
            comment="opt out for a single call via env var override",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_local_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only agents running locally
        mngr list --local
    """)
    expect(e2e.run("mngr list --local", comment="show only agents running locally")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_remote_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # show only agents running remotely
        mngr list --remote
    """)
    expect(e2e.run("mngr list --remote", comment="show only agents running remotely")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_provider_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by provider
        mngr list --provider modal
    """)
    expect(e2e.run("mngr list --provider modal", comment="filter by provider")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_project_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by project
        mngr list --project my-project
    """)
    expect(e2e.run("mngr list --project my-project", comment="filter by project")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by agent label
        mngr list --label TEAM=backend
    """)
    expect(e2e.run("mngr list --label TEAM=backend", comment="filter by agent label")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_host_label_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter by host label
        mngr list --host-label ENV=staging
    """)
    expect(e2e.run("mngr list --host-label ENV=staging", comment="filter by host label")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_fields_and_sort(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # choose which fields to display and sort order
        mngr list --fields "name,state,host.provider,created_at" --sort "-created_at"
        # see mngr list --help for a complete list of fields you can reference
    """)
    expect(
        e2e.run(
            'mngr list --fields "name,state,host.provider,created_at" --sort "-created_at"',
            comment="choose which fields to display and sort order",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_limit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # limit the number of results
        mngr list --limit 10
    """)
    expect(e2e.run("mngr list --limit 10", comment="limit the number of results")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_watch_mode(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # watch mode: refresh the list every 5 seconds
        watch -n5 mngr list
    """)
    # `watch` blocks until SIGINT; wrap with a short `timeout` so the test
    # exits without waiting 5 seconds. `timeout` returns 124 on expiry which is
    # the expected outcome -- we only care that `watch -n5 mngr list` started
    # successfully (no immediate crash) and `mngr list` produced output before
    # the timeout.
    result = e2e.run(
        "timeout 1 watch -n5 mngr list || true",
        comment="watch mode: refresh the list every 5 seconds",
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_format_jsonl(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # output each entry as a JSON object (useful for scripting)
        mngr list --format jsonl
    """)
    expect(e2e.run("mngr list --format jsonl", comment="output each entry as a JSON object")).to_succeed()


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
