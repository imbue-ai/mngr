"""Tests for the OUTPUT FORMATS AND MACHINE-READABLE OUTPUT tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
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
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_list_custom_human_format(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use custom format templates to customize human-readable output for yourself
        mngr list --format '{name} ({state})'
    """)
    # Create a real agent so the custom format template has data to render.
    expect(
        e2e.run(
            "mngr create format-demo --type command --no-ensure-clean --no-connect -- sleep 100931",
            comment="create an agent to format",
        )
    ).to_succeed()

    # The template expands {name} and {state} into "<name> (<STATE>)" per agent.
    result = e2e.run(
        "mngr list --format '{name} ({state})'",
        comment="use custom format templates to customize human-readable output for yourself",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_match(r"format-demo \([A-Z_]+\)")

    # The listed item *is* the agent, so fields are referenced bare ({name},
    # {state}) -- there is no "agent." namespace. Unknown template fields expand
    # to empty strings, which is why the tutorial uses {name}/{state} rather than
    # {agent.name}/{agent.state}.
    bogus = e2e.run(
        "mngr list --format '{agent.name} ({agent.state})'",
        comment="unknown fields (e.g. a non-existent 'agent.' namespace) render empty",
    )
    expect(bogus).to_succeed()
    expect(bogus.stdout).not_to_contain("format-demo")
    expect(bogus.stdout).to_contain("()")


@pytest.mark.release
def test_list_format_json_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON output (full array, good for programmatic use)
        mngr list --format json
    """)
    result = e2e.run("mngr list --format json", comment="JSON output (full array)")
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
# The stream runs a full provider scan before emitting its first snapshot and the
# pipeline then lingers until mngr notices the closed pipe, so this needs longer
# than the default 10s per-test cap.
@pytest.mark.timeout(60)
def test_observe_discovery_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stream discovery events as JSONL (hosts and agents discovered/destroyed)
        mngr observe --discovery-only
    """)
    # Pipe the stream into `head -n 1` so we capture the first emitted line and then
    # tear the pipeline down (mngr exits via SIGPIPE on its next write). The first
    # line on a fresh run is the full discovery snapshot. The outer `timeout` is
    # only a safety cap so the test can never hang waiting on the stream.
    result = e2e.run(
        "timeout 30 sh -c 'mngr observe --discovery-only | head -n 1' || true",
        comment="stream discovery events as JSONL",
        timeout=45.0,
    )
    expect(result).to_succeed()
    # The first JSONL object is a full discovery snapshot (DiscoveryEventType.DISCOVERY_FULL),
    # confirming the stream emits machine-readable discovery events rather than only exiting 0.
    expect(result.stdout).to_contain("DISCOVERY_FULL")


@pytest.mark.release
@pytest.mark.modal
def test_jsonl_works_across_commands(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON and JSONL works with most commands
        mngr list --format json && mngr plugin list --format jsonl
    """)
    # A single invocation keeps the test within its time budget (each
    # `mngr list` runs remote discovery). Both halves write to stdout: the
    # JSON document first, then the JSONL plugin lines (warnings go to stderr).
    result = e2e.run(
        "mngr list --format json && mngr plugin list --format jsonl",
        comment="JSON and JSONL works with most commands",
    )
    expect(result).to_succeed()

    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert output_lines, "expected combined json+jsonl output on stdout"

    # The JSON half (`mngr list --format json`) emits a single parseable object.
    parsed_json = json.loads(output_lines[0])
    assert isinstance(parsed_json, dict), f"expected a JSON object, got {type(parsed_json).__name__}"
    assert "agents" in parsed_json, f"expected an 'agents' key, got keys {list(parsed_json)}"

    # The JSONL half (`mngr plugin list --format jsonl`) emits one object per
    # line on the remaining lines.
    plugin_lines = output_lines[1:]
    assert plugin_lines, "expected at least one JSONL line of plugin output"
    for line in plugin_lines:
        plugin_obj = json.loads(line)
        assert isinstance(plugin_obj, dict), f"expected each JSONL line to be an object, got {line!r}"
        assert "name" in plugin_obj, f"expected a 'name' field in plugin object, got {plugin_obj}"


@pytest.mark.release
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


@pytest.mark.release
@pytest.mark.modal
def test_jsonl_with_jq_stream(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine jsonl with jq for streaming filtering
        mngr list --format jsonl | jq --unbuffered 'select(.state == "RUNNING") | .name'
    """)
    expect(
        e2e.run(
            "mngr list --format jsonl | jq --unbuffered 'select(.state == \"RUNNING\") | .name'",
            comment="combine jsonl with jq for streaming",
        )
    ).to_succeed()
