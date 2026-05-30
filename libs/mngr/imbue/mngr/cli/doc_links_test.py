"""Unit tests for canonical doc-URL building."""

from imbue.mngr.cli.doc_links import imbue_mngr_doc_url


def test_imbue_mngr_doc_url_builds_pinned_blob_url() -> None:
    """imbue_mngr_doc_url builds an imbue-ai/mngr blob URL ending in the repo-relative path."""
    url = imbue_mngr_doc_url("libs/mngr/docs/concepts/idle_detection.md")
    assert url.startswith("https://github.com/imbue-ai/mngr/blob/")
    assert url.endswith("/libs/mngr/docs/concepts/idle_detection.md")
    # The ref is either the installed release tag (vX.Y.Z) or the "main" fallback.
    ref = url.removeprefix("https://github.com/imbue-ai/mngr/blob/").removesuffix(
        "/libs/mngr/docs/concepts/idle_detection.md"
    )
    assert ref == "main" or ref.startswith("v")
