"""Tests for the SCRIPTING AND AUTOMATION tutorial section."""

import tomllib
from pathlib import Path
import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_headless_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run in headless mode (no interactive prompts)
        mngr create my-task --headless --no-connect --message "Do the thing"
    """)
    expect(
        e2e.run(
            'mngr create my-task --headless --no-connect --type command --no-ensure-clean --message "Do the thing" -- sleep 101000',
            comment="run in headless mode",
        )
    ).to_succeed()

    # --no-connect must still leave the agent created and running in the
    # background. Verify the concrete on-host effect directly (rather than via
    # `mngr list`, which would trigger remote-provider discovery): the agent's
    # command process is actually running.
    expect(
        e2e.run('pgrep -f "sleep 101000"', comment="verify the agent's command process is running")
    ).to_succeed()


@pytest.mark.release
def test_config_set_headless_globally(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
        # or set headless globally
        mngr config set headless true
    """)
    set_result = e2e.run("mngr config set headless true", comment="set headless globally")
    expect(set_result).to_succeed()
    # The command reports which scope/file it wrote (project scope by default).
    expect(set_result.stdout).to_contain("Set headless = true")

    # Verify the value was actually persisted, not just that the command exited
    # 0. `set` defaults to project scope, which writes the project settings.toml
    # inside the test repo's config dir. Read that file directly -- this mirrors
    # how a human would confirm the change and, unlike re-invoking `mngr`, does
    # not re-load the freshly-written file (which lacks the test-only
    # `is_allowed_in_pytest` opt-in and would trip the pytest config guard).
    settings_path = project_config_dir / "settings.toml"
    assert settings_path.exists(), f"expected `set` to create {settings_path}"
    persisted = tomllib.loads(settings_path.read_text())
    # It must be the boolean `true`, not the string "true" -- the value is
    # JSON-parsed before being written.
    assert persisted.get("headless") is True, persisted


@pytest.mark.release
def test_config_set_rejects_unknown_key(e2e: E2eSession, project_config_dir: Path) -> None:
    # Unhappy path for the same `mngr config set` command: an unknown config key
    # must be rejected (config set validates the resulting document in strict
    # mode before saving), and crucially the rejected write must not be
    # persisted to disk.
    e2e.write_tutorial_block("""
        # or set headless globally
        mngr config set headless true
    """)
    bad_result = e2e.run(
        "mngr config set not_a_real_setting true",
        comment="reject an unknown config key",
    )
    expect(bad_result).to_fail()
    expect(bad_result.stderr).to_contain("Unknown configuration")
    # Validation happens before the file is written, so no project settings.toml
    # should have been created by the rejected set.
    assert not (project_config_dir / "settings.toml").exists(), "rejected set must not persist a settings file"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_reuse_and_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # idempotent creation: reuse an existing agent if it already exists
        mngr create worker --reuse --provider modal --no-connect && mngr message worker -m "Process the queue"
    """)
    # The tutorial omits --type because real users have a default type
    # configured; the isolated test environment has none, so we pin --type
    # command (a sleep agent) to keep the run hermetic and message-able.
    chained_command = (
        "mngr create worker --reuse --provider modal --no-connect --no-ensure-clean --type command -- sleep 100400"
        ' && mngr message worker -m "Process the queue"'
    )

    # First run: worker does not exist yet, so --reuse falls through to a fresh
    # create, then the message lands on the new agent.
    expect(e2e.run(chained_command, comment="idempotent create + message", timeout=180.0)).to_succeed()

    # Capture the freshly created worker's id so we can prove the second
    # invocation reuses it rather than provisioning a duplicate.
    first_list = e2e.run("mngr list --format json", comment="snapshot agents after first create")
    expect(first_list).to_succeed()
    first_workers = [a for a in json.loads(first_list.stdout)["agents"] if a["name"] == "worker"]
    assert len(first_workers) == 1, f"expected exactly one 'worker' after first create, got: {first_list.stdout}"
    original_worker_id = first_workers[0]["id"]

    # Second run of the identical command: this is the actual idempotency path
    # the tutorial illustrates. The existing agent must be reused (not recreated),
    # which the CLI reports on stderr.
    reuse_result = e2e.run(chained_command, comment="re-run is idempotent: existing worker is reused", timeout=180.0)
    expect(reuse_result).to_succeed()
    expect(reuse_result.stderr).to_contain("Reusing existing agent")

    # The reuse must not have created a second agent, and the surviving agent
    # must be the same one (same id) created on the first run.
    second_list = e2e.run("mngr list --format json", comment="snapshot agents after reuse")
    expect(second_list).to_succeed()
    second_workers = [a for a in json.loads(second_list.stdout)["agents"] if a["name"] == "worker"]
    assert len(second_workers) == 1, f"--reuse created a duplicate worker: {second_list.stdout}"
    assert second_workers[0]["id"] == original_worker_id, "--reuse recreated the agent instead of reusing it"


@pytest.mark.release
def test_get_json_into_var(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get JSON output for parsing in scripts
        AGENT_INFO=$(mngr list --format json)
    """)
    # Capture into a shell variable exactly as the tutorial shows, then echo it
    # back so the captured JSON lands on stdout where we can parse it. mngr's
    # warnings go to stderr, so $AGENT_INFO holds only the JSON document.
    result = e2e.run(
        'AGENT_INFO=$(mngr list --format json) && echo "$AGENT_INFO"',
        comment="get JSON output for parsing in scripts",
    )
    expect(result).to_succeed()
    # The point of --format json is machine-parseable output, so verify the
    # captured value really is JSON with the documented top-level shape rather
    # than merely a non-empty string.
    parsed = json.loads(result.stdout)
    assert isinstance(parsed["agents"], list), parsed
    assert isinstance(parsed["errors"], list), parsed


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_get_json_into_var_includes_created_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get JSON output for parsing in scripts
        AGENT_INFO=$(mngr list --format json)
    """)
    # A script consuming `mngr list --format json` acts on the agents it finds,
    # so create one first and confirm it shows up in the parsed listing.
    expect(
        e2e.run(
            "mngr create json-target --type command --no-ensure-clean --no-connect -- sleep 100199",
            comment="create an agent to appear in the JSON listing",
        )
    ).to_succeed()
    result = e2e.run(
        'AGENT_INFO=$(mngr list --format json) && echo "$AGENT_INFO"',
        comment="get JSON output for parsing in scripts",
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    agent_names = [agent["name"] for agent in parsed["agents"]]
    assert "json-target" in agent_names, parsed


@pytest.mark.release
def test_observe_discovery_pipe_python(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use discovery stream for streaming results into other tools
        mngr observe --discovery-only | while read -r line; do
          echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('name', 'unknown'))"
        done
    """)
    # observe blocks indefinitely; wrap with timeout so the while-loop exits.
    expect(
        e2e.run(
            'timeout 1 bash -c \'mngr observe --discovery-only | while read -r line; do echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get(\\"name\\", \\"unknown\\"))"; done\' || true',
            comment="pipe discovery stream into python (timeout-capped)",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(120)
def test_usage_wait_and_create(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # `mngr usage wait` blocks until a CEL predicate over the current usage snapshot
        # evaluates true, then exits 0 (exit 2 on --timeout). Compose it with `mngr create`
        # to opportunistically spawn work when you're near the end of a rate-limit window
        # with spare capacity -- the predicate below means "more than 75% of the 5h
        # window has elapsed AND under half the limit has been used", so there's budget
        # headroom that would otherwise reset unused.
        # The CEL context per source matches `mngr usage --format json` sources[i]; see
        # the `mngr usage wait --help` page for the full field list.
        mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
          && mngr create chore@.modal --no-connect --message "Find and fix an issue in the codebase."
    """)
    # There's no usage data in the isolated test env, so the predicate never
    # matches. With --timeout 1 the wait gives up after ~1s (it honors the
    # timeout even though the default poll interval is 30s) and exits 2; the
    # `&&` then short-circuits the chained create.
    result = e2e.run(
        "mngr usage wait --timeout 1 --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50'"
        ' && mngr create chore@.modal --no-connect --message "Find and fix an issue in the codebase."',
        comment="opportunistic create gated by usage wait (timeout-capped)",
    )
    # Exit code 2 is `mngr usage wait`'s documented timeout code, and it
    # propagates as the chained command's exit code because `&&` skips the
    # create when the wait fails.
    expect(result).to_have_exit_code(2)
    # Concretely verify the create was skipped: no `chore` agent exists.
    listing = e2e.run("mngr list --format json", comment="confirm the gated create did not run")
    expect(listing).to_succeed()
    expect(listing.stdout).not_to_contain("chore")
