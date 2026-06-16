"""Unit tests for markdown -> ANSI rendering and terminal link rewriting."""

import re

from imbue.mngr.cli.markdown_render import markdown_to_ansi

_BASE = "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/cron_recipes.md"


def _emitted_link_hrefs(ansi: str) -> set[str]:
    """Extract the URLs from the OSC-8 hyperlinks rich emits into the ANSI output."""
    return set(re.findall(r"\x1b\]8;[^;]*;([^\x1b]+)\x1b", ansi))


def test_anchor_link_resolves_against_same_doc() -> None:
    """An anchor-only link resolves to the doc's own URL plus the fragment."""
    hrefs = _emitted_link_hrefs(markdown_to_ansi("[Sec](#user-input-tracking)", 100, link_base=_BASE))
    assert f"{_BASE}#user-input-tracking" in hrefs


def test_sibling_link_resolves_in_same_dir() -> None:
    """A bare relative link resolves against the doc's directory."""
    hrefs = _emitted_link_hrefs(markdown_to_ansi("[Other](other.md)", 100, link_base=_BASE))
    assert "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/other.md" in hrefs


def test_parent_link_resolves_upward() -> None:
    """A '../' link resolves up out of the doc's directory."""
    hrefs = _emitted_link_hrefs(markdown_to_ansi("[Readme](../README.md#x)", 100, link_base=_BASE))
    assert "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/README.md#x" in hrefs


def test_absolute_and_mailto_links_pass_through() -> None:
    """Already-absolute links (https, mailto) are left unchanged."""
    md = "[Site](https://example.com/y) and [Mail](mailto:a@b.com)."
    hrefs = _emitted_link_hrefs(markdown_to_ansi(md, 100, link_base=_BASE))
    assert "https://example.com/y" in hrefs
    assert "mailto:a@b.com" in hrefs


def test_reference_style_link_is_resolved() -> None:
    """A reference-style link (which a naive regex cannot see) is resolved correctly.

    The parser resolves ``[ref][r]`` to its definition's target before we rewrite,
    so the relative reference target becomes an absolute URL.
    """
    md = "Use [ref][r].\n\n[r]: ref_target.md#frag\n"
    hrefs = _emitted_link_hrefs(markdown_to_ansi(md, 100, link_base=_BASE))
    assert "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/ref_target.md#frag" in hrefs


def test_titled_link_is_resolved() -> None:
    """A link with a title attribute resolves its href (the title is not part of the href)."""
    hrefs = _emitted_link_hrefs(markdown_to_ansi('[X](rel.md "a title")', 100, link_base=_BASE))
    assert "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/rel.md" in hrefs


def test_link_inside_code_span_is_not_rewritten() -> None:
    """A ``](...)`` sequence inside a code span is literal text, not a link, so it is untouched.

    This is the case a regex-based rewriter gets wrong; the parser does not.
    """
    output = markdown_to_ansi("Literal `[notalink](nope.md)` here.", 100, link_base=_BASE)
    assert "[notalink](nope.md)" in output
    assert "github.com" not in output


def test_no_link_base_leaves_links_alone() -> None:
    """Without a link_base, relative links are rendered as-is (no rewriting)."""
    hrefs = _emitted_link_hrefs(markdown_to_ansi("[Other](other.md)", 100))
    assert hrefs == {"other.md"}


def _visible_lines(ansi: str) -> list[str]:
    """Strip ANSI escape sequences, returning the visible text of each non-blank line."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", re.sub(r"\x1b\]8;[^\x1b]*\x1b\\", "", ansi))
    return [line for line in plain.splitlines() if line.strip()]


def test_indent_left_pads_every_line() -> None:
    """A positive indent left-pads every rendered line by that many spaces."""
    output = markdown_to_ansi("First paragraph.\n\nSecond paragraph.", 70, indent=7)
    lines = _visible_lines(output)
    assert lines, "expected rendered content"
    assert all(line.startswith("       ") for line in lines)
    assert "First paragraph." in output
    assert "Second paragraph." in output


def test_no_indent_does_not_pad() -> None:
    """The default indent of zero leaves text flush against the left margin."""
    output = markdown_to_ansi("Flush left.", 70)
    assert _visible_lines(output)[0].startswith("Flush left.")
