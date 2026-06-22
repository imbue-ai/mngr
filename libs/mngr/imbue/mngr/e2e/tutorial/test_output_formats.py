"""Tests for the OUTPUT FORMATS AND MACHINE-READABLE OUTPUT tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# NOTE: this test is intentionally NOT marked @pytest.mark.modal. `mngr ls` (an
# alias for `mngr list`) is a read-only path (is_environment_creation_allowed=False),
# so it never shells out to the `modal` CLI -- it reaches Modal only via in-process
# gRPC inside the `mngr` subprocess. The resource guard's Modal SDK monkeypatch lives
# only in the pytest process, so it cannot observe the subprocess's gRPC traffic, and
# the modal CLI binary guard (which is cross-process) is never tripped. Marking the
# test @pytest.mark.modal would therefore fail the guard's NEVER_INVOKED check
# deterministically. The command does not require Modal to succeed (Modal discovery
# failures are non-fatal warnings). A longer timeout is still needed because Modal
# discovery is slow when credentials are present and exceeds the 10s default.
@pytest.mark.release
@pytest.mark.timeout(180)
def test_default_output_human_readable(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # default output is human-readable
        mngr ls
    """)
    result = e2e.run("mngr ls", comment="default output is human-readable")
    expect(result).to_succeed()
    # The default format is human-readable, not machine-readable: an empty
    # listing renders the prose "No agents found" rather than the "[]" that
    # `--format json` would emit or the empty output of `--format jsonl`.
    expect(result.stdout).to_contain("No agents found")
    expect(result.stdout).not_to_contain("[]")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_custom_human_format(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use custom format templates to customize human-readable output for yourself
        mngr list --format '{name} ({state})'
    """)
    # Create a real agent so the custom format template has data to render. With
    # no --provider/host the agent lands on the always-available local provider.
    expect(
        e2e.run(
            "mngr create format-demo --type command --no-ensure-clean --no-connect -- sleep 100931",
            comment="create an agent to format",
        )
    ).to_succeed()

    # Scope discovery to the local provider (where format-demo lives). The bare
    # tutorial command fans out to every enabled backend, so its exit code
    # depends on whether *unrelated* backends happen to be reachable in the
    # environment: in CI the workflow exports cloud credentials and runs the
    # Docker daemon, so all providers respond and `mngr list` exits 0, but on a
    # host without Docker running or without cloud credentials those backends
    # raise ProviderUnavailableError and the command exits 1 (per
    # error_handling.md) even though the local agent still renders correctly.
    # This test exercises the *format template*, not multi-provider reachability
    # (note the absence of @pytest.mark.docker/@pytest.mark.modal), so `--provider
    # local` keeps it deterministic everywhere. It is an extra flag layered onto
    # the tutorial command, which is preserved verbatim in write_tutorial_block.
    #
    # The template expands {name} and {state} into "<name> (<STATE>)" per agent.
    result = e2e.run(
        "mngr list --provider local --format '{name} ({state})'",
        comment="use custom format templates to customize human-readable output for yourself",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_match(r"format-demo \([A-Z_]+\)")

    # The listed item *is* the agent, so fields are referenced bare ({name},
    # {state}) -- there is no "agent." namespace. Unknown template fields expand
    # to empty strings, which is why the tutorial uses {name}/{state} rather than
    # {agent.name}/{agent.state}.
    bogus = e2e.run(
        "mngr list --provider local --format '{agent.name} ({agent.state})'",
        comment="unknown fields (e.g. a non-existent 'agent.' namespace) render empty",
    )
    expect(bogus).to_succeed()
    expect(bogus.stdout).not_to_contain("format-demo")
    expect(bogus.stdout).to_contain("()")


# NOTE: the tutorial shows a bare `mngr list --format json`, but this test scopes the
# listing to `--provider local` (the same convention the other e2e tutorial tests use).
# The monorepo installs every provider plugin (aws, azure, gcp, vultr, ...), so a bare
# `mngr list` discovers a default instance for each. An unconfigured cloud provider
# (no credentials) raises ProviderUnavailableError during discovery, which the default
# `--on-error abort` turns into a non-zero exit -- an environment artifact unrelated to
# the JSON-format behavior under test. `--provider local` queries only the always-available
# local provider, keeping the test deterministic while still exercising the `--format json`
# document shape. The timeout is bumped above the 10s default because `mngr` startup
# (loading every installed provider plugin, building config) takes ~15s in this
# environment even though the local-only listing itself is instant.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_format_json_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON output (full array, good for programmatic use)
        mngr list --format json
    """)
    result = e2e.run("mngr list --provider local --format json", comment="JSON output (full array)")
    expect(result).to_succeed()
    # `--format json` emits a single top-level object (one parseable document),
    # in contrast to `--format jsonl` which emits one object per line. Verify
    # the document parses and exposes the documented "agents"/"errors" arrays.
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict), f"expected a single JSON object, got {type(parsed).__name__}: {result.stdout!r}"
    assert isinstance(parsed["agents"], list), f"expected an 'agents' array, got {parsed!r}"
    assert isinstance(parsed["errors"], list), f"expected an 'errors' array, got {parsed!r}"


# NOTE: this test is intentionally NOT marked @pytest.mark.modal. `mngr list` is a
# read-only path (is_environment_creation_allowed=False), so it never shells out to
# the `modal` CLI -- it reaches Modal only via in-process gRPC inside the `mngr`
# subprocess. The resource guard's Modal SDK monkeypatch lives only in the pytest
# process (SDK guards are in-process), so it cannot observe the subprocess's gRPC
# traffic, and the modal CLI binary guard (which is cross-process) is never tripped.
# Marking the test @pytest.mark.modal would therefore fail the guard's NEVER_INVOKED
# check deterministically. The command does not require Modal to succeed (Modal
# discovery failures are non-fatal warnings). A longer timeout is still needed because
# Modal discovery is slow when credentials are present and exceeds the 10s default.
@pytest.mark.release
@pytest.mark.timeout(180)
def test_list_format_jsonl_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSONL output (one object per line, good for streaming/piping)
        mngr list --format jsonl
    """)
    result = e2e.run("mngr list --format jsonl", comment="JSONL output")
    expect(result).to_succeed()
    # JSONL means one JSON object per line: every non-empty stdout line must parse
    # as a JSON object -- not a JSON array (as `--format json` produces) and not
    # human-readable text. With no agents present the stream is empty, which is a
    # valid (zero-object) JSONL document, so this also guards against a regression
    # that emitted a `[]` array or a table header under the jsonl format.
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"Expected each JSONL line to be a JSON object, got: {line!r}"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_format_jsonl_with_agent(e2e: E2eSession) -> None:
    # Same tutorial block as test_list_format_jsonl_recap, but the data-bearing
    # happy path: the empty-stream test cannot prove that a real agent is
    # rendered as one self-contained JSON object per line, so create an agent
    # first and assert the stream actually carries it.
    e2e.write_tutorial_block("""
        # JSONL output (one object per line, good for streaming/piping)
        mngr list --format jsonl
    """)
    # Create a real (local command) agent so the JSONL stream has a row to emit.
    expect(
        e2e.run(
            "mngr create jsonl-recap-demo --type command --no-ensure-clean --no-connect -- sleep 100417",
            comment="create an agent so the JSONL stream is non-empty",
        )
    ).to_succeed()

    result = e2e.run("mngr list --format jsonl", comment="JSONL output")
    expect(result).to_succeed()

    # Each non-empty line must be an independent JSON object (the JSONL contract).
    # Crucially, stdout must NOT be a single JSON array -- that is the `--format
    # json` shape -- so json.loads over the whole stdout would fail / not be a
    # dict. Parsing per line proves the one-object-per-line framing.
    agents = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"Expected each JSONL line to be a JSON object, got: {line!r}"
        agents.append(parsed)

    # The created agent must appear as its own object, carrying the per-agent
    # fields (name/id) rather than the `{"agents": [...], "errors": [...]}`
    # envelope that `--format json` wraps results in.
    matching = [agent for agent in agents if agent.get("name") == "jsonl-recap-demo"]
    assert matching, f"expected a JSONL object for the created agent, got: {agents!r}"
    assert matching[0].get("id"), f"expected the agent object to carry an 'id', got: {matching[0]!r}"
    assert "agents" not in matching[0], f"JSONL row should be the agent itself, not the json envelope: {matching[0]!r}"


@pytest.mark.release
# The stream runs a full provider scan before emitting its first snapshot and the
# pipeline then lingers until mngr notices the closed pipe, so this needs longer
# than the default 10s per-test cap.
@pytest.mark.timeout(60)
def test_observe_discovery_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stream discovery events as JSONL (hosts and agents discovered/destroyed)
        mngr observe --discovery-only
    """)
    # Pipe the stream into `grep -m 1 DISCOVERY_FULL` so we capture the full
    # discovery snapshot line and then tear the pipeline down (grep exits on the
    # first match, and mngr exits via SIGPIPE on its next write). We grep for the
    # snapshot rather than taking the first line with `head -n 1`: a fresh run
    # emits any provider-discovery failures as DISCOVERY_ERROR lines first (e.g.
    # an unconfigured cloud provider), so the snapshot is not necessarily the very
    # first line. The outer `timeout` is only a safety cap so the test can never
    # hang waiting on the stream.
    result = e2e.run(
        "timeout 30 sh -c 'mngr observe --discovery-only | grep -m 1 DISCOVERY_FULL' || true",
        comment="stream discovery events as JSONL",
        timeout=45.0,
    )
    expect(result).to_succeed()
    # The captured line is a full discovery snapshot (DiscoveryEventType.DISCOVERY_FULL),
    # confirming the stream emits machine-readable discovery events rather than only
    # exiting 0. Parse it and assert on the documented snapshot shape (a JSON object
    # carrying the "agents"/"hosts" arrays) so the check is not merely a substring match.
    snapshot_line = result.stdout.strip().splitlines()[-1]
    snapshot = json.loads(snapshot_line)
    assert snapshot["type"] == "DISCOVERY_FULL", f"expected a DISCOVERY_FULL event, got {snapshot!r}"
    assert isinstance(snapshot["agents"], list), f"expected an 'agents' array, got {snapshot!r}"
    assert isinstance(snapshot["hosts"], list), f"expected a 'hosts' array, got {snapshot!r}"


@pytest.mark.release
# Intentionally NOT marked @pytest.mark.modal (mirroring
# ``test_list_format_jsonl_recap``): ``mngr list`` is a read-only path that
# reaches Modal only via in-process gRPC inside the ``mngr`` subprocess, which
# the resource guard's in-process Modal SDK monkeypatch cannot observe and which
# never shells out to the ``modal`` CLI binary (the only cross-process-tracked
# path). Marking the test @pytest.mark.modal would therefore fail the guard's
# NEVER_INVOKED check. The e2e fixture pins ``enabled_backends`` from the test's
# markers, so an unmarked test discovers only the local provider -- the command
# stays a fast, deterministic JSON+JSONL output check independent of ambient
# cloud credentials or a running Docker daemon. The chained
# ``mngr plugin list`` invocation still warrants slightly more than the 10s
# default cap because each ``mngr`` subprocess loads every installed plugin.
@pytest.mark.timeout(60)
def test_jsonl_works_across_commands(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON and JSONL works with most commands
        mngr list --format json && mngr plugin list --format jsonl
    """)
    # Both halves write to stdout: the JSON document first, then the JSONL plugin
    # lines (warnings go to stderr).
    result = e2e.run(
        "mngr list --format json && mngr plugin list --format jsonl",
        comment="JSON and JSONL works with most commands",
        timeout=45.0,
    )
    expect(result).to_succeed()

    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert output_lines, "expected combined json+jsonl output on stdout"

    # The JSON half (`mngr list --format json`) emits a single parseable object.
    parsed_json = json.loads(output_lines[0])
    assert isinstance(parsed_json, dict), f"expected a JSON object, got {type(parsed_json).__name__}"
    assert isinstance(parsed_json.get("agents"), list), f"expected an 'agents' array, got {parsed_json!r}"
    # Discovery ran cleanly: in this isolated environment the only enabled
    # provider is the local one (nothing to enumerate, nothing unreachable), so
    # the documented `errors` array is present and empty. A non-empty array here
    # would mean a provider failed discovery -- which `mngr list` would also have
    # exited non-zero for, failing the `to_succeed()` check above.
    assert parsed_json.get("errors") == [], f"expected no discovery errors, got {parsed_json.get('errors')!r}"

    # The JSONL half (`mngr plugin list --format jsonl`) emits one object per
    # line on the remaining lines.
    plugin_lines = output_lines[1:]
    assert plugin_lines, "expected at least one JSONL line of plugin output"
    for line in plugin_lines:
        plugin_obj = json.loads(line)
        assert isinstance(plugin_obj, dict), f"expected each JSONL line to be an object, got {line!r}"
        assert "name" in plugin_obj, f"expected a 'name' field in plugin object, got {plugin_obj}"


# NOTE: this test is intentionally NOT marked @pytest.mark.modal. `mngr list` is a
# read-only path (is_environment_creation_allowed=False), so it never shells out to
# the `modal` CLI -- it reaches Modal only via in-process gRPC inside the `mngr`
# subprocess, which the cross-process modal CLI guard never observes. Marking the
# test @pytest.mark.modal would therefore fail the guard's NEVER_INVOKED check. The
# command does not require Modal to succeed (Modal discovery failures are non-fatal
# warnings), but a longer timeout is needed because Modal discovery is slow when
# credentials are present and exceeds the 10s default.
@pytest.mark.release
@pytest.mark.timeout(180)
def test_json_with_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine json with jq for powerful filtering and transformation
        mngr list --format json | jq '.agents[] | select(.state == "RUNNING") | .name'
    """)
    jq_result = e2e.run(
        "mngr list --format json | jq '.agents[] | select(.state == \"RUNNING\") | .name'",
        comment="combine json with jq",
    )
    # The whole pipeline must succeed. This also pins the JSON shape the
    # tutorial depends on: `mngr list --format json` emits a single object with
    # an `agents` array, so the filter dereferences `.agents[]`. A wrong path
    # (e.g. iterating the top-level object directly) makes jq exit non-zero.
    expect(jq_result).to_succeed()
    # The isolated test environment has no agents, so selecting RUNNING agents
    # yields no names: the `select` filter evaluated against real JSON and
    # matched nothing (rather than erroring on an unexpected shape).
    expect(jq_result.stdout).to_be_empty()


# NOTE: like `test_list_format_jsonl_recap`, this test is intentionally NOT marked
# @pytest.mark.modal. The underlying `mngr list --format jsonl` is a read-only path
# (is_environment_creation_allowed=False), so it never shells out to the `modal` CLI --
# it reaches Modal only via in-process gRPC inside the `mngr` subprocess, which the
# cross-process CLI binary guard cannot observe. Marking the test @pytest.mark.modal
# would therefore fail the guard's NEVER_INVOKED check deterministically. A longer
# timeout is still needed because Modal discovery is slow when credentials are present
# and exceeds the 10s default; the jq pipeline also lingers until mngr finishes.
@pytest.mark.release
@pytest.mark.timeout(180)
def test_jsonl_with_jq_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine jsonl with jq for streaming filtering
        mngr list --format jsonl | jq --unbuffered 'select(.state == "RUNNING") | .name'
    """)
    jq_result = e2e.run(
        "mngr list --format jsonl | jq --unbuffered 'select(.state == \"RUNNING\") | .name'",
        comment="combine jsonl with jq for streaming",
    )
    # The whole pipeline must succeed. This pins the JSONL contract the tutorial
    # depends on: each emitted line is a standalone JSON object, so jq's bare
    # `select(.state == ...)` applies per object (no `.agents[]` deref as the
    # `--format json` filter needs). A non-object line (e.g. a `[]` array or a
    # table header) would make jq exit non-zero.
    expect(jq_result).to_succeed()
    # The isolated test environment has no RUNNING agents, so the `select` filter
    # matched nothing and emitted no names: it evaluated against real per-line
    # JSON rather than erroring on an unexpected shape.
    expect(jq_result.stdout).to_be_empty()
