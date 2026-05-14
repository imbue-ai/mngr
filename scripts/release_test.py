import sys
from pathlib import Path

import pytest

# scripts/release.py uses bare imports of its sibling modules (e.g.
# `from changelog_release_utils import ...`), matching how it's invoked
# (`uv run scripts/release.py ...`). Make those resolvable for pytest by
# adding scripts/ to sys.path before importing release.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.release import REPO_ROOT  # noqa: E402
from scripts.release import _gate_release_on_pending_changelog_entries  # noqa: E402
from scripts.release import _pluralize_entry  # noqa: E402


def test_pluralize_entry_singular() -> None:
    assert _pluralize_entry(1) == "entry"


def test_pluralize_entry_plural_zero() -> None:
    assert _pluralize_entry(0) == "entries"


def test_pluralize_entry_plural_multiple() -> None:
    assert _pluralize_entry(5) == "entries"


def test_gate_returns_true_when_no_pending_entries(capsys: pytest.CaptureFixture[str]) -> None:
    result = _gate_release_on_pending_changelog_entries([], dry_run=False)
    assert result is True
    assert capsys.readouterr().out == ""


def test_gate_returns_true_when_no_pending_entries_and_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    result = _gate_release_on_pending_changelog_entries([], dry_run=True)
    assert result is True
    assert capsys.readouterr().out == ""


def test_gate_warns_and_returns_true_in_dry_run_with_pending_entries(capsys: pytest.CaptureFixture[str]) -> None:
    entries = [REPO_ROOT / "changelog" / "fake-entry.md"]
    result = _gate_release_on_pending_changelog_entries(entries, dry_run=True)
    assert result is True
    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "1 pending changelog entry" in output
    assert "changelog/fake-entry.md" in output


def test_gate_blocks_and_returns_false_with_pending_entries(capsys: pytest.CaptureFixture[str]) -> None:
    entries = [
        REPO_ROOT / "changelog" / "fake-a.md",
        REPO_ROOT / "changelog" / "fake-b.md",
    ]
    result = _gate_release_on_pending_changelog_entries(entries, dry_run=False)
    assert result is False
    captured = capsys.readouterr()
    # The blocking-error path writes to stderr (matches the rest of
    # release.py's 'ERROR:' convention); stdout should stay empty.
    assert captured.out == ""
    err = captured.err
    assert "ERROR" in err
    assert "2 pending changelog entries" in err
    assert "changelog/fake-a.md" in err
    assert "changelog/fake-b.md" in err
    # The error path prints the on-demand command for the user to copy.
    assert "mngr schedule run" in err
