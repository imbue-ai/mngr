"""Tests for the SCRIPTING AND AUTOMATION tutorial section."""

import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_headless_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run in headless mode (no interactive prompts)
        mngr create my-task --headless --no-connect --message "Do the thing"
    """)
    expect(
        e2e.run(
            'mngr create my-task --headless --no-connect --type command --no-ensure-clean --message "Do the thing" -- sleep 101000',
            comment="run in headless mode",
            timeout=120.0,
        )
    ).to_succeed()

    # Verify the agent was actually created and is discoverable, not just that
    # the headless create command exited 0.
    list_result = e2e.run("mngr list", comment="verify the headless agent appears in the list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
def test_config_set_headless_globally(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or set headless globally
        mngr config set headless true
    """)
    # The default scope is project, so "globally" here means project-wide (it
    # applies to every invocation in this repo), as opposed to the per-command
    # --headless flag. Locate the project config file before writing it -- the
    # file does not exist yet, so loading it isn't blocked by the test harness's
    # is_allowed_in_pytest opt-in guard (which would reject the freshly-written
    # file on any later `mngr` invocation in this repo).
    path_result = e2e.run("mngr config path --scope project", comment="locate the project config file")
    expect(path_result).to_succeed()
    project_config_path = Path(path_result.stdout.strip())

    set_result = e2e.run("mngr config set headless true", comment="set headless globally")
    expect(set_result).to_succeed()
    # The set command reports the scope and file it wrote to.
    expect(set_result.stdout).to_contain("Set headless = true in project")

    # Verify the value actually persisted to the project config file, as a true
    # boolean (not the string "true").
    persisted = tomllib.loads(project_config_path.read_text())
    assert persisted.get("headless") is True, f"Expected headless = true in {project_config_path}, got {persisted!r}"


@pytest.mark.release
def test_config_set_rejects_unknown_key(e2e: E2eSession) -> None:
    """Unhappy path for the same `mngr config set` command: unknown keys are rejected.

    `mngr config set` validates the resulting config before writing the file, so
    an unknown key fails with a non-zero exit and the config file is left
    unchanged. (Note that the value's *type* is not validated -- e.g.
    `set headless notabool` is accepted -- because validation goes through
    `model_construct`, which only rejects unknown fields.)
    """
    e2e.write_tutorial_block("""
        # or set headless globally
        mngr config set headless true
    """)
    # The e2e fixture pre-seeds the project config file with the pytest opt-in
    # key, so the file already exists. Capture its contents before the rejected
    # write so we can prove the write left the file untouched.
    path_result = e2e.run("mngr config path --scope project", comment="locate the project config file")
    expect(path_result).to_succeed()
    project_config_path = Path(path_result.stdout.strip())
    contents_before = project_config_path.read_text() if project_config_path.exists() else None

    bad_result = e2e.run("mngr config set definitely_not_a_real_setting true", comment="reject an unknown config key")
    expect(bad_result).to_fail()
    expect(bad_result.stderr).to_contain("Unknown configuration fields")

    # The rejected write must not have modified the config file: its contents are
    # byte-for-byte identical, and the rejected key never made it to disk.
    contents_after = project_config_path.read_text() if project_config_path.exists() else None
    assert contents_after == contents_before, (
        f"Rejected write must not modify {project_config_path}: {contents_before!r} -> {contents_after!r}"
    )
    assert contents_after is None or "definitely_not_a_real_setting" not in contents_after


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
            "mngr create worker --reuse --provider modal --no-connect --no-ensure-clean --type command -- sleep 101200"
            ' && mngr message worker -m "Process the queue"',
            comment="idempotent create + message",
            timeout=180.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(180)
def test_get_json_into_var(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # get JSON output for parsing in scripts
        AGENT_INFO=$(mngr list --format json)
    """)
    # `mngr list` queries enabled remote providers (Modal), so it can take well
    # over the 10s global pytest timeout; override that marker and give the
    # subprocess matching headroom.
    #
    # No @pytest.mark.modal: `mngr list` discovers Modal via the Python SDK
    # in-process *inside the mngr subprocess*, but the modal resource guard's
    # SDK monkeypatch is only installed in the pytest process. The subprocess
    # only ever touches the guard tracking file when it shells out to the
    # `modal` CLI, which `mngr list` never does (it gracefully skips Modal when
    # the environment does not exist). The command also does not require Modal
    # to be reachable -- it returns an empty list either way -- so marking it
    # @pytest.mark.modal would only trigger a spurious "never invoked modal"
    # guard failure.
    result = e2e.run(
        'AGENT_INFO=$(mngr list --format json) && echo "${#AGENT_INFO}"',
        comment="get JSON output for parsing in scripts",
        timeout=120.0,
    )
    expect(result).to_succeed()
    # Verify the variable actually captured the JSON document rather than an
    # empty string: a well-formed `mngr list --format json` payload is an object
    # with `agents` and `errors` arrays, so the echoed character count is
    # comfortably non-trivial.
    captured_length = int(result.stdout.strip())
    assert captured_length > 0, f"expected AGENT_INFO to capture JSON, got length {captured_length}"


# NOTE: deliberately NOT marked @pytest.mark.modal. `mngr observe --discovery-only`
# reaches modal only through the in-process gRPC SDK, whose resource guard is a
# pytest-process monkeypatch that does not cross into the `mngr` subprocess. The
# only subprocess-visible modal guard is the `modal` CLI PATH wrapper, and a
# discovery/list command never shells out to that CLI (only create/deploy do).
# Marking this @pytest.mark.modal would therefore always fail the "marked but
# never invoked modal" guard check.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_observe_discovery_pipe_python(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use discovery stream for streaming results into other tools
        mngr observe --discovery-only | while read -r line; do
          echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('name', 'unknown'))"
        done
    """)
    # Warm the discovery cache first. `mngr list` runs an unfiltered listing,
    # which writes a full discovery snapshot to disk; that lets the `observe`
    # below emit the cached snapshot instantly on its fast path instead of
    # racing the (provider-querying) initial sync against the timeout. The raw
    # snapshot is real discovery JSONL: it carries the DISCOVERY_FULL event type.
    expect(e2e.run("mngr list", comment="warm the discovery cache")).to_succeed()
    raw = e2e.run(
        "timeout 5 mngr observe --discovery-only || true",
        comment="capture the raw discovery stream",
        timeout=45.0,
    )
    expect(raw.stdout).to_contain("DISCOVERY_FULL")
    # observe blocks indefinitely; wrap with timeout so the while-loop exits.
    result = e2e.run(
        'timeout 5 bash -c \'mngr observe --discovery-only | while read -r line; do echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get(\\"name\\", \\"unknown\\"))"; done\' || true',
        comment="pipe discovery stream into python (timeout-capped)",
        timeout=45.0,
    )
    expect(result).to_succeed()
    # Each discovery event is valid JSON the one-liner parses with json.load;
    # none carries a top-level "name", so the "unknown" fallback is printed for
    # every event. Seeing it confirms the snapshot flowed end to end through the
    # pipe (observe -> JSONL -> python), not merely that the timeout exited 0.
    expect(result.stdout).to_contain("unknown")


@pytest.mark.release
@pytest.mark.timeout(180)
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
    # In a fresh isolated env there are no usage sources, so the predicate can
    # never match; use --timeout 1 so usage wait gives up after its first poll
    # and exits 2 (EXIT_CODE_TIMEOUT). The chained `mngr create` must then be
    # short-circuited by the `&&`, so the whole command's exit code is 2.
    result = e2e.run(
        "mngr usage wait --timeout 1 --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50'"
        ' && mngr create chore@.modal --no-connect --message "Find and fix an issue in the codebase."',
        comment="opportunistic create gated by usage wait (timeout-capped)",
        timeout=120.0,
    )
    # Exit code 2 is produced only by usage wait's timeout path; a successful
    # predicate followed by a successful create would exit 0, and a create
    # failure would surface a different non-zero code. So exit 2 confirms both
    # that usage wait timed out and that the create was skipped.
    expect(result).to_have_exit_code(2)
    expect(result.stdout).to_contain("Timed out")

    # Verify the concrete effect: the gated create was skipped, so no `chore`
    # agent exists.
    listing = e2e.run("mngr list --format json", comment="confirm the gated create was skipped", timeout=120.0)
    expect(listing).to_succeed()
    expect(listing.stdout).not_to_contain("chore")
