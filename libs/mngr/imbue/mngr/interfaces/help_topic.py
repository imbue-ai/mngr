"""Help topic page model and the helper for building topics from markdown files.

A topic help page documents a concept that spans multiple commands (e.g. agent
address syntax or filter syntax) rather than a single CLI command. Plugins
contribute topics via the ``register_help_topics`` hook, which returns
:class:`TopicHelpPage` objects.

This lives in the interfaces layer (alongside the other plugin-facing models
like ``CreateAgentOptions``) so that the plugin hookspec can reference
:class:`TopicHelpPage` without the plugins layer importing upward into the CLI.
The runtime topic registry lives in ``cli/help_topics.py``.
"""

import re
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class TopicHelpPage(FrozenModel):
    """A standalone help topic page (not associated with any CLI command).

    Topic pages document concepts that span multiple commands, such as
    filter syntax or agent address format.
    """

    key: str = Field(description="Topic identifier (e.g., 'filter')")
    one_line_description: str = Field(description="Brief one-line description")
    content: str = Field(description="Full content of the topic page")
    aliases: tuple[str, ...] = Field(default=(), description="Topic aliases (e.g., ('addr',) for 'address')")
    see_also: tuple[tuple[str, str], ...] = Field(
        default=(), description="See Also references as (name, description) tuples"
    )
    docs_path: str | None = Field(
        default=None,
        description="Path to the source doc file relative to the docs root (e.g., 'concepts/idle_detection.md'). "
        "Used by the doc generator to create correct relative links.",
    )


def _extract_first_heading(content: str) -> str:
    """Extract the text of the first markdown heading from content."""
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            # Remove [future] tags
            title = re.sub(r"\s*\[future\]", "", title)
            return title
    return ""


def _strip_first_heading(content: str) -> str:
    """Strip the first markdown heading and any trailing blank lines after it."""
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            remaining = lines[i + 1 :]
            while remaining and not remaining[0].strip():
                remaining.pop(0)
            return "\n".join(remaining)
    return content


def build_topics_from_directory(path_prefix: str, directory: Path) -> tuple[TopicHelpPage, ...]:
    """Build (unregistered) topic pages from every ``.md`` file in a directory.

    For each markdown file, the topic key is the filename stem (e.g. ``filter``
    from ``filter.md``), the one-line description is the file's first markdown
    heading, and the content is everything after that heading.

    Returns an empty tuple if the directory does not exist. The returned pages
    are not registered; callers (mngr's built-in scan, or a plugin's
    ``register_help_topics`` hook) decide what to do with them.

    Plugins that just want to expose a directory of markdown files as help
    topics can implement the hook as::

        @hookimpl
        def register_help_topics():
            return build_topics_from_directory("my_plugin", Path(__file__).parent / "docs")
    """
    if not directory.exists():
        return ()
    pages: list[TopicHelpPage] = []
    for md_file in sorted(directory.glob("*.md")):
        raw_content = md_file.read_text()
        pages.append(
            TopicHelpPage(
                key=md_file.stem,
                one_line_description=_extract_first_heading(raw_content),
                content=_strip_first_heading(raw_content),
                docs_path=f"{path_prefix}/{md_file.name}",
            )
        )
    return tuple(pages)
