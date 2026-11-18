from pathlib import Path

import pytest

from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from scripts.check_changelog_entries import changed_files_against_base
from scripts.check_changelog_entries import find_missing_entries
from scripts.check_changelog_entries import is_exempt_branch
from scripts.check_changelog_entries import main
from scripts.check_changelog_entries import projects_stamping_a_release
from scripts.check_changelog_entries import resolve_diff_base
from scripts.check_changelog_entries import touched_projects

_RELEASED_SECTION = "## [v1.0.0] - 2026-05-01\n\n- Added: An old feature.\n"
_UNSTAMPED_CHANGELOG = f"# Changelog\n\n## [Unreleased]\n\n- Added: A feature.\n\n{_RELEASED_SECTION}"
_STAMPED_CHANGELOG = (
    f"# Changelog\n\n## [Unreleased]\n\n## [v1.1.0] - 2026-05-11\n\n- Added: A feature.\n\n{_RELEASED_SECTION}"
)


def _init_repo(tmp_path: Path) -> Path:
    """Create a git repo with a project skeleton committed on main.

    Layout mirrors the real repo enough for the project-mapping helpers: a
    ``libs/mngr`` project with a ``pyproject.toml`` and a ``changelog/``
    directory, plus the synthetic ``dev`` bucket directories. Builds on the
    shared ``init_git_repo`` helper (git config isolation, branch ``main``,
    initial commit), then commits the skeleton on top.
    """
    repo = tmp_path / "repo"
    init_git_repo(repo, initial_commit=True)
    (repo / "libs" / "mngr" / "changelog").mkdir(parents=True)
    (repo / "libs" / "mngr" / "pyproject.toml").write_text("")
    (repo / "dev" / "changelog").mkdir(parents=True)
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "project skeleton")
    return repo


def _branch_changing_the_changelog(tmp_path: Path, branch: str, before: str, after: str) -> Path:
    """Create a repo whose ``branch`` changes ``libs/mngr/CHANGELOG.md`` from ``before`` (on main) to ``after``."""
    repo = _init_repo(tmp_path)
    (repo / "libs" / "mngr" / "CHANGELOG.md").write_text(before)
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "changelog")
    run_git_command(repo, "checkout", "-b", branch)
    (repo / "libs" / "mngr" / "CHANGELOG.md").write_text(after)
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change the changelog")
    return repo


@pytest.fixture(autouse=True)
def _clear_github_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear GitHub Actions env so branch/base detection uses the temp repo's git."""
    for var in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_BASE_REF", "CHANGELOG_DIFF_BASE_REF"):
        monkeypatch.delenv(var, raising=False)


def test_resolve_diff_base_rejects_base_equal_to_head(tmp_path: Path) -> None:
    """The core regression: a repo where main == HEAD (the offload sandbox
    shape) must NOT yield a usable base -- it must raise, not pass."""
    repo = _init_repo(tmp_path)
    # Branch off without adding a commit: HEAD == main, exactly the sandbox case.
    run_git_command(repo, "checkout", "-b", "feature")

    with pytest.raises(RuntimeError, match="distinct from HEAD"):
        resolve_diff_base(repo)


def test_resolve_diff_base_returns_main_when_distinct(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    assert resolve_diff_base(repo) == "main"


def test_resolve_diff_base_prefers_the_explicit_override_over_the_payload_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stacked PRs: the CI workflow resolves the live base branch into
    CHANGELOG_DIFF_BASE_REF because the webhook payload (GITHUB_BASE_REF)
    reports the trunk for PRs in a native GitHub stack. The override must win."""
    repo = _init_repo(tmp_path)
    # A stack: main <- layer-one <- feature. The payload claims main; the live
    # base is layer-one.
    run_git_command(repo, "checkout", "-b", "layer-one")
    (repo / "libs" / "mngr" / "lower.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "lower layer")
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "upper.py").write_text("x = 2\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "upper layer")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("CHANGELOG_DIFF_BASE_REF", "layer-one")

    base = resolve_diff_base(repo)

    assert base == "layer-one"
    changed = changed_files_against_base(base, repo)
    assert "libs/mngr/upper.py" in changed
    assert "libs/mngr/lower.py" not in changed


def test_changed_files_detects_project_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    changed = changed_files_against_base("main", repo)
    assert "libs/mngr/thing.py" in changed
    assert touched_projects(changed, repo) == {"mngr"}


def test_root_file_maps_to_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "justfile").write_text("recipe:\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "root change")

    changed = changed_files_against_base("main", repo)
    assert touched_projects(changed, repo) == {"dev"}


def test_find_missing_entries(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # mngr has no entry for this branch; dev does.
    (repo / "dev" / "changelog" / "feature-x.md").write_text("entry\n")
    missing = find_missing_entries("feature/x", {"mngr", "dev"}, repo)
    assert missing == ["libs/mngr/changelog/feature-x.md"]


def test_main_fails_when_entry_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change")

    assert main(repo) == 1


def test_main_passes_when_entry_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")
    (repo / "libs" / "mngr" / "thing.py").write_text("x = 1\n")
    (repo / "libs" / "mngr" / "changelog" / "feature.md").write_text("did a thing\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "change with entry")

    assert main(repo) == 0


def test_main_needs_no_entry_for_a_project_whose_changelog_gains_a_release_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _branch_changing_the_changelog(tmp_path, "release", _UNSTAMPED_CHANGELOG, _STAMPED_CHANGELOG)
    (repo / "libs" / "mngr" / "version.py").write_text('VERSION = "1.1.0"\n')
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "bump the version")

    assert main(repo) == 0
    assert "['mngr'] need none" in capsys.readouterr().out


def test_main_still_needs_entries_for_the_other_projects_a_release_touches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _branch_changing_the_changelog(tmp_path, "release", _UNSTAMPED_CHANGELOG, _STAMPED_CHANGELOG)
    (repo / "justfile").write_text("recipe:\n")
    run_git_command(repo, "add", "-A")
    run_git_command(repo, "commit", "-m", "root change")

    assert main(repo) == 1
    err = capsys.readouterr().err
    assert "dev/changelog/release.md" in err
    assert "libs/mngr/changelog/release.md" not in err


@pytest.mark.parametrize(
    "edited_changelog",
    [
        pytest.param(_UNSTAMPED_CHANGELOG.replace("A feature.", "A better feature."), id="unreleased-bullet"),
        pytest.param(_UNSTAMPED_CHANGELOG.replace("An old feature.", "An older feature."), id="released-bullet"),
    ],
)
def test_main_needs_an_entry_for_a_changelog_edit_that_stamps_no_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edited_changelog: str
) -> None:
    repo = _branch_changing_the_changelog(tmp_path, "correction", _UNSTAMPED_CHANGELOG, edited_changelog)

    assert main(repo) == 1
    assert "libs/mngr/changelog/correction.md" in capsys.readouterr().err


def test_projects_stamping_a_release_ignores_an_added_unreleased_heading(tmp_path: Path) -> None:
    repo = _branch_changing_the_changelog(
        tmp_path, "setup", f"# Changelog\n\n{_RELEASED_SECTION}", _UNSTAMPED_CHANGELOG
    )

    assert projects_stamping_a_release("main", {"mngr"}, repo) == set()


def test_projects_stamping_a_release_names_only_the_projects_that_stamp(tmp_path: Path) -> None:
    repo = _branch_changing_the_changelog(tmp_path, "release", _UNSTAMPED_CHANGELOG, _STAMPED_CHANGELOG)

    assert projects_stamping_a_release("main", {"mngr", "dev"}, repo) == {"mngr"}


def test_projects_stamping_a_release_ignores_a_heading_main_changed_after_the_branch_was_cut(tmp_path: Path) -> None:
    repo = _branch_changing_the_changelog(
        tmp_path, "feature", _STAMPED_CHANGELOG, _STAMPED_CHANGELOG.replace("- Added: A feature.", "- Added: It.")
    )
    run_git_command(repo, "checkout", "main")
    (repo / "libs" / "mngr" / "CHANGELOG.md").write_text(_STAMPED_CHANGELOG.replace("2026-05-11", "2026-05-12"))
    run_git_command(repo, "commit", "-am", "re-date the release on main")
    run_git_command(repo, "checkout", "feature")

    assert projects_stamping_a_release("main", {"mngr"}, repo) == set()


def test_main_errors_in_sandbox_shape(tmp_path: Path) -> None:
    """End-to-end: the offload-sandbox shape (main == HEAD) returns exit 2, the
    loud-failure code -- never a vacuous 0."""
    repo = _init_repo(tmp_path)
    run_git_command(repo, "checkout", "-b", "feature")

    assert main(repo) == 2


def test_main_skips_on_main_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    # Still on 'main'; nothing to enforce.
    assert main(repo) == 0


def test_is_exempt_branch() -> None:
    assert is_exempt_branch("mngr/changelog-consolidation-2026-06-13")
    assert is_exempt_branch("dependabot/github_actions/github-actions-abc123")
    assert not is_exempt_branch("mngr/some-feature")
