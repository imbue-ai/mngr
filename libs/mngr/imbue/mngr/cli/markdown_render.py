"""Markdown -> ANSI rendering via rich.

This module top-imports rich, which is moderately heavy. It is imported lazily
(see ``render_markdown`` in ``help_formatter.py``) so rich stays out of the CLI
startup path -- it is only loaded when help is actually being displayed to an
interactive terminal.
"""

from io import StringIO

from rich.console import Console
from rich.markdown import Markdown


def markdown_to_ansi(markdown: str, width: int) -> str:
    """Render a markdown string to an ANSI-formatted string at the given width."""
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=width, color_system="standard")
    console.print(Markdown(markdown))
    return buffer.getvalue()
