"""Help topic page model.

A topic help page documents a concept that spans multiple commands (e.g. agent
address syntax or filter syntax) rather than a single CLI command. Plugins
contribute topics via the ``register_help_topics`` hook, which returns
:class:`TopicHelpPage` objects.

This lives in the interfaces layer (alongside the other plugin-facing models
like ``CreateAgentOptions``) so that the plugin hookspec can reference
:class:`TopicHelpPage` without the plugins layer importing upward into the CLI.
The runtime topic registry lives in ``cli/help_topics.py``.

A topic's body comes from exactly one of two sources, both treated as the full
body text to display:

- ``content``: an inline body, supplied directly by the registrant. Rendered
  verbatim (use this for preformatted, terminal-native text).
- ``body_path``: an absolute path to a markdown file, read lazily at display
  time and rendered as markdown. Use this to keep long-form prose in a ``.md``
  file (authorable, browsable on GitHub) instead of inlining it. The registrant
  resolves the absolute path (so this model needs no knowledge of any docs root).

The metadata (key, description, aliases, see-also) is always declared
explicitly -- nothing is inferred by parsing the markdown body.
"""

from pathlib import Path

from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel


class TopicHelpPage(FrozenModel):
    """A standalone help topic page (not associated with any CLI command).

    Topic pages document concepts that span multiple commands, such as
    filter syntax or agent address format.
    """

    key: str = Field(description="Topic identifier (e.g., 'filter')")
    one_line_description: str = Field(description="Brief one-line description (shown in the topic list)")
    aliases: tuple[str, ...] = Field(default=(), description="Topic aliases (e.g., ('addr',) for 'address')")
    see_also: tuple[tuple[str, str], ...] = Field(
        default=(), description="See Also references as (name, description) tuples"
    )
    content: str | None = Field(
        default=None,
        description="Inline body text, rendered verbatim. Provide this OR body_path, not both.",
    )
    body_path: Path | None = Field(
        default=None,
        description="Absolute path to a markdown file providing the body, read lazily and rendered as "
        "markdown. Provide this OR content, not both.",
    )
    docs_path: str | None = Field(
        default=None,
        description="Path to the source doc file relative to the docs root (e.g., 'concepts/idle_detection.md'). "
        "Used by the doc generator to create correct relative links.",
    )

    @model_validator(mode="after")
    def _exactly_one_body_source(self) -> "TopicHelpPage":
        """Require exactly one of content / body_path so the body is unambiguous."""
        if (self.content is None) == (self.body_path is None):
            raise ValueError(
                f"TopicHelpPage {self.key!r} must set exactly one of 'content' or 'body_path' "
                f"(got content={self.content is not None}, body_path={self.body_path is not None})"
            )
        return self

    @property
    def is_markdown_body(self) -> bool:
        """Whether the body should be rendered as markdown (True for body_path-backed topics)."""
        return self.body_path is not None

    def load_body(self) -> str:
        """Return the topic body text.

        For inline ``content`` this is returned verbatim. For ``body_path`` the
        file is read lazily; a missing file yields an empty string (graceful
        degradation, e.g. if a packaging error left the docs out of a wheel).
        """
        if self.content is not None:
            return self.content
        # body_path is guaranteed non-None here by _exactly_one_body_source.
        assert self.body_path is not None
        try:
            return self.body_path.read_text()
        except OSError:
            return ""
