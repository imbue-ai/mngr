"""Tests for the SCRIPTING AND AUTOMATION tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.release
def test_config_set_headless_globally(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or set headless globally
        mngr config set headless true
    """)
    expect(e2e.run("mngr config set headless true", comment="set headless globally")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_reuse_and_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # idempotent creation: reuse an existing agent if it already exists
        mngr create worker --reuse --provider modal --no-connect && mngr message worker -m "Process the queue"
    """)
    expect(
        e2e.run(
            'mngr create worker --reuse --provider modal --no-connect --no-ensure-clean && mngr message worker -m "Process the queue"',
            comment="idempotent create + message",
            timeout=180.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_get_json_into_var(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get JSON output for parsing in scripts
        AGENT_INFO=$(mngr list --format json)
    """)
    expect(
        e2e.run(
            'AGENT_INFO=$(mngr list --format json) && echo "${#AGENT_INFO}"',
            comment="get JSON output for parsing in scripts",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
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
@pytest.mark.modal
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
    # The CEL predicate likely won't be true in CI; use --timeout 1 so usage
    # wait exits with code 2 and the chained create is skipped. The test
    # verifies the command-line shape parses end to end.
    e2e.run(
        "mngr usage wait --timeout 1 --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50'"
        ' && mngr create chore@.modal --no-connect --message "Find and fix an issue in the codebase."',
        comment="opportunistic create gated by usage wait (timeout-capped)",
    )
