"""Canonical doc URLs for help topics.

:func:`imbue_mngr_doc_url` builds the GitHub blob URL for a doc shipped in the
imbue-ai/mngr repo, pinned to the installed release. In-repo topic providers
(mngr's built-ins and the mngr_usage plugin) use it to set ``DocFile.source_url``
so that relative links in those docs can be resolved to working GitHub URLs when
shown in an interactive terminal (the resolution itself happens at render time
in ``cli/markdown_render.py``).

This lives in the cli layer (not the plugin-facing ``interfaces`` layer) because
it encodes mngr's repo identity and does a runtime version lookup -- application
knowledge rather than a data model.
"""

from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version

_IMBUE_MNGR_REPO_URL = "https://github.com/imbue-ai/mngr"


@cache
def _imbue_mngr_release_ref() -> str:
    """The git ref to pin doc links to: the installed mngr release's tag, else ``main``.

    Released wheels ship their docs in lockstep with their version tag, so links
    pinned to that tag resolve to exactly the docs the user has installed. Falls
    back to ``main`` when the distribution version can't be read (e.g. mngr isn't
    installed as a package).

    Assumes the release tag is literally ``v`` + the distribution version (the
    repo's convention, e.g. version ``0.2.9`` -> tag ``v0.2.9``); a tag-convention
    change would silently produce 404 links. Caveat: in a source checkout whose
    version predates a not-yet-released doc, that doc's link can 404 until the
    next release -- inherent to version-pinning, harmless for real installs.

    Cached: the version is fixed for the process, and this is hit once per
    built-in topic at registration (on every CLI startup), so we read the
    distribution metadata only once.
    """
    try:
        return f"v{version('imbue-mngr')}"
    except PackageNotFoundError:
        return "main"


def imbue_mngr_doc_url(repo_relative_path: str) -> str:
    """GitHub blob URL for a doc shipped in the imbue-ai/mngr repo, pinned to the installed release.

    ``repo_relative_path`` is the doc's path from the repo root (e.g.
    ``"libs/mngr/docs/concepts/idle_detection.md"``). In-repo topic providers
    (mngr's built-ins and the mngr_usage plugin) use this to populate
    ``DocFile.source_url``, so relative links in those docs render as working
    GitHub URLs when shown in an interactive terminal.
    """
    return f"{_IMBUE_MNGR_REPO_URL}/blob/{_imbue_mngr_release_ref()}/{repo_relative_path}"
