"""Unit tests for bake source resolution + identity derivation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_imbue_cloud.bake.bake_source import BakeSource
from imbue.mngr_imbue_cloud.bake.bake_source import BakeSourceError
from imbue.mngr_imbue_cloud.bake.bake_source import _clone_repo_at_tag
from imbue.mngr_imbue_cloud.bake.bake_source import _verify_remote_has_tag
from imbue.mngr_imbue_cloud.bake.bake_source import merge_bake_identity_attributes
from imbue.mngr_imbue_cloud.bake.bake_source import resolved_bake_source
from imbue.mngr_imbue_cloud.errors import RepoIdentityError

_ORIGIN_URL: str = "git@github.com:imbue-ai/forever-claude-template.git"
_CANONICAL: str = "github.com/imbue-ai/forever-claude-template"


def _init_repo(repo_dir: Path, *, origin_url: str | None = None, branch: str = "main") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    if origin_url is not None:
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", origin_url], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
    (repo_dir / "f.txt").write_text("content")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True)


# --- merge_bake_identity_attributes ---


def test_merge_bake_identity_attributes_stamps_canonical_identity() -> None:
    source = BakeSource(workspace_dir=Path("/x"), repo_url=_CANONICAL, repo_branch_or_tag="v0.3.0")
    merged = merge_bake_identity_attributes({"memory_gb": 8}, source)
    assert merged == {"memory_gb": 8, "repo_url": _CANONICAL, "repo_branch_or_tag": "v0.3.0"}


@pytest.mark.parametrize("identity_key", ["repo_url", "repo_branch_or_tag"])
def test_merge_bake_identity_attributes_rejects_hand_passed_identity_keys(identity_key: str) -> None:
    source = BakeSource(workspace_dir=Path("/x"), repo_url=_CANONICAL, repo_branch_or_tag="v0.3.0")
    with pytest.raises(BakeSourceError):
        merge_bake_identity_attributes({identity_key: "whatever"}, source)


# --- resolved_bake_source: selector validation ---


def test_resolved_bake_source_requires_exactly_one_selector_neither() -> None:
    with pytest.raises(BakeSourceError):
        with resolved_bake_source(from_tag=None, workspace_dir=None, repo_url="x", repo_branch_or_tag_override=None):
            pass


def test_resolved_bake_source_requires_exactly_one_selector_both(tmp_path: Path) -> None:
    with pytest.raises(BakeSourceError):
        with resolved_bake_source(
            from_tag="v1", workspace_dir=str(tmp_path), repo_url="x", repo_branch_or_tag_override=None
        ):
            pass


# --- resolved_bake_source: workspace-dir (dev) mode ---


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolved_bake_source_workspace_dir_derives_origin_and_branch(tmp_path: Path) -> None:
    repo_dir = tmp_path / "fct"
    _init_repo(repo_dir, origin_url=_ORIGIN_URL, branch="josh/ovh-exploration")
    with resolved_bake_source(
        from_tag=None, workspace_dir=str(repo_dir), repo_url="ignored", repo_branch_or_tag_override=None
    ) as source:
        assert source.workspace_dir == repo_dir
        assert source.repo_url == _CANONICAL
        assert source.repo_branch_or_tag == "josh/ovh-exploration"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolved_bake_source_workspace_dir_relative_path_is_canonicalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative --workspace-dir (e.g. ``fct``, not ``./fct``) still canonicalizes via origin.

    Regression: the local-vs-URL heuristic keys on a leading / ./ ../ ~, so a bare
    relative path was misread as a URL and stamped verbatim. Resolving to absolute
    first fixes it.
    """
    repo_dir = tmp_path / "fct"
    _init_repo(repo_dir, origin_url=_ORIGIN_URL, branch="main")
    monkeypatch.chdir(tmp_path)
    with resolved_bake_source(
        from_tag=None, workspace_dir="fct", repo_url="ignored", repo_branch_or_tag_override=None
    ) as source:
        assert source.repo_url == _CANONICAL


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolved_bake_source_workspace_dir_branch_override_wins(tmp_path: Path) -> None:
    repo_dir = tmp_path / "fct"
    _init_repo(repo_dir, origin_url=_ORIGIN_URL, branch="some-branch")
    with resolved_bake_source(
        from_tag=None, workspace_dir=str(repo_dir), repo_url="ignored", repo_branch_or_tag_override="v9.9.9"
    ) as source:
        assert source.repo_branch_or_tag == "v9.9.9"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolved_bake_source_workspace_dir_without_origin_raises(tmp_path: Path) -> None:
    repo_dir = tmp_path / "fct"
    _init_repo(repo_dir, origin_url=None)
    with pytest.raises(RepoIdentityError):
        with resolved_bake_source(
            from_tag=None, workspace_dir=str(repo_dir), repo_url="ignored", repo_branch_or_tag_override=None
        ):
            pass


def test_resolved_bake_source_workspace_dir_not_a_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(BakeSourceError):
        with resolved_bake_source(
            from_tag=None,
            workspace_dir=str(tmp_path / "missing"),
            repo_url="ignored",
            repo_branch_or_tag_override=None,
        ):
            pass


# --- resolved_bake_source: from-tag (production) tag validation ---


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolved_bake_source_from_tag_rejects_non_tag(tmp_path: Path) -> None:
    # A local source repo (passed as repo_url) with NO matching tag: tag verification
    # fails before any clone/normalization, so the whole call raises BakeSourceError.
    source_repo = tmp_path / "source"
    _init_repo(source_repo, origin_url=_ORIGIN_URL)
    with pytest.raises(BakeSourceError):
        with resolved_bake_source(
            from_tag="v0.0.0-does-not-exist",
            workspace_dir=None,
            repo_url=str(source_repo),
            repo_branch_or_tag_override=None,
        ):
            pass


# --- the from-tag git helpers (exercised offline against a local source repo) ---


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_verify_remote_has_tag_passes_for_real_tag_and_fails_for_absent(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    _init_repo(source_repo, origin_url=_ORIGIN_URL)
    subprocess.run(["git", "-C", str(source_repo), "tag", "v1.2.3"], check=True)
    # Real tag: no raise.
    _verify_remote_has_tag(str(source_repo), "v1.2.3")
    # Absent tag: raises.
    with pytest.raises(BakeSourceError):
        _verify_remote_has_tag(str(source_repo), "v9.9.9")


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_clone_repo_at_tag_materializes_tagged_content(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    _init_repo(source_repo, origin_url=_ORIGIN_URL)
    subprocess.run(["git", "-C", str(source_repo), "tag", "v1.2.3"], check=True)
    dest = tmp_path / "clone"
    _clone_repo_at_tag(str(source_repo), "v1.2.3", dest)
    assert (dest / "f.txt").read_text() == "content"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_clone_repo_at_tag_raises_on_bad_tag(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    _init_repo(source_repo, origin_url=_ORIGIN_URL)
    dest = tmp_path / "clone"
    with pytest.raises(BakeSourceError):
        _clone_repo_at_tag(str(source_repo), "v0.0.0-nope", dest)
