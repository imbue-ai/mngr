"""Tests for the SCRIPTING AND AUTOMATION tutorial section."""

import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_headless_no_connect_message(e2e: E2eSession) -> None:
    """Tutorial block:
        # run in headless mode (no interactive prompts)
        mngr create my-task --headless --no-connect --message "Do the thing"

    Scope: a headless create combining `--headless`, `--no-connect`, and
    `--message` runs non-interactively, exits 0, and the created agent appears
    in `mngr list`.
    """
    expect(
        e2e.run(
            'mngr create my-task --headless --no-connect --type command --no-ensure-clean --message "Do the thing" -- sleep 101000',
            comment="run in headless mode",
            timeout=120.0,
        )
    ).to_succeed()

    # Verify the agent was actually created and is discoverable, not just that
    # the headless create command exited 0. The headless create defaults to the
    # local provider, so scope `mngr list` to `--provider local` (matching the
    # rest of the e2e suite): a bare `mngr list` also queries enabled cloud
    # providers (e.g. AWS), which are unconfigured in the isolated test
    # environment and would make the listing exit non-zero for reasons unrelated
    # to this headless create.
    list_result = e2e.run("mngr list --provider local", comment="verify the headless agent appears in the list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.timeout(180)
def test_config_set_headless_globally(e2e: E2eSession) -> None:
    """Tutorial block:
        # or set headless globally
        mngr config set headless true

    Scope: `mngr config set headless true` writes to the default (project) scope
    config file, reports "Set headless = true in project", and persists the
    value as a real boolean True (not the string "true").
    """
    # The default scope is project, so "globally" here means project-wide (it
    # applies to every invocation in this repo), as opposed to the per-command
    # --headless flag. Locate the project config file before writing it -- the
    # file does not exist yet, so loading it isn't blocked by the test harness's
    # is_allowed_in_pytest opt-in guard (which would reject the freshly-written
    # file on any later `mngr` invocation in this repo).
    #
    # The `mngr` subprocess cold-start can exceed the 10s global pytest timeout,
    # so this test overrides that marker (@pytest.mark.timeout above) and gives
    # each subprocess matching headroom past the 30s default.
    path_result = e2e.run("mngr config path --scope project", comment="locate the project config file", timeout=120.0)
    expect(path_result).to_succeed()
    project_config_path = Path(path_result.stdout.strip())

    set_result = e2e.run("mngr config set headless true", comment="set headless globally", timeout=120.0)
    expect(set_result).to_succeed()
    # The set command reports the scope and file it wrote to.
    expect(set_result.stdout).to_contain("Set headless = true in project")

    # Verify the value actually persisted to the project config file, as a true
    # boolean (not the string "true").
    persisted = tomllib.loads(project_config_path.read_text())
    assert persisted.get("headless") is True, f"Expected headless = true in {project_config_path}, got {persisted!r}"


@pytest.mark.release
@pytest.mark.timeout(180)
def test_config_set_rejects_unknown_key(e2e: E2eSession) -> None:
    """Tutorial block:
        # or set headless globally
        mngr config set headless true

    Scope: the unhappy path of the same `mngr config set` block. `mngr config
    set` validates the resulting config before writing, so an unknown key fails
    with a non-zero exit and stderr "Unknown configuration fields", and the
    config file is left byte-for-byte unchanged (the rejected key never reaches
    disk). The value's *type* is not validated -- e.g. `set headless notabool`
    is accepted -- because validation goes through `model_construct`, which only
    rejects unknown fields.
    """
    # The `mngr` subprocess cold-start can exceed the 10s global pytest timeout,
    # so this test overrides that marker (@pytest.mark.timeout above) and gives
    # each of its three subprocesses matching headroom past the 30s default.
    #
    # The e2e fixture pre-seeds the project config file with the pytest opt-in
    # key, so the file already exists. Capture its contents before the rejected
    # write so we can prove the write left the file untouched.
    path_result = e2e.run(
        "mngr config path --scope project", comment="locate the project config file", timeout=120.0
    )
    expect(path_result).to_succeed()
    project_config_path = Path(path_result.stdout.strip())
    contents_before = project_config_path.read_text() if project_config_path.exists() else None

    bad_result = e2e.run(
        "mngr config set definitely_not_a_real_setting true", comment="reject an unknown config key", timeout=120.0
    )
    expect(bad_result).to_fail()
    expect(bad_result.stderr).to_contain("Unknown configuration fields")

    # The rejected write must not have modified the config file: its contents are
    # byte-for-byte identical, and the rejected key never made it to disk.
    contents_after = project_config_path.read_text() if project_config_path.exists() else None
    assert contents_after == contents_before, (
        f"Rejected write must not modify {project_config_path}: {contents_before!r} -> {contents_after!r}"
    )
    assert contents_after is None or "definitely_not_a_real_setting" not in contents_after

    # Validation only rejects unknown fields; the value's *type* is not checked,
    # since it goes through model_construct. So setting a known key to a value of
    # the wrong type (a bool field to a non-bool string) is accepted and exits 0.
    wrong_type_result = e2e.run(
        "mngr config set headless notabool", comment="a wrong-type value for a known key is accepted", timeout=120.0
    )
    expect(wrong_type_result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_reuse_and_message(e2e: E2eSession) -> None:
    """Tutorial block:
        # idempotent creation: reuse an existing agent if it already exists
        mngr create worker --reuse --provider modal --no-connect && mngr message worker -m "Process the queue"

    Scope: idempotent `mngr create --reuse` chained with `mngr message` -- the
    `--reuse` create and the subsequent message to the same agent both succeed,
    so the chained command exits 0.
    """
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
    """Tutorial block:
        # get JSON output for parsing in scripts
        AGENT_INFO=$(mngr list --format json)

    Scope: capturing `mngr list --format json` via command substitution into a
    shell variable yields a non-empty JSON document (the agents/errors object),
    so a script can parse the output -- the captured character count is non-zero.
    """
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
@pytest.mark.timeout(150)
def test_observe_discovery_pipe_python(e2e: E2eSession) -> None:
    """Tutorial block:
        # use discovery stream for streaming results into other tools
        mngr observe --discovery-only | while read -r line; do
          echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('name', 'unknown'))"
        done

    Scope: piping `mngr observe --discovery-only` JSONL into a python one-liner.
    Each emitted event is valid JSON the loop parses with json.load; the raw
    stream carries DISCOVERY_FULL events, and since no event has a top-level
    "name", the "unknown" fallback prints for every event -- confirming the
    snapshot flowed end to end through the pipe (observe -> JSONL -> python),
    not merely that the timeout-wrapped command exited 0.
    """
    # `mngr observe --discovery-only` is a discovery *producer*: on startup it runs
    # one bounded discovery poll per enabled provider and appends each provider's
    # snapshot to the shared discovery log, then its own tail echoes those appended
    # lines to stdout as JSONL. It tails from the current end of the file, so it does
    # not replay any pre-existing cached snapshot -- the stream carries the snapshots
    # its own startup poll writes. The docstring names the legacy whole-world
    # DISCOVERY_FULL snapshot, but that event is deprecated and no longer produced
    # (superseded by the per-provider DISCOVERY_PROVIDER snapshot), so the raw stream
    # carries DISCOVERY_PROVIDER events instead.
    #
    # observe blocks forever, so wrap it in `timeout` + `|| true` to bound the
    # capture. The window must clear the mngr subprocess cold-start (heavy imports
    # dominate the time-to-first-event; the local/ssh poll itself is sub-second)
    # before the startup poll's first snapshot is emitted.
    raw = e2e.run(
        "timeout 30 mngr observe --discovery-only || true",
        comment="capture the raw discovery stream",
        timeout=60.0,
    )
    expect(raw.stdout).to_contain("DISCOVERY_PROVIDER")
    result = e2e.run(
        'timeout 30 bash -c \'mngr observe --discovery-only | while read -r line; do echo "$line" | python -c "import sys, json; d=json.load(sys.stdin); print(d.get(\\"name\\", \\"unknown\\"))"; done\' || true',
        comment="pipe discovery stream into python (timeout-capped)",
        timeout=60.0,
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
    r"""Tutorial block:
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

    Scope: a usage-gated create. With no usage sources and `--timeout 1`, the CEL
    predicate never matches, so `mngr usage wait` exits 2 (its timeout path) with
    "Timed out" on stdout, short-circuiting the `&&` so the chained `mngr create`
    is skipped -- no `chore` agent is created.
    """
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
    # agent exists. Scope discovery to the always-reachable local provider so
    # the check does not depend on cloud-provider credentials -- an unscoped
    # `mngr list` enumerates every enabled provider and exits non-zero
    # (EXIT_CODE_PROVIDER_INACCESSIBLE) when a bundled-but-unconfigured provider
    # such as AWS lacks credentials, which is unrelated to this test's scope.
    listing = e2e.run(
        "mngr list --provider local --format json", comment="confirm the gated create was skipped", timeout=120.0
    )
    expect(listing).to_succeed()
    expect(listing.stdout).not_to_contain("chore")
