"""Markdown -> ANSI rendering via rich.

This module top-imports rich (and markdown-it-py, rich's parser), which are
moderately heavy. It is imported lazily (see ``render_markdown`` in
``help_formatter.py``) so they stay out of the CLI startup path -- they are only
loaded when help is actually being displayed to an interactive terminal.
"""

from io import StringIO
from urllib.parse import urljoin

from markdown_it.token import Token
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding


def _rewrite_link_hrefs(tokens: list[Token], base_url: str) -> None:
    """Resolve every link's href against ``base_url`` in place, recursing into children.

    Operates on rich's own parsed markdown-it token tree (the same tree rich then
    renders), so link detection is exactly CommonMark -- reference-style links,
    titled links, and ``](...)`` inside code spans are all handled correctly,
    with no regex. Each href is resolved via ``urljoin``: ``#anchor`` ->
    ``base#anchor``, ``sibling.md`` -> the sibling, ``../x.md`` -> the parent;
    already-absolute targets (``https:``, ``mailto:``) are returned unchanged.
    """
    for token in tokens:
        if token.type == "link_open":
            href = str(token.attrs.get("href", ""))
            token.attrs["href"] = urljoin(base_url, href)
        if token.children:
            _rewrite_link_hrefs(token.children, base_url)


def markdown_to_ansi(markdown: str, width: int, link_base: str | None = None, indent: int = 0) -> str:
    """Render a markdown string to an ANSI-formatted string at the given width.

    When ``link_base`` is given, relative and anchor links are rewritten to
    absolute URLs (resolved against it) before rendering, so the terminal
    hyperlinks rich emits are clickable rather than dead relative targets.

    When ``indent`` is greater than zero, every rendered line is left-padded by
    that many spaces. The content is wrapped to ``width`` minus the indent so the
    padded text still fits, matching the man-page-style indentation used for the
    DESCRIPTION and other prose sections of command help.
    """
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=width, color_system="standard")
    rendered = Markdown(markdown)
    if link_base is not None:
        _rewrite_link_hrefs(rendered.parsed, link_base)
    console.print(Padding(rendered, (0, 0, 0, indent)) if indent else rendered)
    return buffer.getvalue()
