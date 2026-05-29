"""Tests for the LABELS AND FILTERING tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_with_multiple_labels(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create agents with labels for organization
        mngr create my-task --label team=backend --label priority=high
    """)
    expect(
        e2e.run(
            "mngr create my-task --label team=backend --label priority=high --type command --no-ensure-clean --no-connect -- sleep 100930",
            comment="create agents with labels for organization",
        )
    ).to_succeed()

    # Verify the labels were actually attached to the created agent, not just
    # that the create command exited 0. List as JSON and inspect the agent.
    # The list call's provider-discovery setup exceeds the global 10s timeout,
    # hence the @pytest.mark.timeout(180) override above.
    list_result = e2e.run(
        "mngr list --format json",
        comment="list agents to verify the labels were applied",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    agents_by_name = {agent["name"]: agent for agent in json.loads(list_result.stdout)["agents"]}
    assert "my-task" in agents_by_name, f"expected agent 'my-task' to exist, found {sorted(agents_by_name)}"
    labels = agents_by_name["my-task"]["labels"]
    assert labels.get("team") == "backend", f"expected label team=backend, got labels={labels}"
    assert labels.get("priority") == "high", f"expected label priority=high, got labels={labels}"


# NOTE: no @pytest.mark.modal here. A bare `mngr list` does discovery-only across
# providers (is_environment_creation_allowed=False), so it never shells out to the
# `modal` CLI (the chokepoint the resource guard tracks across subprocesses). The
# in-process Modal SDK monkeypatch does not propagate into the `mngr` subprocess,
# so marking this test @pytest.mark.modal fails the guard's superfluous-mark check.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_list_filter_by_label_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list agents filtered by label using CEL expressions
        mngr list --include 'labels.priority == "high"'
    """)
    # Create two agents with different priority labels so the CEL filter has
    # something to discriminate between -- otherwise the command runs against an
    # empty environment and the assertion would not exercise filtering at all.
    expect(
        e2e.run(
            "mngr create high-prio --label priority=high --type command --no-ensure-clean --no-connect -- sleep 100931",
            comment="create a high-priority agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create low-prio --label priority=low --type command --no-ensure-clean --no-connect -- sleep 100932",
            comment="create a low-priority agent",
        )
    ).to_succeed()
    # list agents filtered by label using CEL expressions
    result = e2e.run(
        "mngr list --include 'labels.priority == \"high\"'",
        comment="filter by label using CEL",
    )
    expect(result).to_succeed()
    # Only the high-priority agent should match the CEL expression.
    expect(result.stdout).to_contain("high-prio")
    expect(result.stdout).not_to_contain("low-prio")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_combine_include_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple filters (AND logic for --include, all must match)
        mngr list --include 'labels.team == "backend"' --include 'state == "RUNNING"'
    """)
    # Create three agents that each isolate one part of the AND. Command agents
    # running a long `sleep` settle in the WAITING lifecycle state (RUNNING is
    # reserved for interactive agents that write an "active" marker, which is too
    # heavy to spin up here), so we exercise the AND against WAITING/STOPPED:
    #   - backend + WAITING -> matches the team filter and (state == WAITING)
    #   - frontend + WAITING -> fails the team filter   (should be excluded)
    #   - backend + STOPPED -> fails the state filter   (should be excluded)
    expect(
        e2e.run(
            "mngr create match-both --type command --no-ensure-clean --no-connect --label team=backend -- sleep 100940",
            comment="backend agent left alive (matches the team filter)",
            timeout=180.0,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create wrong-team --type command --no-ensure-clean --no-connect --label team=frontend -- sleep 100941",
            comment="frontend agent (fails the team filter)",
            timeout=120.0,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create wrong-state --type command --no-ensure-clean --no-connect --label team=backend -- sleep 100942",
            comment="backend agent that we stop (fails the state filter)",
            timeout=120.0,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr stop wrong-state",
            comment="stop the backend agent so it leaves the alive state",
            timeout=60.0,
        )
    ).to_succeed()

    # Run the exact tutorial command. None of our agents are RUNNING, so the AND
    # correctly yields nothing: this proves the state filter is actually applied
    # (the WAITING backend agent is excluded rather than leaking through).
    tutorial_result = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"RUNNING\"' --format json",
        comment="combine multiple filters (AND logic for --include, all must match)",
    )
    expect(tutorial_result).to_succeed()
    running_backends = {agent["name"] for agent in json.loads(tutorial_result.stdout)["agents"]}
    assert running_backends == set(), (
        f"No agent is both backend and RUNNING, so the AND filter must return nothing, got: {running_backends}"
    )

    # Verify the AND positively with a state our agents actually reach: only the
    # backend agent that is still alive (WAITING) matches both filters. The
    # frontend agent is dropped by the team filter and the stopped backend agent
    # by the state filter.
    all_result = e2e.run("mngr list --format json", comment="snapshot of all agents for context")
    waiting_result = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"WAITING\"' --format json",
        comment="combine multiple --include filters (AND) against an observable state",
    )
    expect(waiting_result).to_succeed()
    all_agents = json.loads(all_result.stdout)["agents"]
    matching_names = {agent["name"] for agent in json.loads(waiting_result.stdout)["agents"]}
    assert matching_names == {"match-both"}, (
        f"Expected only the backend+alive agent to match both --include filters, got: {matching_names}\n"
        f"ALL AGENTS: {[(a['name'], a.get('state'), a.get('labels')) for a in all_agents]}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_exclude_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # exclude agents matching a filter
        mngr list --exclude 'labels.team == "frontend"'
    """)
    # Create labeled agents so the exclude filter has something concrete to act on.
    # Local command agents keep the test fast and deterministic; the exclude logic
    # is provider-agnostic, so no remote provider is needed here.
    expect(
        e2e.run(
            "mngr create frontend-agent --label team=frontend --type command --no-ensure-clean --no-connect -- sleep 100516",
            comment="create a frontend agent that the exclude filter should drop",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create backend-agent --label team=backend --type command --no-ensure-clean --no-connect -- sleep 100517",
            comment="create a backend agent that the exclude filter should keep",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"'",
        comment="exclude agents matching a filter",
    )
    expect(result).to_succeed()
    # The frontend agent matches the exclude filter and must be dropped, while
    # the backend agent does not match and must remain.
    expect(result.stdout).to_contain("backend-agent")
    expect(result.stdout).not_to_contain("frontend-agent")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_exclude_filter_no_match(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # exclude agents matching a filter
        mngr list --exclude 'labels.team == "frontend"'
    """)
    # Edge case: when the exclude filter matches no agent, nothing is dropped and
    # every agent is still listed. Neither created agent is on the "frontend" team.
    expect(
        e2e.run(
            "mngr create infra-agent --label team=infra --type command --no-ensure-clean --no-connect -- sleep 100518",
            comment="create an infra agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create backend-agent --label team=backend --type command --no-ensure-clean --no-connect -- sleep 100519",
            comment="create a backend agent",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"'",
        comment="exclude agents matching a filter that matches nothing",
    )
    expect(result).to_succeed()
    # No agent is on the frontend team, so the exclude filter drops nothing.
    expect(result.stdout).to_contain("infra-agent")
    expect(result.stdout).to_contain("backend-agent")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(300)
def test_list_combine_exclude_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple exclusion filters (OR logic for --exclude, any can match)
        mngr list --exclude 'labels.team == "frontend"' --exclude 'labels.team == "devops"'
    """)
    # Create a labeled agent so the exclusion filters have something to act on.
    # Using the Modal provider also exercises the Modal CLI (satisfying the
    # resource guard, which a read-only `mngr list` would not). A `command`
    # agent running `sleep` avoids needing any agent credentials.
    expect(
        e2e.run(
            "mngr create backend-agent --provider modal --type command --label team=backend --no-connect --no-ensure-clean -- sleep 100204",
            comment="create a backend-team agent to filter against",
            timeout=180.0,
        )
    ).to_succeed()

    # The backend agent matches neither exclusion clause, so the OR-exclude
    # keeps it.
    kept = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"' --exclude 'labels.team == \"devops\"'",
        comment="combine multiple --exclude filters (OR)",
    )
    expect(kept).to_succeed()
    expect(kept.stdout).to_contain("backend-agent")

    # OR logic, "any can match": adding a clause that *does* match the backend
    # agent drops it, even though the other clause does not match.
    dropped = e2e.run(
        "mngr list --exclude 'labels.team == \"backend\"' --exclude 'labels.team == \"devops\"'",
        comment="an agent matching any exclusion clause is dropped",
    )
    expect(dropped).to_succeed()
    expect(dropped.stdout).not_to_contain("backend-agent")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_compound_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just do combined filters directly in the CEL expression:
        mngr list --include 'labels.team == "backend" && state == "RUNNING"'
    """)
    expect(
        e2e.run(
            'mngr list --include \'labels.team == "backend" && state == "RUNNING"\'',
            comment="combine filters in a single CEL expression",
            timeout=90.0,
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_compound_cel_excludes_non_matching(e2e: E2eSession) -> None:
    """A non-matching agent is excluded by the compound CEL expression.

    Shares the tutorial block with ``test_list_compound_cel`` but verifies the
    actual filtering behavior: a real agent that fails the ``labels.team ==
    "backend"`` clause must be absent from the filtered result even though it is
    present in an unfiltered listing. This exercises the compound expression
    against a real agent rather than only checking that the command succeeds on
    an empty environment.
    """
    e2e.write_tutorial_block("""
        # you can also just do combined filters directly in the CEL expression:
        mngr list --include 'labels.team == "backend" && state == "RUNNING"'
    """)
    expect(
        e2e.run(
            "mngr create frontend-task --label team=frontend --type command --no-ensure-clean --no-connect -- sleep 100937",
            comment="create a non-matching (frontend) agent",
            timeout=120.0,
        )
    ).to_succeed()

    # Sanity check: the agent really exists in an unfiltered listing, so that
    # the filtered result below is empty because of the filter, not because the
    # environment is empty.
    unfiltered = e2e.run("mngr list --format json", comment="confirm the agent exists unfiltered", timeout=90.0)
    expect(unfiltered).to_succeed()
    unfiltered_names = [agent["name"] for agent in json.loads(unfiltered.stdout)["agents"]]
    assert unfiltered_names == ["frontend-task"], f"Expected only 'frontend-task', got {unfiltered_names}"

    filtered = e2e.run(
        'mngr list --include \'labels.team == "backend" && state == "RUNNING"\' --format json',
        comment="combine filters in a single CEL expression",
        timeout=90.0,
    )
    expect(filtered).to_succeed()
    filtered_agents = json.loads(filtered.stdout)["agents"]
    assert filtered_agents == [], f"Expected the frontend agent to be excluded, got {filtered_agents}"


@pytest.mark.release
@pytest.mark.timeout(60)
def test_message_filtered_backend(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with other commands: message only backend agents by passing "-" to have the list of matching agents piped in via stdin
        mngr list --include 'labels.team == "backend"' --ids | mngr message - -m "Please run the backend test suite"
    """)
    # No backend agents exist in the test env, so the filtered id list is empty
    # and the message becomes a no-op. We assert the pipeline succeeds end to
    # end -- that's the contract the tutorial is illustrating. Provider
    # discovery (e.g. probing the unavailable Docker daemon) makes this slower
    # than the default 10s timeout, hence the explicit timeout mark.
    expect(
        e2e.run(
            'mngr list --include \'labels.team == "backend"\' --ids | mngr message - -m "Please run the backend test suite"',
            comment="message only backend agents via filter+stdin",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_message_filtered_backend_targets_only_backend(e2e: E2eSession) -> None:
    """Same tutorial block, but with real agents so we can verify the filter
    actually narrows the message to backend agents.

    Creates one backend-labeled agent and one frontend-labeled agent, then runs
    the tutorial's filter+stdin pipeline and asserts the message landed on the
    backend agent only -- the frontend agent must be untouched.
    """
    e2e.write_tutorial_block("""
        # use filters with other commands: message only backend agents by passing "-" to have the list of matching agents piped in via stdin
        mngr list --include 'labels.team == "backend"' --ids | mngr message - -m "Please run the backend test suite"
    """)
    expect(
        e2e.run(
            "mngr create backend-task --label team=backend --type command --no-ensure-clean --no-connect -- sleep 100307",
            comment="create a backend-labeled agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create frontend-task --label team=frontend --type command --no-ensure-clean --no-connect -- sleep 100308",
            comment="create a frontend-labeled agent",
        )
    ).to_succeed()
    # The filter must select the backend agent and exclude the frontend one.
    # (--ids prints opaque agent ids, so we use a name template here to assert
    # on the selection by name.)
    names_result = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --format '{name}'",
        comment="list names of backend agents only",
    )
    expect(names_result).to_succeed()
    expect(names_result.stdout).to_contain("backend-task")
    expect(names_result.stdout).not_to_contain("frontend-task")
    # Running the full pipeline must message the backend agent and only it.
    result = e2e.run(
        'mngr list --include \'labels.team == "backend"\' --ids | mngr message - -m "Please run the backend test suite"',
        comment="message only backend agents via filter+stdin",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("backend-task")
    expect(result.stdout).not_to_contain("frontend-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(600)
def test_exec_filtered_remote_disk(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with exec: check disk usage on remote agents only
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - "df -h /workspace"
    """)
    # The filtered exec only does something meaningful when a Modal agent
    # actually exists, so first create one whose work directory lives at
    # /workspace (the canonical Modal workspace path used elsewhere in the
    # tutorial, e.g. `mngr create my-task@.modal:/workspace`). This also
    # ensures Modal is genuinely exercised, satisfying @pytest.mark.modal.
    expect(
        e2e.run(
            "mngr create check-disk@.modal:/workspace --type command --no-connect --no-ensure-clean -- sleep 100942",
            comment="create a remote Modal agent whose workspace lives at /workspace",
            timeout=300.0,
        )
    ).to_succeed()

    result = e2e.run(
        'mngr list --include \'host.provider == "modal"\' --ids | mngr exec - "df -h /workspace"',
        comment="exec across remote agents only",
        timeout=120.0,
    )
    expect(result).to_succeed()
    # df printed its report for the remote agent's /workspace filesystem.
    expect(result.stdout).to_contain("Filesystem")
    expect(result.stdout).to_contain("check-disk")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_destroy_filtered_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with destroy: clean up all stopped agents for a team
        mngr list --include 'labels.team == "backend"' --include 'state == "STOPPED"' --ids | mngr destroy - --force --dry-run
    """)
    # A real labeled agent is required for the filter to match anything; create
    # one on Modal so the dry-run has a concrete target to (not) destroy.
    expect(
        e2e.run(
            "mngr create backend-task --provider modal --label team=backend --type command "
            "--no-connect --no-ensure-clean -- sleep 100000",
            comment="create a backend-labeled agent to filter on",
            timeout=180.0,
        )
    ).to_succeed()

    # The exact tutorial command: clean up stopped backend agents. The agent we
    # just created is RUNNING (not STOPPED), so this matches nothing and is a
    # safe no-op -- but it must still exit 0 and destroy nothing.
    stopped_dry_run = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"STOPPED\"' --ids | mngr destroy - --force --dry-run",
        comment="dry-run destroy via filter+stdin",
        timeout=60.0,
    )
    expect(stopped_dry_run).to_succeed()
    # Nothing matched the STOPPED filter, so nothing should be reported destroyed.
    expect(stopped_dry_run.stdout).not_to_contain("Destroyed agent:")

    # Now exercise the dry-run preview against a filter that DOES match (the
    # running backend agent). It must report the agent as a destroy candidate
    # without actually destroying it.
    matching_dry_run = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --ids | mngr destroy - --force --dry-run",
        comment="preview which backend agents would be destroyed",
        timeout=60.0,
    )
    expect(matching_dry_run).to_succeed()
    expect(matching_dry_run.stdout).to_contain("backend-task")
    expect(matching_dry_run.stdout).not_to_contain("Destroyed agent:")

    # The dry-run must not have destroyed anything: the agent is still listed
    # (list without --ids shows the human-readable name).
    remaining = e2e.run(
        "mngr list --include 'labels.team == \"backend\"'",
        comment="confirm the agent survived the dry-run",
        timeout=60.0,
    )
    expect(remaining).to_succeed()
    expect(remaining.stdout).to_contain("backend-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just list agents by filtering using jq:
        mngr list --format json | jq '.agents[] | select(.labels.priority == "high")'
    """)
    # Create agents with differing priority labels so the jq filter has real
    # data to select from. The high-priority agent runs on modal so that the
    # subsequent `mngr list` genuinely exercises modal discovery.
    expect(
        e2e.run(
            "mngr create high-prio --provider modal --label priority=high --type command "
            "--no-ensure-clean --no-connect -- sleep 100942",
            comment="create a high-priority agent on modal",
            timeout=180.0,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create low-prio --label priority=low --type command "
            "--no-ensure-clean --no-connect -- sleep 100943",
            comment="create a low-priority agent locally",
        )
    ).to_succeed()
    # you can also just list agents by filtering using jq
    result = e2e.run(
        "mngr list --format json | jq '.agents[] | select(.labels.priority == \"high\")'",
        comment="list with jq filter",
    )
    expect(result).to_succeed()
    # the filter must return only the high-priority agent, not the low one
    expect(result.stdout).to_contain("high-prio")
    expect(result.stdout).not_to_contain("low-prio")


@pytest.mark.release
@pytest.mark.modal
def test_list_jsonl_jq_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or even stream the filters with jq by using jsonl:
        mngr list --format jsonl | jq --unbuffered 'select(.labels.priority == "high")'
    """)
    expect(
        e2e.run(
            "mngr list --format jsonl | jq --unbuffered 'select(.labels.priority == \"high\")'",
            comment="stream jq filter via jsonl",
        )
    ).to_succeed()
