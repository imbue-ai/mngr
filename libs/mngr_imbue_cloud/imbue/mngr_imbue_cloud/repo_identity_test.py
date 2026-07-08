"""Unit tests for repo_identity canonicalization."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_imbue_cloud.errors import RepoIdentityError
from imbue.mngr_imbue_cloud.repo_identity import canonicalize_repo_source
from imbue.mngr_imbue_cloud.repo_identity import is_local_repo_source
from imbue.mngr_imbue_cloud.repo_identity import normalize_repo_url
from imbue.mngr_imbue_cloud.repo_identity import resolve_repo_current_branch
from imbue.mngr_imbue_cloud.repo_identity import resolve_repo_origin_url

_CANONICAL: str = "github.com/imbue-ai/forever-claude-template"


@pytest.mark.parametrize(
    "raw_url",
    [
        "https://github.com/imbue-ai/forever-claude-template.git",
        "https://github.com/imbue-ai/forever-claude-template",
        "https://github.com/imbue-ai/forever-claude-template/",
        "git@github.com:imbue-ai/forever-claude-template.git",
        "git@github.com:imbue-ai/forever-claude-template",
        "ssh://git@github.com/imbue-ai/forever-claude-template.git",
        "git://github.com/imbue-ai/forever-claude-template.git",
        "https://GitHub.com/imbue-ai/forever-claude-template.git",
        "https://user:secret@github.com/imbue-ai/forever-claude-template.git",
    ],
)
def test_normalize_repo_url_collapses_equivalent_forms_to_one_key(raw_url: str) -> None:
    assert normalize_repo_url(raw_url) == _CANONICAL


def test_normalize_repo_url_preserves_org_repo_case_but_lowercases_host() -> None:
    assert (
        normalize_repo_url("https://GitHub.com/Imbue-AI/Forever-Claude-Template.git")
        == "github.com/Imbue-AI/Forever-Claude-Template"
    )


@pytest.mark.parametrize("bad", ["", "   ", "https://", "github.com", "git@github.com:"])
def test_normalize_repo_url_rejects_inputs_without_host_and_path(bad: str) -> None:
    with pytest.raises(RepoIdentityError):
        normalize_repo_url(bad)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/home/user/project/fct", True),
        ("./fct", True),
        ("../fct", True),
        ("~/project/fct", True),
        ("https://github.com/imbue-ai/forever-claude-template.git", False),
        ("git@github.com:imbue-ai/forever-claude-template.git", False),
        ("ssh://git@github.com/imbue-ai/forever-claude-template", False),
    ],
)
def test_is_local_repo_source_distinguishes_paths_from_urls(value: str, expected: bool) -> None:
    assert is_local_repo_source(value) is expected


def _init_repo_with_origin(repo_dir: Path, origin_url: str, *, branch: str = "main") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", origin_url], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "t"], check=True)
    (repo_dir / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_canonicalize_repo_source_resolves_local_path_to_origin(tmp_path: Path) -> None:
    repo_dir = tmp_path / "clone"
    _init_repo_with_origin(repo_dir, "git@github.com:imbue-ai/forever-claude-template.git")
    assert canonicalize_repo_source(str(repo_dir)) == _CANONICAL


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolve_repo_current_branch_returns_checked_out_branch(tmp_path: Path) -> None:
    repo_dir = tmp_path / "clone"
    _init_repo_with_origin(repo_dir, "git@github.com:imbue-ai/forever-claude-template.git", branch="josh/exploration")
    assert resolve_repo_current_branch(repo_dir) == "josh/exploration"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_resolve_repo_origin_url_raises_without_origin(tmp_path: Path) -> None:
    repo_dir = tmp_path / "no_origin"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    with pytest.raises(RepoIdentityError):
        resolve_repo_origin_url(repo_dir)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_canonicalize_repo_source_remote_url_does_not_touch_filesystem() -> None:
    # A remote URL is normalized directly (no git invocation / path resolution).
    assert canonicalize_repo_source("https://github.com/imbue-ai/forever-claude-template") == _CANONICAL
