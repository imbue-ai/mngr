"""Tests for the LABELS AND FILTERING tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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

    # Verify both labels were actually attached to the created agent, not just
    # that the create command exited 0.
    list_result = e2e.run("mngr list --format json", comment="Verify both labels appear in JSON output")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1, f"expected exactly one 'my-task' agent, got {len(matching_agents)}"
    assert matching_agents[0]["labels"]["team"] == "backend"
    assert matching_agents[0]["labels"]["priority"] == "high"


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_filter_by_label_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list agents filtered by label using CEL expressions
        mngr list --include 'labels.priority == "high"'
    """)
    # Set up two agents with different priority labels so the CEL filter has
    # something to both include and exclude. These run on the local provider:
    # the command never creates Modal state, so it does not invoke the Modal
    # CLI and must not carry @pytest.mark.modal.
    expect(
        e2e.run(
            "mngr create high-pri --type command --no-ensure-clean --no-connect --label priority=high -- sleep 100933",
            comment="create a high-priority agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create low-pri --type command --no-ensure-clean --no-connect --label priority=low -- sleep 100934",
            comment="create a low-priority agent",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr list --include 'labels.priority == \"high\"'",
        comment="filter by label using CEL",
    )
    expect(result).to_succeed()
    # The CEL filter must keep the matching agent and drop the non-matching one.
    expect(result.stdout).to_contain("high-pri")
    expect(result.stdout).not_to_contain("low-pri")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_combine_include_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple filters (AND logic for --include, all must match)
        mngr list --include 'labels.team == "backend"' --include 'state == "RUNNING"'
    """)
    # Set up agents that exercise both clauses of the AND filter:
    #   - backend-running:  labels.team == backend, still running
    #   - frontend-running: labels.team == frontend (fails the team clause)
    #   - backend-stopped:  labels.team == backend but STOPPED (fails the state clause)
    # Pin a unique sleep value per agent so leaked processes trace back to the create call.
    for name, label, sleep_seconds in [
        ("backend-running", "team=backend", 100201),
        ("frontend-running", "team=frontend", 100202),
        ("backend-stopped", "team=backend", 100203),
    ]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect --label {label} -- sleep {sleep_seconds}",
                comment=f"create {name} with {label}",
            )
        ).to_succeed()
    # Stop one backend agent so it fails the state clause of the combined filter.
    expect(e2e.run("mngr stop backend-stopped", comment="stop one backend agent")).to_succeed()

    # Baseline: filtering on the team clause alone keeps both backend agents and
    # drops the frontend one. This is the set the second clause further narrows.
    team_only = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --format json",
        comment="filter on the team clause alone",
    )
    expect(team_only).to_succeed()
    team_only_names = {agent["name"] for agent in json.loads(team_only.stdout)["agents"]}
    assert team_only_names == {"backend-running", "backend-stopped"}, team_only_names

    # The combined filter ANDs both clauses, so every returned agent must match
    # team == backend AND state == RUNNING. frontend-running fails the team
    # clause and backend-stopped fails the state clause, so neither may appear.
    combined = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"RUNNING\"' --format json",
        comment="combine multiple --include filters (AND)",
    )
    expect(combined).to_succeed()
    combined_agents = json.loads(combined.stdout)["agents"]
    combined_names = {agent["name"] for agent in combined_agents}
    assert "frontend-running" not in combined_names, combined_names
    assert "backend-stopped" not in combined_names, combined_names
    # Whatever survives the AND must satisfy both clauses simultaneously.
    for agent in combined_agents:
        assert agent["labels"]["team"] == "backend", agent
        assert agent["state"] == "RUNNING", agent

    # Positive AND case: with a second clause that one backend agent does
    # satisfy, the intersection is exactly that agent. backend-stopped matches
    # team == backend AND state == STOPPED; backend-running fails the state
    # clause and frontend-running fails the team clause. (This team+state
    # combination is the one the tutorial's destroy example uses.)
    combined_stopped = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"STOPPED\"' --format json",
        comment="combined AND filter with a positive match",
    )
    expect(combined_stopped).to_succeed()
    combined_stopped_names = {agent["name"] for agent in json.loads(combined_stopped.stdout)["agents"]}
    assert combined_stopped_names == {"backend-stopped"}, combined_stopped_names


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_list_exclude_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # exclude agents matching a filter
        mngr list --exclude 'labels.team == "frontend"'
    """)
    # Set up agents on two different teams so the exclusion is actually
    # observable (an empty list would let any filter "succeed" vacuously).
    expect(
        e2e.run(
            "mngr create frontend-agent --type command --no-ensure-clean --no-connect --label team=frontend -- sleep 100200",
            comment="create a frontend-team agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create backend-agent --type command --no-ensure-clean --no-connect --label team=backend -- sleep 100201",
            comment="create a backend-team agent",
        )
    ).to_succeed()

    # exclude agents matching a filter
    result = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"'",
        comment="exclude agents matching a filter",
    )
    expect(result).to_succeed()
    # The frontend-team agent must be excluded; the backend-team agent must remain.
    expect(result.stdout).to_contain("backend-agent")
    assert "frontend-agent" not in result.stdout, f"frontend-agent should have been excluded:\n{result.stdout}"

    # Confirm the same exclusion structurally via JSON so the assertion does not
    # depend on the human-readable table layout.
    json_result = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"' --format json",
        comment="exclude agents matching a filter (JSON for a robust assertion)",
    )
    expect(json_result).to_succeed()
    names = {agent["name"] for agent in json.loads(json_result.stdout)["agents"]}
    assert names == {"backend-agent"}, f"expected only backend-agent to remain, got {names}"


@pytest.mark.timeout(180)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_list_combine_exclude_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple exclusion filters (OR logic for --exclude, any can match)
        mngr list --exclude 'labels.team == "frontend"' --exclude 'labels.team == "devops"'
    """)
    # Create one agent per team so the OR-logic exclusion actually has agents to act on.
    for index, (name, team) in enumerate(
        (("frontend-svc", "frontend"), ("devops-svc", "devops"), ("backend-svc", "backend"))
    ):
        expect(
            e2e.run(
                f"mngr create {name} --label team={team} --type command --no-ensure-clean --no-connect "
                f"-- sleep {100931 + index}",
                comment=f"create a {team} agent for filtering",
            )
        ).to_succeed()

    result = e2e.run(
        "mngr list --exclude 'labels.team == \"frontend\"' --exclude 'labels.team == \"devops\"' --format json",
        comment="combine multiple --exclude filters (OR)",
    )
    expect(result).to_succeed()
    # --exclude uses OR logic: an agent is dropped if it matches ANY filter, so both
    # the frontend and devops agents are excluded while the backend agent remains.
    remaining = {agent["name"] for agent in json.loads(result.stdout)["agents"]}
    assert remaining == {"backend-svc"}, f"expected only backend-svc to remain, got {remaining}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_compound_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just do combined filters directly in the CEL expression:
        mngr list --include 'labels.team == "backend" && state == "RUNNING"'
    """)
    # Set up labelled agents so the compound expression has data to act on. The
    # filter keeps only agents that are BOTH labelled team=backend AND in the
    # RUNNING state. Use local command agents (sleeping) -- they are fast and
    # deterministic, and an idle `sleep` settles into the WAITING state, which
    # lets us prove that the `state == "RUNNING"` half of the conjunction is
    # actually enforced.
    expect(
        e2e.run(
            "mngr create backend-task --provider local --label team=backend --type command "
            "--no-ensure-clean --no-connect -- sleep 100941",
            comment="create a backend agent (idle, so its state is WAITING)",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create frontend-task --provider local --label team=frontend --type command "
            "--no-ensure-clean --no-connect -- sleep 100942",
            comment="create a frontend agent to confirm the label clause discriminates",
        )
    ).to_succeed()

    # Baseline: the label clause on its own selects the backend agent and
    # excludes the frontend one.
    label_only = e2e.run(
        "mngr list --include 'labels.team == \"backend\"'",
        comment="label clause alone selects the backend agent",
    )
    expect(label_only).to_succeed()
    assert "backend-task" in label_only.stdout, label_only.stdout
    assert "frontend-task" not in label_only.stdout, label_only.stdout

    # The exact tutorial command ANDs that label clause with state == "RUNNING".
    # Both agents are idle (WAITING), so the conjunction now excludes the backend
    # agent too -- demonstrating that BOTH predicates of the compound expression
    # are applied (the same result as the previous two-`--include` form).
    result = e2e.run(
        'mngr list --include \'labels.team == "backend" && state == "RUNNING"\'',
        comment="combine filters in a single CEL expression",
    )
    expect(result).to_succeed()
    assert "backend-task" not in result.stdout, result.stdout
    assert "frontend-task" not in result.stdout, result.stdout


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_message_filtered_backend(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with other commands: message only backend agents by passing "-" to have the list of matching agents piped in via stdin
        mngr list --include 'labels.team == "backend"' --ids | mngr message - -m "Please run the backend test suite"
    """)
    # Create one backend-labeled agent (the intended message target) and one
    # frontend-labeled agent (which the filter must exclude) so the filter+stdin
    # pipeline has real agents to act on and we can verify it targets only the
    # backend one.
    for name, team in (("backend-agent", "backend"), ("frontend-agent", "frontend")):
        expect(
            e2e.run(
                f"mngr create {name} --label team={team} --type command --no-ensure-clean --no-connect -- sleep 100930",
                comment=f"create {name} labeled team={team}",
                timeout=120.0,
            )
        ).to_succeed()
    result = e2e.run(
        'mngr list --include \'labels.team == "backend"\' --ids | mngr message - -m "Please run the backend test suite"',
        comment="message only backend agents via filter+stdin",
        timeout=120.0,
    )
    expect(result).to_succeed()
    # The message must reach the backend agent and skip the frontend agent.
    expect(result.stdout).to_contain("backend-agent")
    expect(result.stdout).not_to_contain("frontend-agent")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_exec_filtered_remote_disk(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with exec: check disk usage on remote agents only
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - "df -h /workspace"
    """)
    # Create a real Modal agent so the host.provider filter has something to
    # match and the exec actually runs on a remote host (df -h /workspace).
    # The work directory is mounted at /workspace (via --target-path) so the
    # path exists on the remote host. Without an agent the filter matches
    # nothing, exec is a no-op, and Modal is never exercised. A command-type
    # agent (sleeping) keeps the host alive cheaply without needing Claude.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --target-path /workspace --type command --no-connect --no-ensure-clean -- sleep 100942",
            comment="create a remote Modal agent to filter and exec on",
            timeout=120.0,
        )
    ).to_succeed()
    result = e2e.run(
        'mngr list --include \'host.provider == "modal"\' --ids | mngr exec - "df -h /workspace"',
        comment="exec across remote agents only",
    )
    expect(result).to_succeed()
    # Verify the exec actually ran df on the remote host: df -h prints a header
    # row ("Filesystem ... Use% Mounted on"). A zero exit code already proves
    # /workspace exists on the host (df errors on a missing path). The per-agent
    # success line ties the output back to the Modal agent we filtered to.
    expect(result.stdout).to_contain("Filesystem")
    expect(result.stdout).to_contain("Mounted on")
    expect(result.stdout).to_contain("Command succeeded on agent my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_destroy_filtered_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with destroy: clean up all stopped agents for a team
        mngr list --include 'labels.team == "backend"' --include 'state == "STOPPED"' --ids | mngr destroy - --force --dry-run
    """)
    # Set up a stopped, backend-labeled agent so the filter has a concrete
    # target to preview. A local command agent is sufficient here -- the
    # dry-run never touches a remote provider, so this test is not marked
    # @pytest.mark.modal (it would otherwise fail the resource guard for
    # carrying a mark it never exercises).
    expect(
        e2e.run(
            "mngr create backend-task --label team=backend --type command --no-ensure-clean --no-connect -- sleep 100930",
            comment="create a backend-labeled agent to target",
        )
    ).to_succeed()
    expect(e2e.run("mngr stop backend-task", comment="stop it so its state becomes STOPPED")).to_succeed()
    expect(e2e.run("mngr list", comment="confirm the agent is STOPPED").stdout).to_match(r"backend-task\s+STOPPED")

    # The actual tutorial command: dry-run destroy of all stopped backend agents.
    dry_run_result = e2e.run(
        "mngr list --include 'labels.team == \"backend\"' --include 'state == \"STOPPED\"' --ids | mngr destroy - --force --dry-run",
        comment="dry-run destroy via filter+stdin",
    )
    expect(dry_run_result).to_succeed()
    # The dry-run must preview the matched agent...
    expect(dry_run_result.stdout).to_contain("backend-task")

    # ...but must NOT actually destroy it: the agent still exists afterward.
    list_after = e2e.run("mngr list", comment="verify the dry-run left the agent intact")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_match(r"backend-task\s+STOPPED")


@pytest.mark.release
@pytest.mark.modal
def test_list_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just list agents by filtering using jq:
        mngr list --format json | jq '.agents[] | select(.labels.priority == "high")'
    """)
    expect(
        e2e.run(
            "mngr list --format json | jq '.agents[] | select(.labels.priority == \"high\")'",
            comment="list with jq filter",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_jsonl_jq_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or even stream the filters with jq by using jsonl:
        mngr list --format jsonl | jq --unbuffered 'select(.labels.priority == "high")'
    """)
    # The LABELS tutorial section first creates labeled agents and then filters
    # them. Seed two agents with different priority labels so the streaming jq
    # filter has a real line to select (priority=high) and a real line to drop
    # (priority=low) -- this verifies the jsonl stream parses and the label
    # filter actually discriminates, rather than passing on an empty fleet.
    expect(
        e2e.run(
            "mngr create high-task --label priority=high --type command"
            " --no-connect --no-ensure-clean -- sleep 100000",
            comment="seed a high-priority agent for the filter to select",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create low-task --label priority=low --type command --no-connect --no-ensure-clean -- sleep 100000",
            comment="seed a low-priority agent the filter must drop",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr list --format jsonl | jq --unbuffered 'select(.labels.priority == \"high\")'",
        comment="stream jq filter via jsonl",
    )
    expect(result).to_succeed()
    # The streamed, jq-filtered output must contain the high-priority agent and
    # must NOT contain the low-priority one -- proving the label filter matched.
    expect(result.stdout).to_contain("high-task")
    expect(result.stdout).not_to_contain("low-task")
