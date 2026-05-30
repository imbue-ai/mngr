"""Unit tests for doc-link utilities (URL building and link rewriting)."""

from imbue.mngr.cli.doc_links import imbue_mngr_doc_url
from imbue.mngr.cli.doc_links import rewrite_links_to_absolute

_DOC_URL = "https://github.com/imbue-ai/mngr/blob/v1.2.3/libs/mngr_usage/docs/cron_recipes.md"


def test_rewrite_links_anchor_resolves_against_same_file() -> None:
    """An anchor-only target resolves to the doc's own URL plus the fragment."""
    result = rewrite_links_to_absolute("[Sec](#user-input-tracking)", _DOC_URL)
    assert result == f"[Sec]({_DOC_URL}#user-input-tracking)"


def test_rewrite_links_sibling_resolves_in_same_dir() -> None:
    """A bare relative target resolves against the doc's directory."""
    result = rewrite_links_to_absolute("[Other](other.md)", _DOC_URL)
    assert result == "[Other](https://github.com/imbue-ai/mngr/blob/v1.2.3/libs/mngr_usage/docs/other.md)"


def test_rewrite_links_parent_dir_resolves_upward() -> None:
    """A '../' target resolves up out of the doc's directory."""
    result = rewrite_links_to_absolute("[Readme](../README.md#x)", _DOC_URL)
    assert result == "[Readme](https://github.com/imbue-ai/mngr/blob/v1.2.3/libs/mngr_usage/README.md#x)"


def test_rewrite_links_absolute_url_unchanged() -> None:
    """An already-absolute https link is left untouched."""
    text = "[Site](https://example.com/page)"
    assert rewrite_links_to_absolute(text, _DOC_URL) == text


def test_rewrite_links_mailto_unchanged() -> None:
    """A mailto: link (has a scheme) is left untouched."""
    text = "[Mail](mailto:a@b.com)"
    assert rewrite_links_to_absolute(text, _DOC_URL) == text


def test_rewrite_links_leaves_non_link_text() -> None:
    """Text with no markdown links is returned unchanged."""
    text = "Plain text with (parentheses) but no links."
    assert rewrite_links_to_absolute(text, _DOC_URL) == text


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
