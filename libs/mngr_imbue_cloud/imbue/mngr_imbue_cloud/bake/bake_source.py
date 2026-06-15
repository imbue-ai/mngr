"""Resolve a pool bake's content source and its truthful identity attributes.

A bake takes exactly one of two source selectors and *derives* the identity it
stamps -- operators never hand-type ``repo_url`` / ``repo_branch_or_tag``, which
is the class of mistake (label diverging from content) the fast-path-matching
spec removes:

- ``--from-tag`` (production): clone the canonical repo at an exact tag into a
  fresh temp dir and bake from that, so the content provably equals the tag.
- ``--workspace-dir`` (dev): bake from a working tree (uncommitted changes
  included); the branch label is best-effort.

Both modes stamp the *canonical* ``repo_url`` (so a request and the host it
adopts agree on "the same repo") plus the ``repo_branch_or_tag`` derived from
the source.
"""

import shutil
import tempfile
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.repo_identity import canonicalize_repo_source
from imbue.mngr_imbue_cloud.repo_identity import resolve_repo_current_branch

# The canonical FCT remote, the default repo a production ``--from-tag`` bake clones.
DEFAULT_FCT_REPO_URL: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
# Identity keys the bake always derives; operators must not hand-pass them in --attributes.
_IDENTITY_ATTRIBUTE_KEYS: Final[frozenset[str]] = frozenset({"repo_url", "repo_branch_or_tag"})
_GIT_TIMEOUT_SECONDS: Final[float] = 300.0


class BakeSourceError(ImbueCloudError, ValueError):
    """Raised when the bake source selectors or derived identity are invalid."""


class BakeSource(FrozenModel):
    """A resolved bake source: where to bake from and the identity to stamp."""

    workspace_dir: Path = Field(description="Directory the bake reads content from")
    repo_url: str = Field(description="Canonical repo identity to stamp into the pool row")
    repo_branch_or_tag: str = Field(description="Git ref label to stamp into the pool row")


def merge_bake_identity_attributes(operator_attributes: Mapping[str, Any], bake_source: BakeSource) -> dict[str, Any]:
    """Merge derived identity onto the operator's non-identity attributes.

    Rejects ``repo_url`` / ``repo_branch_or_tag`` in ``operator_attributes`` --
    they are always derived from the source, never hand-passed (passing them
    would let the label drift from the content).
    """
    conflicting = sorted(_IDENTITY_ATTRIBUTE_KEYS.intersection(operator_attributes))
    if conflicting:
        raise BakeSourceError(
            f"--attributes must not contain identity keys {conflicting}; they are derived from the bake "
            "source (--from-tag or --workspace-dir). Remove them from --attributes."
        )
    return {
        **operator_attributes,
        "repo_url": bake_source.repo_url,
        "repo_branch_or_tag": bake_source.repo_branch_or_tag,
    }


def _verify_remote_has_tag(repo_url: str, tag: str) -> None:
    """Raise BakeSourceError unless ``tag`` is a real tag on ``repo_url``."""
    cg = ConcurrencyGroup(name="bake-source-verify-tag")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "ls-remote", "--tags", repo_url, f"refs/tags/{tag}"],
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise BakeSourceError(
            f"could not list tags on {repo_url} (git exit {result.returncode}): {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        raise BakeSourceError(
            f"{tag!r} is not a tag on {repo_url}; production (--from-tag) bakes require a real tag "
            "(use --workspace-dir for a branch/working-tree dev bake)"
        )


def _clone_repo_at_tag(repo_url: str, tag: str, dest_dir: Path) -> None:
    """Shallow-clone ``repo_url`` at exactly ``tag`` into ``dest_dir``."""
    cg = ConcurrencyGroup(name="bake-source-clone-tag")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "clone", "--depth", "1", "--branch", tag, repo_url, str(dest_dir)],
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise BakeSourceError(
            f"failed to clone {repo_url} at tag {tag} (git exit {result.returncode}): {result.stderr.strip()}. "
            "Check the tag exists and your git credentials can reach the repo."
        )


@contextmanager
def resolved_bake_source(
    *,
    from_tag: str | None,
    workspace_dir: str | None,
    repo_url: str,
    repo_branch_or_tag_override: str | None,
) -> Iterator[BakeSource]:
    """Yield the resolved :class:`BakeSource` for a bake; clean up any temp clone on exit.

    Exactly one of ``from_tag`` / ``workspace_dir`` must be set (passing both, or
    neither, is an error). ``repo_url`` is the canonical repo for ``--from-tag``
    (it is ignored for ``--workspace-dir``, which derives the repo from the
    folder's ``origin``). ``repo_branch_or_tag_override`` only applies to
    ``--workspace-dir`` (it overrides the folder's current branch).
    """
    if (from_tag is None) == (workspace_dir is None):
        raise BakeSourceError(
            "exactly one of --from-tag (production, clones a tag) or --workspace-dir (dev, a working tree) is required"
        )
    if from_tag is not None:
        _verify_remote_has_tag(repo_url, from_tag)
        temp_dir = Path(tempfile.mkdtemp(prefix="mngr-bake-tag-"))
        try:
            _clone_repo_at_tag(repo_url, from_tag, temp_dir)
            yield BakeSource(
                workspace_dir=temp_dir,
                repo_url=canonicalize_repo_source(repo_url),
                repo_branch_or_tag=from_tag,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        # workspace_dir is non-None here (narrowed by the xor check above).
        assert workspace_dir is not None
        expanded_dir = Path(workspace_dir).expanduser()
        if not expanded_dir.is_dir():
            raise BakeSourceError(f"--workspace-dir {expanded_dir} is not a directory")
        # Resolve to an absolute path so canonicalization treats it as a local
        # repo (the local-vs-URL heuristic keys on a leading / ./ ../ ~, which a
        # bare relative path like ``foo/bar`` would otherwise fail).
        resolved_dir = expanded_dir.resolve()
        canonical_repo_url = canonicalize_repo_source(str(resolved_dir))
        branch = repo_branch_or_tag_override or resolve_repo_current_branch(resolved_dir)
        yield BakeSource(
            workspace_dir=resolved_dir,
            repo_url=canonical_repo_url,
            repo_branch_or_tag=branch,
        )
