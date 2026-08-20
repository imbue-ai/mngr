"""Acceptance tests: the GitHub doc URLs that imbue_mngr_doc_url builds actually resolve.

These hit github.com over the network (so they're acceptance-marked -- kept out
of the fast unit suite, but run in CI on every PR via offload, since a broken ref
policy is important to catch early). They guard the version-pinning ref policy in
doc_links against silent breakage -- a wrong tag format, or the repo being
renamed/made private -- which would turn every terminal help-topic link into a
dead 404.
"""

import subprocess

import pytest

from imbue.mngr.cli.doc_links import _IMBUE_MNGR_REPO_URL
from imbue.mngr.cli.doc_links import imbue_mngr_doc_url

# A long-standing doc present on main and in tagged releases. (Docs added on an
# unmerged branch can 404 at an older release tag -- the documented
# version-pinning caveat -- so we deliberately pick a stable one.)
_STABLE_DOC = "libs/mngr/docs/concepts/idle_detection.md"


def _http_status(url: str) -> int:
    """Return the HTTP status code curl gets for ``url`` (following redirects)."""
    result = subprocess.run(
        ["curl", "-sSL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "30", url],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


@pytest.mark.acceptance
def test_release_pinned_doc_url_resolves() -> None:
    """The version-pinned URL imbue_mngr_doc_url builds resolves (no 404)."""
    url = imbue_mngr_doc_url(_STABLE_DOC)
    assert _http_status(url) == 200, f"version-pinned doc URL 404s: {url}"


@pytest.mark.acceptance
def test_main_pinned_doc_url_resolves() -> None:
    """The same doc resolves on main (the ref policy's fallback)."""
    url = f"{_IMBUE_MNGR_REPO_URL}/blob/main/{_STABLE_DOC}"
    assert _http_status(url) == 200, f"main doc URL 404s: {url}"


@pytest.mark.acceptance
def test_missing_doc_url_404s() -> None:
    """Negative control: a nonexistent doc 404s, so the 200 checks above aren't vacuous."""
    url = f"{_IMBUE_MNGR_REPO_URL}/blob/main/libs/mngr/docs/concepts/this_doc_does_not_exist_zzz.md"
    assert _http_status(url) == 404
