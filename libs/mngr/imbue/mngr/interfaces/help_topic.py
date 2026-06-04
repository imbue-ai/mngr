"""Help topic page model.

A topic help page documents a concept that spans multiple commands (e.g. agent
address syntax or filter syntax) rather than a single CLI command. Plugins
contribute topics via the ``register_help_topics`` hook, which returns
:class:`TopicHelpPage` objects.

This lives in the interfaces layer (alongside the other plugin-facing models
like ``CreateAgentOptions``) so that the plugin hookspec can reference
:class:`TopicHelpPage` without the plugins layer importing upward into the CLI.
The runtime topic registry lives in ``cli/help_topics.py``.

A topic's body is one of two explicitly-typed sources -- :class:`InlineContent`
(markdown text supplied directly) or :class:`DocFile` (a markdown file, read
lazily at display time) -- both rendered as markdown. The marker types make the
intent unambiguous (a bare string path would otherwise look like inline text).
All metadata (key, description, aliases, see-also) is declared explicitly --
nothing is inferred by parsing the body.
"""

from pathlib import Path
from typing import assert_never

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class InlineContent(FrozenModel):
    """A topic body supplied inline as markdown text."""

    markdown: str = Field(description="The markdown body text")


class DocFile(FrozenModel):
    """A topic body backed by a markdown file, read lazily at display time.

    The registrant resolves the absolute path (so the topic model needs no
    knowledge of any docs root). The file is read at display time and must
    exist -- a missing file raises (a packaging bug) rather than degrading to
    an empty body.
    """

    path: Path = Field(description="Absolute path to the markdown file")
    source_url: str | None = Field(
        default=None,
        description="Canonical URL of this file (e.g. its GitHub blob URL). When set, relative and "
        "anchor links in the body are rewritten against it so they are clickable in the terminal.",
    )


class TopicHelpPage(FrozenModel):
    """A standalone help topic page (not associated with any CLI command).

    Topic pages document concepts that span multiple commands, such as
    filter syntax or agent address format.
    """

    key: str = Field(description="Topic identifier (e.g., 'filter')")
    one_line_description: str = Field(description="Brief one-line description (shown in the topic list)")
    body: InlineContent | DocFile = Field(description="The topic body, rendered as markdown")
    aliases: tuple[str, ...] = Field(default=(), description="Topic aliases (e.g., ('addr',) for 'address')")
    see_also: tuple[tuple[str, str], ...] = Field(
        default=(), description="See Also references as (name, description) tuples"
    )
    docs_path: str | None = Field(
        default=None,
        description="Path to the source doc file relative to the docs root (e.g., 'concepts/idle_detection.md'). "
        "Used by the doc generator to create correct relative links.",
    )

    def load_body(self) -> str:
        """Return the topic body text: inline markdown verbatim, or the file's contents.

        A ``DocFile`` is read here (at display time), and its file must exist --
        built-in topic docs are shipped in the wheel and plugins must ship theirs
        -- so a missing file raises (a packaging bug) rather than yielding a
        silently-empty page.
        """
        match self.body:
            case InlineContent(markdown=markdown):
                return markdown
            case DocFile(path=path):
                return path.read_text()
            case _ as unreachable:
                assert_never(unreachable)

    def link_base_url(self) -> str | None:
        """Base URL for resolving relative/anchor links in the body, or None if unknown.

        Only file-backed bodies (:class:`DocFile`) have a canonical source
        location; inline bodies have nowhere to resolve relative links against,
        so they return None (links are left as-is).
        """
        match self.body:
            case DocFile(source_url=source_url):
                return source_url
            case InlineContent():
                return None
            case _ as unreachable:
                assert_never(unreachable)
