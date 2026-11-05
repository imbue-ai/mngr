import os
import socket
import time
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.agent_creator import checkout_branch
from imbue.minds.desktop_client.agent_creator import clone_git_repo
from imbue.minds.desktop_client.testing import make_test_workspace_template_cache
from imbue.minds.desktop_client.testing import materialize_cached_release
from imbue.minds.desktop_client.workspace_template_cache import WorkspaceTemplateCache
from imbue.minds.desktop_client.workspace_template_cache import WorkspaceTemplateCacheStatus
from imbue.minds.desktop_client.workspace_template_cache import make_workspace_template_cache
from imbue.minds.desktop_client.workspace_template_cache import resolve_release_tag_commit
from imbue.minds.desktop_client.workspace_template_cache import template_cache_entry_name
from imbue.minds.primitives import GitUrl
from imbue.minds.primitives import TemplateReleaseTag
from imbue.mngr.utils.git_utils import GIT_MIRROR_PUSH_REFSPECS
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command

_RELEASE_TAG = TemplateReleaseTag("minds-v4.5.6")


def _git(repo: Path, *args: str) -> str:
    return run_git_command(repo, *args).stdout.strip()


def _release_commit(origin: Path) -> str:
    return _git(origin, "rev-parse", f"{_RELEASE_TAG}^{{commit}}")


def _make_tagged_origin(tmp_path: Path) -> Path:
    """A repo whose release tag (annotated, as the release process makes them) sits on a second commit.

    The release carries an executable file and a symlink, which a copy must bring over as they are.
    """
    origin = tmp_path / "origin"
    init_git_repo(origin)
    script = origin / "run.sh"
    script.write_text("#!/bin/sh\necho 51842\n")
    script.chmod(0o755)
    (origin / "readme-link").symlink_to("README.md")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "release commit")
    _git(origin, "tag", "-a", str(_RELEASE_TAG), "-m", "release")
    return origin


def _url(origin: Path) -> str:
    return "file://{}".format(origin)


def _make_cache(origin: Path, tmp_path: Path, concurrency_group: ConcurrencyGroup) -> WorkspaceTemplateCache:
    return make_test_workspace_template_cache(_url(origin), _RELEASE_TAG, tmp_path, concurrency_group)


def _repo_state(repo: Path) -> dict[str, str]:
    """Everything about a clone that a later checkout, ``mngr create``, or mirror push reads, as comparable text."""
    state = {
        name: _git(repo, *args)
        for name, args in {
            "refs": ("for-each-ref",),
            "index": ("ls-files", "--stage"),
            "status": ("status", "--porcelain", "--ignored"),
            "origin": ("remote", "get-url", "origin"),
        }.items()
    }
    for meta_name in ("HEAD", "FETCH_HEAD", "config"):
        state[meta_name] = (repo / ".git" / meta_name).read_text()
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            state[str(relative)] = "link -> {}".format(os.readlink(path))
        elif path.is_file():
            state[str(relative)] = "mode {:o}: {}".format(path.stat().st_mode & 0o777, path.read_text())
        else:
            assert path.is_dir(), f"unexpected file type at {path}"
    return state


def test_a_copied_clone_is_the_repository_the_network_clone_makes(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    cloned = tmp_path / "cloned" / "repo"
    clone_git_repo(GitUrl(_url(origin)), cloned, branch=_RELEASE_TAG)
    copied = tmp_path / "copied" / "repo"
    waits: list[str] = []
    assert materialize_cached_release(cache, copied, waits)

    assert _repo_state(copied) == _repo_state(cloned)
    assert waits == []

    # What the create does next must come out the same too: the release as a named local branch.
    checkout_branch(cloned, _RELEASE_TAG)
    checkout_branch(copied, _RELEASE_TAG)
    assert _repo_state(copied) == _repo_state(cloned)
    assert _git(copied, "rev-parse", "--abbrev-ref", "HEAD") == str(_RELEASE_TAG)
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


def test_a_copied_clone_hardlinks_the_cached_objects(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])

    # A fetch this small leaves loose objects rather than a pack; every one must be shared.
    cached_objects_dir = cache.entry_dir / ".git" / "objects"
    cached_object_files = sorted(path for path in cached_objects_dir.rglob("*") if path.is_file())
    assert cached_object_files
    for cached_object_file in cached_object_files:
        copied_object_file = copied / ".git" / "objects" / cached_object_file.relative_to(cached_objects_dir)
        assert copied_object_file.stat().st_ino == cached_object_file.stat().st_ino
    # The index is the copy's own file, so checking out in the copy cannot touch the cache.
    assert (copied / ".git" / "index").stat().st_ino != (cache.entry_dir / ".git" / "index").stat().st_ino


def test_a_copied_clone_mirror_pushes_the_release_with_its_full_history(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])
    checkout_branch(copied, _RELEASE_TAG)

    bare = tmp_path / "bare.git"
    run_git_command(tmp_path, "init", "-q", "--bare", str(bare))
    _git(copied, "push", "--force", "--prune", str(bare), *GIT_MIRROR_PUSH_REFSPECS)

    assert _git(bare, "for-each-ref", "--format=%(refname)") == f"refs/heads/{_RELEASE_TAG}"
    assert _git(bare, "rev-list", "--count", str(_RELEASE_TAG)) == _git(
        origin, "rev-list", "--count", str(_RELEASE_TAG)
    )


@pytest.mark.parametrize(
    "repo_suffix, ref",
    [
        ("", "main"),
        ("", "release-sha"),
        ("/", str(_RELEASE_TAG)),
        (".git", str(_RELEASE_TAG)),
    ],
)
def test_a_create_of_another_repo_or_ref_is_left_to_clone(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, repo_suffix: str, ref: str
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    requested_ref = _release_commit(origin) if ref == "release-sha" else ref

    clone_dir = tmp_path / "clone" / "repo"
    waits: list[str] = []
    is_copied = cache.materialize_clone(
        repo_url=_url(origin) + repo_suffix,
        ref=requested_ref,
        clone_dir=clone_dir,
        stall_timeout_seconds=30.0,
        on_waiting=lambda: waits.append("waited"),
    )

    assert not is_copied
    assert not clone_dir.exists()
    assert waits == []


def test_a_create_before_any_prefetch_clones_without_waiting(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    cache = _make_cache(_make_tagged_origin(tmp_path), tmp_path, root_concurrency_group)
    waits: list[str] = []

    assert not materialize_cached_release(cache, tmp_path / "clone" / "repo", waits)
    assert waits == []
    assert cache.status is WorkspaceTemplateCacheStatus.IDLE


@pytest.mark.timeout(30)
def test_a_create_during_the_prefetch_copies_once_it_finishes(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)

    cache.start_prefetch()
    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])

    assert cache.status is WorkspaceTemplateCacheStatus.READY
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


def test_a_failed_prefetch_leaves_creates_to_clone_at_once(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    cache = _make_cache(tmp_path / "no-such-origin-30417", tmp_path, root_concurrency_group)

    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.UNAVAILABLE
    waits: list[str] = []
    assert not materialize_cached_release(cache, tmp_path / "clone" / "repo", waits)
    assert waits == []
    assert not any((tmp_path / "template-cache").iterdir())


def test_a_later_prefetch_reuses_a_current_clone(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    origin = _make_tagged_origin(tmp_path)
    assert _make_cache(origin, tmp_path, root_concurrency_group).prefetch_once() is WorkspaceTemplateCacheStatus.READY
    later_cache = _make_cache(origin, tmp_path, root_concurrency_group)
    # A fresh clone would not carry this file.
    marker = later_cache.entry_dir / "kept-clone-marker-51150"
    marker.touch()

    assert later_cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    assert marker.exists()


def test_a_later_prefetch_keeps_the_clone_when_the_remote_is_unreachable(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    release_commit = _release_commit(origin)
    assert _make_cache(origin, tmp_path, root_concurrency_group).prefetch_once() is WorkspaceTemplateCacheStatus.READY
    origin.rename(tmp_path / "origin-gone")

    later_cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert later_cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(later_cache, copied, [])
    assert _git(copied, "rev-parse", "HEAD") == release_commit


def test_a_later_prefetch_reclones_a_release_tag_that_moved_upstream(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    assert _make_cache(origin, tmp_path, root_concurrency_group).prefetch_once() is WorkspaceTemplateCacheStatus.READY
    (origin / "README.md").write_text("retagged 77120\n")
    _git(origin, "commit", "-am", "fix after tagging")
    _git(origin, "tag", "-f", "-a", str(_RELEASE_TAG), "-m", "release again")

    later_cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert later_cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(later_cache, copied, [])
    assert (copied / "README.md").read_text() == "retagged 77120\n"
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


@pytest.mark.parametrize("damage", ["missing", "stale"])
def test_a_copy_checks_out_the_release_whatever_the_cached_index_holds(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, damage: str
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    cloned = tmp_path / "cloned" / "repo"
    clone_git_repo(GitUrl(_url(origin)), cloned, branch=_RELEASE_TAG)
    if damage == "missing":
        (cache.entry_dir / ".git" / "index").unlink()
    else:
        (cache.entry_dir / "README.md").write_text("staged in the cache 40213\n")
        _git(cache.entry_dir, "add", "README.md")

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])

    assert _repo_state(copied) == _repo_state(cloned)
    checkout_branch(copied, _RELEASE_TAG)
    assert _git(copied, "status", "--porcelain") == ""
    assert (copied / "README.md").read_text() == (origin / "README.md").read_text()


def test_a_copy_leaves_out_a_socket_in_the_cached_git_dir(
    tmp_path: Path, short_tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Git's filesystem monitor, when a user turns it on, keeps a Unix socket in ``.git``, which no copy can carry."""
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    # A socket path is limited to about 100 bytes, so bind it short and move it in.
    bound_path = short_tmp_path / "s"
    with socket.socket(socket.AF_UNIX) as unix_socket:
        unix_socket.bind(str(bound_path))
    bound_path.rename(cache.entry_dir / ".git" / "fsmonitor--daemon.ipc")

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])

    assert not (copied / ".git" / "fsmonitor--daemon.ipc").exists()
    assert cache.status is WorkspaceTemplateCacheStatus.READY
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


def test_a_later_prefetch_replaces_a_damaged_clone(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    (cache.entry_dir / ".git" / "FETCH_HEAD").unlink()

    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])
    checkout_branch(copied, _RELEASE_TAG)
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


def test_a_copy_that_fails_leaves_no_clone_behind_and_stops_the_cache_serving(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A clone purged from under a ready cache (the OS may empty the cache root at any time) must not fail creates."""
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    for object_file in [path for path in (cache.entry_dir / ".git" / "objects").rglob("*") if path.is_file()]:
        object_file.unlink()

    clone_dir = tmp_path / "clone" / "repo"
    assert not materialize_cached_release(cache, clone_dir, [])

    assert not clone_dir.exists()
    assert cache.status is WorkspaceTemplateCacheStatus.UNAVAILABLE


def test_a_copy_that_fails_at_its_destination_keeps_the_cache_serving(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY
    # A finished copy cannot be renamed onto a directory that holds files.
    occupied_dir = tmp_path / "occupied" / "repo"
    occupied_dir.mkdir(parents=True)
    (occupied_dir / "unrelated-61530").write_text("kept\n")

    assert not materialize_cached_release(cache, occupied_dir, [])

    assert sorted(path.name for path in occupied_dir.parent.iterdir()) == ["repo"]
    assert [path.name for path in occupied_dir.iterdir()] == ["unrelated-61530"]
    assert cache.status is WorkspaceTemplateCacheStatus.READY
    copied = tmp_path / "copied" / "repo"
    assert materialize_cached_release(cache, copied, [])
    assert _git(copied, "rev-parse", "HEAD") == _release_commit(origin)


def test_a_prefetch_clears_its_other_releases_and_crash_leftovers_but_nothing_else(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    cache = _make_cache(origin, tmp_path, root_concurrency_group)
    cache_dir = tmp_path / "template-cache"
    for previous_release in (cache_dir / "minds-v4.5.5-0123456789ab", cache_dir / "minds-v4.5.6-rc.1-0123456789ab"):
        (previous_release / ".git").mkdir(parents=True)
    # Files the cache did not create: an older build's clone, and the marker the
    # legacy-state migration check expects to find here after the app has run.
    legacy_clone = cache_dir / "forever-claude-template"
    legacy_clone.mkdir()
    migration_marker = cache_dir / "ci-legacy-marker"
    migration_marker.write_text("ci-legacy-template-cache\n")
    crashed_clone = cache_dir / ".incoming-crashed"
    crashed_clone.mkdir()
    crashed_delete = cache_dir / ".discarded-crashed"
    crashed_delete.mkdir()
    a_day_ago = time.time() - 25 * 60 * 60
    for leftover in (crashed_clone, crashed_delete):
        os.utime(leftover, (a_day_ago, a_day_ago))
    live_clone = cache_dir / ".incoming-live"
    live_clone.mkdir()

    assert cache.prefetch_once() is WorkspaceTemplateCacheStatus.READY

    assert sorted(path.name for path in cache_dir.iterdir()) == sorted(
        [cache.entry_dir.name, live_clone.name, legacy_clone.name, migration_marker.name]
    )


def test_a_pin_that_is_not_a_release_tag_turns_the_cache_off(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    cache = make_workspace_template_cache(
        repo_url=_url(tmp_path / "origin"),
        pinned_ref="main",
        cache_dir=tmp_path / "template-cache",
        concurrency_group=root_concurrency_group,
    )

    assert cache is None


def test_a_prerelease_pin_is_cached(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    cache = make_workspace_template_cache(
        repo_url=_url(tmp_path / "origin"),
        pinned_ref="minds-v0.9.0-rc.1",
        cache_dir=tmp_path / "template-cache",
        concurrency_group=root_concurrency_group,
    )

    assert cache is not None
    assert cache.release_tag == "minds-v0.9.0-rc.1"


def test_cache_entry_names_separate_repos_and_releases() -> None:
    name = template_cache_entry_name("https://github.com/imbue-ai/default-workspace-template.git", _RELEASE_TAG)

    assert name.startswith(f"{_RELEASE_TAG}-")
    assert name != template_cache_entry_name("https://github.com/imbue-ai/another-template.git", _RELEASE_TAG)
    assert name != template_cache_entry_name(
        "https://github.com/imbue-ai/default-workspace-template.git", TemplateReleaseTag("minds-v4.5.7")
    )


def test_resolve_release_tag_commit_peels_an_annotated_tag(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)

    commit = resolve_release_tag_commit(
        repo_url=_url(origin), release_tag=str(_RELEASE_TAG), concurrency_group=root_concurrency_group
    )

    assert commit == _release_commit(origin)
    assert commit != _git(origin, "rev-parse", str(_RELEASE_TAG))


def test_resolve_release_tag_commit_reads_a_lightweight_tag(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    origin = _make_tagged_origin(tmp_path)
    _git(origin, "tag", "minds-v9.9.9", "HEAD~1")

    commit = resolve_release_tag_commit(
        repo_url=_url(origin), release_tag="minds-v9.9.9", concurrency_group=root_concurrency_group
    )

    assert commit == _git(origin, "rev-parse", "HEAD~1")


@pytest.mark.parametrize("release_tag", ["minds-v0.0.404", None])
def test_resolve_release_tag_commit_is_none_for_a_missing_tag_or_remote(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, release_tag: str | None
) -> None:
    origin = _make_tagged_origin(tmp_path)
    repo_url = _url(origin) if release_tag is not None else _url(tmp_path / "no-such-remote-81144")

    commit = resolve_release_tag_commit(
        repo_url=repo_url, release_tag=release_tag or str(_RELEASE_TAG), concurrency_group=root_concurrency_group
    )

    assert commit is None
