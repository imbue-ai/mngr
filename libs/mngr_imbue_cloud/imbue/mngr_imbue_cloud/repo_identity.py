"""Canonical repository identity for the imbue_cloud fast path.

Both the provider's lease path and the bake tooling derive a repository's
canonical key here so the two sides cannot drift: a request and the host it
adopts agree on "the same repo" iff this function produces the same string for
both. The key is stored verbatim in ``pool_hosts.attributes`` and matched by the
connector's JSONB ``@>`` containment, so identical output on both sides is the
whole correctness contract.
"""

from pathlib import Path
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.errors import RepoIdentityError

_GIT_TIMEOUT_SECONDS: Final[float] = 30.0
# Leading URL schemes we strip before normalizing (lowercased compare).
_URL_SCHEMES: Final[tuple[str, ...]] = ("https://", "http://", "ssh://", "git://", "git+ssh://")
_GIT_SUFFIX: Final[str] = ".git"


@pure
def is_local_repo_source(repo_source: str) -> bool:
    """True iff ``repo_source`` names a local filesystem path rather than a remote git URL.

    Mirrors the desktop client's notion: a value containing ``://`` is a remote
    URL; an scp-style ``git@host:org/repo`` (no ``://``) is also remote; only a
    path beginning with ``/``, ``./``, ``../`` or ``~`` is local.
    """
    text = repo_source.strip()
    if "://" in text:
        return False
    return text.startswith(("/", "./", "../", "~"))


@pure
def normalize_repo_url(raw_url: str) -> str:
    """Normalize a git remote URL to a canonical ``host/org/repo`` key.

    Strips the scheme and any ``user@`` prefix, rewrites scp-style
    ``host:org/repo`` to ``host/org/repo``, drops a trailing ``.git`` / ``/``,
    and lowercases the host (path casing is preserved). Raises
    ``RepoIdentityError`` when the input does not yield both a host and a path.
    """
    text = raw_url.strip()
    if not text:
        raise RepoIdentityError("empty repository URL")

    # Strip a leading scheme (case-insensitive match, original-case strip).
    lowered = text.lower()
    for scheme in _URL_SCHEMES:
        if lowered.startswith(scheme):
            text = text[len(scheme) :]
            break

    # Strip a leading ``user@`` (e.g. ``git@``, or ``user:pass@``) from the
    # authority segment -- the part before the first ``/``.
    authority = text.split("/", 1)[0]
    if "@" in authority:
        text = text[text.index("@") + 1 :]

    # Rewrite scp-style ``host:org/repo`` to ``host/org/repo``. The colon only
    # separates host from path when it appears before the first ``/``.
    head = text.split("/", 1)[0]
    if ":" in head:
        host, _, rest = text.partition(":")
        text = f"{host}/{rest}"

    # Drop trailing slashes and a single trailing ``.git``.
    text = text.rstrip("/")
    if text.endswith(_GIT_SUFFIX):
        text = text[: -len(_GIT_SUFFIX)]
    text = text.rstrip("/")

    # Lowercase only the host (first path segment); org/repo casing is kept.
    host, separator, rest = text.partition("/")
    if not host or not rest:
        raise RepoIdentityError(f"cannot derive a canonical repo identity from {raw_url!r}")
    return f"{host.lower()}{separator}{rest}"


def resolve_repo_origin_url(local_path: Path) -> str:
    """Return the ``origin`` remote URL of the git repo at ``local_path``.

    Raises ``RepoIdentityError`` if the path is not a git repo or has no
    ``origin`` remote (so no canonical identity can be established).
    """
    cg = ConcurrencyGroup(name="repo-identity-origin")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "-C", str(local_path), "remote", "get-url", "origin"],
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    if result.returncode != 0:
        raise RepoIdentityError(
            f"cannot determine the 'origin' remote of local repo {local_path} "
            f"(git exit {result.returncode}): {result.stderr.strip()}"
        )
    origin = result.stdout.strip()
    if not origin:
        raise RepoIdentityError(f"local repo {local_path} has an empty 'origin' remote URL")
    return origin


def resolve_repo_current_branch(local_path: Path) -> str:
    """Return the current branch name of the git repo at ``local_path``.

    Raises ``RepoIdentityError`` on a detached HEAD or a non-repo path.
    """
    cg = ConcurrencyGroup(name="repo-identity-branch")
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "-C", str(local_path), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
        )
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch or branch == "HEAD":
        raise RepoIdentityError(
            f"cannot determine the current branch of local repo {local_path} "
            f"(detached HEAD or not a git repo): {result.stderr.strip()}"
        )
    return branch


def canonicalize_repo_source(repo_source: str) -> str:
    """Canonical repo key for a remote URL or a local path.

    A local path is resolved to its ``origin`` remote first (so a local clone and
    the remote it came from collapse to the same key); a remote URL is normalized
    directly. Raises ``RepoIdentityError`` if no canonical identity exists.
    """
    if is_local_repo_source(repo_source):
        origin_url = resolve_repo_origin_url(Path(repo_source).expanduser())
        return normalize_repo_url(origin_url)
    return normalize_repo_url(repo_source)
