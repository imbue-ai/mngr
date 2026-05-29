"""Tests for the OUTPUT FORMATS AND MACHINE-READABLE OUTPUT tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# Note: @pytest.mark.modal is NOT used on this discovery-only test
# (mngr ls with no agents). The modal resource guard in
# libs/mngr/conftest.py is a PATH wrapper for the `modal` CLI binary. These
# e2e tests run mngr as a subprocess; read-only discovery skips the modal
# provider entirely when no environment exists (ProviderEmptyError in
# ModalProviderBackend.build_provider_instance) and otherwise reaches Modal
# only via the in-process Python SDK, never the `modal` CLI. The PATH wrapper
# therefore never fires, so adding @pytest.mark.modal would cause "Test marked
# with @pytest.mark.modal but never invoked modal" failures. (Create-modal
# tests differ: they invoke `modal environment create` during host creation,
# which the wrapper does intercept -- see test_create_modal.py.)
@pytest.mark.release
def test_default_output_human_readable(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # default output is human-readable
        mngr ls
    """)
    result = e2e.run("mngr ls", comment="default output is human-readable")
    expect(result).to_succeed()
    # The fresh environment has no agents, so the default (human-readable)
    # renderer prints a friendly message rather than an empty machine-readable
    # collection. Asserting on this confirms we got the human format.
    expect(result.stdout).to_contain("No agents found")
    # And confirm it is NOT a machine-readable format: a JSON array would begin
    # with "[" and JSONL with "{". The human default does neither.
    expect(result.stdout).not_to_match(r"^\s*[\[{]")


@pytest.mark.release
def test_list_custom_human_format(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use custom format templates to customize human-readable output for yourself
        mngr list --format '{agent.name} ({agent.state})'
    """)
    expect(
        e2e.run(
            "mngr list --format '{agent.name} ({agent.state})'",
            comment="custom format template",
        )
    ).to_succeed()


# NOTE: no @pytest.mark.modal here. With the remote providers the e2e fixture
# leaves enabled, `mngr list` performs its Modal discovery via the in-process
# Modal SDK *inside the mngr subprocess*, which neither resource-guard mechanism
# (the PATH binary wrapper nor the in-process SDK monkeypatch) can observe -- so
# the modal mark would always trip the guard's "marked but never invoked" check.
# The command also succeeds (empty list) without Modal credentials, so it does
# not actually require the mark. The generous timeout covers the mngr cold-start
# plus the slow Modal SDK discovery that happens when credentials are present.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_format_json_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON output (full array, good for programmatic use)
        mngr list --format json
    """)
    result = e2e.run("mngr list --format json", comment="JSON output (full array)")
    expect(result).to_succeed()
    # The whole point of --format json is machine-readable output: stdout must be
    # valid JSON exposing the agent listing (provider warnings go to stderr, so
    # they do not contaminate the parseable payload). In this isolated fixture no
    # agents have been created, so the listing is present but empty.
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict), f"expected a JSON object, got {type(parsed).__name__}: {parsed!r}"
    assert isinstance(parsed["agents"], list), f"expected 'agents' to be a list, got {parsed['agents']!r}"
    assert parsed["agents"] == [], f"expected no agents in the isolated env, got {parsed['agents']!r}"


@pytest.mark.release
def test_list_format_jsonl_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSONL output (one object per line, good for streaming/piping)
        mngr list --format jsonl
    """)
    result = e2e.run("mngr list --format jsonl", comment="JSONL output")
    expect(result).to_succeed()
    # Verify the JSONL contract: each line of stdout is an independent JSON
    # object, never a wrapping array (that is what distinguishes jsonl from the
    # json format). A fresh environment has no agents, so stdout is empty here;
    # the loop still guards against a regression that emits a JSON array or any
    # non-object payload, since `[...]` would parse as a list and fail the check.
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert isinstance(parsed, dict), f"Expected each JSONL line to be a JSON object, got: {line!r}"


# NOTE: no @pytest.mark.modal here. `mngr observe --discovery-only` discovers
# Modal via the in-process SDK (`sandbox_list`), which the resource guard cannot
# track across the `mngr` subprocess boundary -- only the `modal` CLI binary is
# tracked, and that is invoked solely during host *creation* (modal environment
# create / modal deploy), not during read-only discovery. Marking this test
# @pytest.mark.modal would therefore trip the guard's "never invoked modal"
# check. Discovery degrades gracefully when Modal is unavailable, so the mark is
# not needed for correctness either.
@pytest.mark.release
@pytest.mark.timeout(60)
def test_observe_discovery_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stream discovery events as JSONL (hosts and agents discovered/destroyed)
        mngr observe --discovery-only
    """)
    # `mngr observe --discovery-only` streams forever, so bound it with `timeout`.
    # The window must be long enough for the initial full-discovery snapshot to
    # be flushed to stdout (a fresh environment has no cached snapshot, so the
    # synchronous first poll across all providers must complete first).
    result = e2e.run(
        "timeout 15 mngr observe --discovery-only || true",
        comment="stream discovery events as JSONL",
        timeout=30.0,
    )
    expect(result).to_succeed()
    # Verify the actual behavior: observe must emit a parseable JSONL discovery
    # stream, not merely exit cleanly. Every non-blank line is a JSON object, and
    # a full discovery snapshot (DISCOVERY_FULL) must appear so consumers can
    # reconstruct authoritative state.
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert events, f"expected JSONL discovery events on stdout, got: {result.stdout!r}"
    assert any(event.get("type") == "DISCOVERY_FULL" for event in events), (
        f"expected a DISCOVERY_FULL event in observe output, got types: {[event.get('type') for event in events]}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_jsonl_works_across_commands(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON and JSONL works with most commands
        mngr config list --format json && mngr plugin list --format jsonl
    """)
    # Run the combined command exactly as the tutorial shows it. mngr CLI
    # startup is slow (~5s), so we parse this single invocation's output rather
    # than re-running each command and blowing the per-test time budget.
    result = e2e.run(
        "mngr config list --format json && mngr plugin list --format jsonl",
        comment="JSON and JSONL works with most commands",
    )
    expect(result).to_succeed()

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    # The first command's --format json output is a single-line JSON document.
    config_payload = json.loads(lines[0])
    assert isinstance(config_payload, dict)
    assert "config" in config_payload
    # The second command's --format jsonl output is one JSON object per line,
    # each a self-contained record. There is more than one plugin.
    plugin_lines = lines[1:]
    assert len(plugin_lines) > 1
    for line in plugin_lines:
        record = json.loads(line)
        assert isinstance(record, dict)
        assert "name" in record


@pytest.mark.release
@pytest.mark.timeout(60)
def test_json_and_jsonl_are_structurally_different(e2e: E2eSession) -> None:
    """The same command renders --format json as a single array-bearing document
    and --format jsonl as one object per line, covering the same tutorial block
    from the angle of how the two machine-readable formats actually differ."""
    e2e.write_tutorial_block("""
        # JSON and JSONL works with most commands
        mngr config list --format json && mngr plugin list --format jsonl
    """)
    # --format json: a single JSON document whose "plugins" key holds the array.
    json_result = e2e.run("mngr plugin list --format json", comment="json: single document with a plugins array")
    expect(json_result).to_succeed()
    json_payload = json.loads(json_result.stdout)
    assert isinstance(json_payload, dict)
    plugins = json_payload["plugins"]
    assert isinstance(plugins, list)
    assert len(plugins) > 1

    # --format jsonl: the same records, but one JSON object per line. The whole
    # blob is therefore NOT a single JSON document.
    jsonl_result = e2e.run("mngr plugin list --format jsonl", comment="jsonl: one object per line")
    expect(jsonl_result).to_succeed()
    jsonl_records = [json.loads(line) for line in jsonl_result.stdout.splitlines() if line.strip()]
    assert len(jsonl_records) == len(plugins)


@pytest.mark.release
@pytest.mark.timeout(60)
def test_json_with_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine json with jq for powerful filtering and transformation
        mngr list --format json | jq '.agents[] | select(.state == "RUNNING") | .name'
    """)
    result = e2e.run(
        "mngr list --format json | jq '.agents[] | select(.state == \"RUNNING\") | .name'",
        comment="combine json with jq",
    )
    expect(result).to_succeed()
    # A fresh environment has no RUNNING agents, so the jq filter yields nothing.
    # This confirms jq parsed the {"agents": [...], "errors": [...]} object and
    # filtered cleanly: the bare `.[]` form would instead error on that object
    # (jq exits non-zero), so an empty, successful result is the meaningful signal.
    expect(result.stdout).to_be_empty()


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
