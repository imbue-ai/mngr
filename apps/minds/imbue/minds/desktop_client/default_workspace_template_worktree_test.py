from pathlib import Path

import pytest

from imbue.minds.desktop_client.default_workspace_template_worktree import _current_mngr_branch
from imbue.minds.desktop_client.default_workspace_template_worktree import (
    materialize_paired_default_workspace_template_worktree,
)
from imbue.minds.desktop_client.default_workspace_template_worktree import template_configuration_identity
from imbue.minds.testing import make_git_repo
from imbue.mngr.utils.testing import run_git_command


def test_current_mngr_branch_prefers_github_head_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a pull_request CI run the checkout is detached, so the PR source
    branch must come from GITHUB_HEAD_REF rather than `git rev-parse HEAD`."""
    monkeypatch.setenv("GITHUB_HEAD_REF", "mngr/some-feature")
    monkeypatch.setenv("GITHUB_REF_NAME", "123/merge")
    assert _current_mngr_branch() == "mngr/some-feature"


def test_current_mngr_branch_uses_github_ref_name_for_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a push CI run GITHUB_HEAD_REF is unset and GITHUB_REF_NAME is the branch."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert _current_mngr_branch() == "main"


def test_current_mngr_branch_ignores_pr_merge_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR's GITHUB_REF_NAME is a `<n>/merge` ref, not a real branch; it must be
    ignored so resolution falls through to git rather than asking DEFAULT_WORKSPACE_TEMPLATE for a
    `<n>/merge` branch."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "2065/merge")
    assert _current_mngr_branch() != "2065/merge"


def test_materialize_reports_the_coordinate_of_a_tree_that_is_already_present(tmp_path: Path) -> None:
    """An operator's own worktree is never clobbered, and the run still has to report
    which DEFAULT_WORKSPACE_TEMPLATE tree built its workspace, so the coordinate is read
    back off whatever tree is there."""
    destination = make_git_repo(tmp_path, name="default-workspace-template")
    run_git_command(destination, "checkout", "-b", "author/one")
    head = run_git_command(destination, "rev-parse", "HEAD").stdout.strip()

    materialized = materialize_paired_default_workspace_template_worktree(destination)

    assert materialized.path == destination
    assert materialized.ref == "author/one"
    assert materialized.commit == head
    assert template_configuration_identity(materialized.ref) == "default-workspace-template@author/one"


@pytest.mark.flaky
def test_materialize_names_a_detached_tree_by_its_commit(tmp_path: Path) -> None:
    """A detached tree has no branch to name. Its commit becomes the coordinate rather
    than an empty identity, which would collapse unrelated trees into one group."""
    destination = make_git_repo(tmp_path, name="default-workspace-template")
    head = run_git_command(destination, "rev-parse", "HEAD").stdout.strip()
    run_git_command(destination, "checkout", "--detach", head)

    materialized = materialize_paired_default_workspace_template_worktree(destination)

    assert materialized.ref == head
    assert materialized.commit == head
