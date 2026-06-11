import pytest

from imbue.minds.desktop_client.e2e_workspace_runner import _current_mngr_branch


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
    ignored so resolution falls through to git rather than asking FCT for a
    `<n>/merge` branch."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "2065/merge")
    assert _current_mngr_branch() != "2065/merge"
