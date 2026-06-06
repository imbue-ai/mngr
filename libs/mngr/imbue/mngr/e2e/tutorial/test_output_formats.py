"""Tests for the OUTPUT FORMATS AND MACHINE-READABLE OUTPUT tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.modal
def test_default_output_human_readable(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # default output is human-readable
        mngr ls
    """)
    expect(e2e.run("mngr ls", comment="default output is human-readable")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
def test_list_format_json_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON output (full array, good for programmatic use)
        mngr list --format json
    """)
    expect(e2e.run("mngr list --format json", comment="JSON output (full array)")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_format_jsonl_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSONL output (one object per line, good for streaming/piping)
        mngr list --format jsonl
    """)
    expect(e2e.run("mngr list --format jsonl", comment="JSONL output")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_observe_discovery_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stream discovery events as JSONL (hosts and agents discovered/destroyed)
        mngr observe --discovery-only
    """)
    expect(
        e2e.run(
            "timeout 1 mngr observe --discovery-only || true",
            comment="stream discovery events as JSONL",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_jsonl_works_across_commands(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # JSON and JSONL works with most commands
        mngr snapshot list --format json && mngr plugin list --format jsonl
    """)
    expect(
        e2e.run(
            "mngr snapshot list --format json && mngr plugin list --format jsonl",
            comment="JSON and JSONL works with most commands",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_json_with_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine json with jq for powerful filtering and transformation
        mngr list --format json | jq '.[] | select(.state == "RUNNING") | .name'
    """)
    expect(
        e2e.run(
            "mngr list --format json | jq '.[] | select(.state == \"RUNNING\") | .name'",
            comment="combine json with jq",
        )
    ).to_succeed()


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
