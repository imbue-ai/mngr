"""Doc-link utilities for help topics.

Two related concerns, both kept out of the plugin-facing ``interfaces`` layer
(which only declares the :attr:`DocFile.source_url` field these populate):

- :func:`imbue_mngr_doc_url` builds the canonical GitHub blob URL for a doc
  shipped in the imbue-ai/mngr repo, pinned to the installed release. In-repo
  topic providers (mngr's built-ins and the mngr_usage plugin) use it to set
  ``DocFile.source_url``. It encodes mngr's repo identity and does a runtime
  version lookup, which is application/CLI knowledge rather than a data model.

- :func:`rewrite_links_to_absolute` rewrites relative and anchor markdown links
  to absolute URLs (against a doc's ``source_url``) at terminal render time, so
  the hyperlinks rich emits are clickable instead of dead relative targets.
"""

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version
from urllib.parse import urljoin

_IMBUE_MNGR_REPO_URL = "https://github.com/imbue-ai/mngr"


def _imbue_mngr_release_ref() -> str:
    """The git ref to pin doc links to: the installed mngr release's tag, else ``main``.

    Released wheels ship their docs in lockstep with their version tag (e.g.
    ``v0.2.9``), so links pinned to that tag resolve to exactly the docs the user
    has installed. Falls back to ``main`` when the distribution version can't be
    read (e.g. mngr isn't installed as a package). Caveat: in a source checkout
    whose version predates a not-yet-released doc, that doc's link can 404 until
    the next release -- inherent to version-pinning, and harmless for real
    installs (where version and shipped docs always match).
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


# Matches a markdown inline-link target: the "(...)" after a "]". The captured
# group is the link target. Parallels the relative-link rewriting that
# scripts/make_cli_docs.py applies to the PyPI README, but each target is
# resolved against the doc's own URL (so sibling/parent/anchor links work too).
_MARKDOWN_LINK_TARGET_RE = re.compile(r"\]\(([^)]+)\)")

# Matches a leading URL scheme (https:, mailto:, etc.) -- such targets are
# already absolute and are left untouched.
_ABSOLUTE_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _rewrite_link_match(base_url: str, match: re.Match[str]) -> str:
    """Resolve a single matched markdown link target against ``base_url``.

    A target that is already absolute (has a URL scheme like ``https:`` or
    ``mailto:``) is returned unchanged; anything else (relative path or bare
    ``#anchor``) is resolved against ``base_url`` via ``urljoin``.
    """
    target = match.group(1)
    if _ABSOLUTE_URL_RE.match(target):
        return match.group(0)
    return f"]({urljoin(base_url, target)})"


def rewrite_links_to_absolute(markdown: str, base_url: str) -> str:
    """Rewrite relative and anchor markdown link targets to absolute URLs.

    Each target is resolved against ``base_url`` (the doc's own canonical URL):
    ``#anchor`` -> ``base#anchor``, ``sibling.md`` -> the sibling's URL,
    ``../x.md`` -> the parent's URL. Already-absolute targets are left unchanged.
    This makes links clickable when rendered as terminal hyperlinks.

    Uses an explicit ``finditer`` splice (rather than ``re.sub`` with a callback)
    so the per-match logic stays a module-level function -- no nested closure.
    """
    pieces: list[str] = []
    last_end = 0
    for match in _MARKDOWN_LINK_TARGET_RE.finditer(markdown):
        pieces.append(markdown[last_end : match.start()])
        pieces.append(_rewrite_link_match(base_url, match))
        last_end = match.end()
    pieces.append(markdown[last_end:])
    return "".join(pieces)
