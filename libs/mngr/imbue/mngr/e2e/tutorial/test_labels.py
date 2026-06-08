"""Tests for the LABELS AND FILTERING tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
def test_list_filter_by_label_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list agents filtered by label using CEL expressions
        mngr list --include 'labels.priority == "high"'
    """)
    expect(
        e2e.run(
            "mngr list --include 'labels.priority == \"high\"'",
            comment="filter by label using CEL",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_combine_include_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple filters (AND logic for --include, all must match)
        mngr list --include 'labels.team == "backend"' --include 'state == "RUNNING"'
    """)
    expect(
        e2e.run(
            "mngr list --include 'labels.team == \"backend\"' --include 'state == \"RUNNING\"'",
            comment="combine multiple --include filters (AND)",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_exclude_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # exclude agents matching a filter
        mngr list --exclude 'labels.team == "frontend"'
    """)
    expect(
        e2e.run(
            "mngr list --exclude 'labels.team == \"frontend\"'",
            comment="exclude agents matching a filter",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_combine_exclude_filters(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # combine multiple exclusion filters (OR logic for --exclude, any can match)
        mngr list --exclude 'labels.team == "frontend"' --exclude 'labels.team == "devops"'
    """)
    expect(
        e2e.run(
            "mngr list --exclude 'labels.team == \"frontend\"' --exclude 'labels.team == \"devops\"'",
            comment="combine multiple --exclude filters (OR)",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_compound_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just do combined filters directly in the CEL expression:
        mngr list --include 'labels.team == "backend" && state == "RUNNING"'
    """)
    expect(
        e2e.run(
            'mngr list --include \'labels.team == "backend" && state == "RUNNING"\'',
            comment="combine filters in a single CEL expression",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_message_filtered_backend(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with other commands: message only backend agents by passing "-" to have the list of matching agents piped in via stdin
        mngr list --include 'labels.team == "backend"' --ids | mngr message - -m "Please run the backend test suite"
    """)
    expect(
        e2e.run(
            'mngr list --include \'labels.team == "backend"\' --ids | mngr message - -m "Please run the backend test suite"',
            comment="message only backend agents via filter+stdin",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_exec_filtered_remote_disk(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with exec: check disk usage on remote agents only
        mngr list --include 'host.provider == "modal"' --ids | mngr exec - "df -h /workspace"
    """)
    expect(
        e2e.run(
            'mngr list --include \'host.provider == "modal"\' --ids | mngr exec - "df -h /workspace"',
            comment="exec across remote agents only",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_destroy_filtered_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use filters with destroy: clean up all stopped agents for a team
        mngr list --include 'labels.team == "backend"' --include 'state == "STOPPED"' --ids | mngr destroy - --force --dry-run
    """)
    expect(
        e2e.run(
            "mngr list --include 'labels.team == \"backend\"' --include 'state == \"STOPPED\"' --ids | mngr destroy - --force --dry-run",
            comment="dry-run destroy via filter+stdin",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_jq_filter(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also just list agents by filtering using jq:
        mngr list --format json | jq '.[] | select(.labels.priority == "high")'
    """)
    expect(
        e2e.run(
            "mngr list --format json | jq '.[] | select(.labels.priority == \"high\")'",
            comment="list with jq filter",
        )
    ).to_succeed()


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
