import sys
import tomllib
from datetime import date
from pathlib import Path

import pytest

# scripts/release.py uses bare imports of its sibling modules (e.g.
# `from changelog_release_utils import ...`), matching how it's invoked
# (`uv run scripts/release.py ...`). Make those resolvable for pytest by
# adding scripts/ to sys.path before importing release.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from scripts.release import _gate_release_on_pending_changelog_entries  # noqa: E402
from scripts.release import _pluralize_entry  # noqa: E402
from scripts.release import _realign_dep_string  # noqa: E402
from scripts.release import update_exclude_newer  # noqa: E402


def _write_changelog_entry(tmp_path: Path, name: str, content: str = "- entry", project: str = "mngr") -> None:
    """Drop an entry under the per-project in-project layout (libs/<project>/changelog/<name>).

    Also stamps a stub ``pyproject.toml`` so ``all_known_projects()`` discovers the project.
    """
    project_dir = tmp_path / "libs" / project
    (project_dir / "changelog").mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text("")
    (project_dir / "changelog" / name).write_text(content)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "entries"),
        (1, "entry"),
        (5, "entries"),
    ],
)
def test_pluralize_entry(count: int, expected: str) -> None:
    assert _pluralize_entry(count) == expected


@pytest.mark.parametrize("dry_run", [False, True])
def test_gate_returns_true_when_no_pending_entries(
    tmp_path: Path, dry_run: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=dry_run)
    assert result is True
    assert capsys.readouterr().out == ""


def test_gate_warns_and_returns_true_in_dry_run_with_pending_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_changelog_entry(tmp_path, "fake-entry.md")
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=True)
    assert result is True
    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "1 pending changelog entry" in output
    assert "libs/mngr/changelog/fake-entry.md" in output


def test_gate_blocks_and_returns_false_with_pending_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_changelog_entry(tmp_path, "fake-a.md", project="mngr")
    _write_changelog_entry(tmp_path, "fake-b.md", project="mngr_lima")
    result = _gate_release_on_pending_changelog_entries(tmp_path, dry_run=False)
    assert result is False
    captured = capsys.readouterr()
    # The blocking-error path writes to stderr (matches the rest of
    # release.py's 'ERROR:' convention); stdout should stay empty.
    assert captured.out == ""
    err = captured.err
    assert "ERROR" in err
    assert "2 pending changelog entries" in err
    assert "libs/mngr/changelog/fake-a.md" in err
    assert "libs/mngr_lima/changelog/fake-b.md" in err
    # The error path points the user at the on-demand trigger recipe.
    assert "just changelog-trigger" in err


def test_realign_dep_string_realigns_existing_pin_regardless_of_force() -> None:
    # An existing == pin is always realigned, whether or not the dep is forced.
    assert _realign_dep_string("imbue-mngr==0.2.8", "0.2.10", force_pin=False) == "imbue-mngr==0.2.10"
    assert _realign_dep_string("imbue-mngr==0.2.8", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_leaves_unpinned_alone_without_force() -> None:
    # A deliberately-unpinned internal dep (non-publishable consumer) stays unpinned.
    assert _realign_dep_string("imbue-mngr", "0.2.10", force_pin=False) == "imbue-mngr"
    assert _realign_dep_string("imbue-mngr>=0.2.0", "0.2.10", force_pin=False) == "imbue-mngr>=0.2.0"


def test_realign_dep_string_introduces_pin_when_forced() -> None:
    # A publishable wheel must pin its internal deps, so force_pin adds the pin
    # (collapsing any looser specifier).
    assert _realign_dep_string("imbue-mngr", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"
    assert _realign_dep_string("imbue-mngr>=0.2.0", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_no_op_when_already_correct() -> None:
    assert _realign_dep_string("imbue-mngr==0.2.10", "0.2.10", force_pin=True) == "imbue-mngr==0.2.10"


def test_realign_dep_string_rejects_extras_and_markers() -> None:
    # The collapse-to-`name==version` form would silently drop an extra or marker;
    # internal deps never carry one, so guard loudly if that assumption breaks.
    with pytest.raises(AssertionError):
        _realign_dep_string("imbue-mngr==0.2.8 ; python_version < '3.12'", "0.2.10", force_pin=False)
    with pytest.raises(AssertionError):
        _realign_dep_string("imbue-mngr[extra]==0.2.8", "0.2.10", force_pin=False)


def _write_root_pyproject(tmp_path: Path, exclude_newer: str) -> Path:
    """Write a minimal root pyproject.toml carrying a `[tool.uv] exclude-newer`.

    Includes an unrelated key under [tool.uv] so the tests can assert that
    update_exclude_newer rewrites only the cutoff and preserves the rest.
    """
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[tool.uv]\nexclude-newer = "{exclude_newer}"\n\n[tool.uv.sources]\nimbue-common = {{ workspace = true }}\n'
    )
    return path


def test_update_exclude_newer_advances_stale_cutoff(tmp_path: Path) -> None:
    # A cutoff well older than two weeks before the release date is advanced to
    # exactly (release_date - 2 weeks), and unrelated config is preserved.
    path = _write_root_pyproject(tmp_path, "2026-01-01T00:00:00Z")
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result == "2026-05-13T00:00:00Z"
    doc = tomllib.loads(path.read_text())
    assert doc["tool"]["uv"]["exclude-newer"] == "2026-05-13T00:00:00Z"
    assert doc["tool"]["uv"]["sources"]["imbue-common"] == {"workspace": True}


def test_update_exclude_newer_keeps_recent_cutoff(tmp_path: Path) -> None:
    # A cutoff younger than the cooldown window (only 4 days before the release
    # date) must be left untouched: advancing it would push it back and re-exclude
    # whatever freshly-pinned dep it was set to admit.
    path = _write_root_pyproject(tmp_path, "2026-05-23T00:00:00Z")
    original = path.read_text()
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result is None
    assert path.read_text() == original


def test_update_exclude_newer_noop_at_window_boundary(tmp_path: Path) -> None:
    # A cutoff exactly at (release_date - 2 weeks) is a no-op: max() ties to the
    # current value, so no rewrite happens.
    path = _write_root_pyproject(tmp_path, "2026-05-13T00:00:00Z")
    result = update_exclude_newer(path, date(2026, 5, 27))
    assert result is None
