from imbue.mngr_diagnose.prompt import build_diagnose_initial_message


def test_message_with_description_and_traceback() -> None:
    msg = build_diagnose_initial_message(
        description="Create fails with spaces",
        traceback_str="Traceback:\n  File foo.py\nValueError: bad",
        mngr_version="0.2.4",
    )
    assert "## mngr Version" in msg
    assert "0.2.4" in msg
    assert "## Problem Description" in msg
    assert "Create fails with spaces" in msg
    assert "## Error Traceback" in msg
    assert "Traceback:\n  File foo.py\nValueError: bad" in msg
    assert "No details were provided" not in msg


def test_message_with_description_only() -> None:
    msg = build_diagnose_initial_message(
        description="Something broke",
        traceback_str=None,
        mngr_version="0.2.4",
    )
    assert "## Problem Description" in msg
    assert "Something broke" in msg
    assert "## Error Traceback" not in msg
    assert "No details were provided" not in msg


def test_message_with_traceback_only() -> None:
    msg = build_diagnose_initial_message(
        description=None,
        traceback_str="Traceback:\n  ValueError",
        mngr_version="0.2.4",
    )
    assert "## Problem Description" not in msg
    assert "## Error Traceback" in msg
    assert "Traceback:\n  ValueError" in msg
    assert "No details were provided" not in msg


def test_message_with_no_details() -> None:
    msg = build_diagnose_initial_message(
        description=None,
        traceback_str=None,
        mngr_version="0.2.4",
    )
    assert "## Problem Description" not in msg
    assert "## Error Traceback" not in msg
    assert "No details were provided. Ask the user to describe the problem before proceeding." in msg


def test_message_includes_agent_instructions() -> None:
    msg = build_diagnose_initial_message(
        description="test",
        traceback_str=None,
        mngr_version="0.2.4",
    )
    assert "python scripts/open-issue.py" in msg
    assert "Root cause analysis" in msg
    assert "worktree of the repository" in msg
