"""Unit tests for canonical doc-URL building."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version

from imbue.mngr.cli.doc_links import imbue_mngr_doc_url


def _expected_ref() -> str:
    """The exact ref imbue_mngr_doc_url pins to in this environment.

    Mirrors doc_links._imbue_mngr_release_ref: the installed release tag
    ("v" + distribution version) when imbue-mngr is installed, else "main".
    """
    try:
        return f"v{version('imbue-mngr')}"
    except PackageNotFoundError:
        return "main"


def test_imbue_mngr_doc_url_builds_pinned_blob_url() -> None:
    """imbue_mngr_doc_url builds an imbue-ai/mngr blob URL pinned to the exact release ref."""
    url = imbue_mngr_doc_url("libs/mngr/docs/concepts/idle_detection.md")
    expected = f"https://github.com/imbue-ai/mngr/blob/{_expected_ref()}/libs/mngr/docs/concepts/idle_detection.md"
    assert url == expected
