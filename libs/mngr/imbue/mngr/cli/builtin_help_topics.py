"""Built-in help topics, contributed through the same hook plugins use.

mngr's own topic pages are registered exactly like external plugins register
theirs: this module implements ``register_help_topics`` and is registered with
the plugin manager as a built-in plugin (see ``create_plugin_manager`` in
``main.py``), mirroring how built-in provider backends and agent types register
through ``register_provider_backend`` / ``register_agent_type``.

The hook is marked ``tryfirst`` so built-in topics are registered before
normally-registered external plugins' topics. Precedence on collisions is
actually enforced by ``register_topic``, which skips any topic whose key or
alias is already taken: because built-in topics register first, a plugin
cannot override them.
"""

from collections.abc import Sequence
from pathlib import Path

from imbue.mngr import hookimpl
from imbue.mngr.cli.help_topics import TopicHelpPage
from imbue.mngr.cli.help_topics import build_topics_from_directory

# Docs root relative to this file:
# this file: libs/mngr/imbue/mngr/cli/builtin_help_topics.py
# docs root: libs/mngr/docs/
_DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"

# Directories whose markdown files become topic pages, mapped to their path
# prefix relative to the docs root (used by the doc generator for link paths).
_TOPIC_DOC_DIRECTORIES: tuple[tuple[str, Path], ...] = (
    ("commands/generic", _DOCS_ROOT / "commands" / "generic"),
    ("concepts", _DOCS_ROOT / "concepts"),
)

# The address topic is hand-authored (not backed by a docs file) so that it can
# carry aliases and "See Also" references.
_ADDRESS_TOPIC = TopicHelpPage(
    key="address",
    one_line_description="Agent address syntax for targeting agents and hosts",
    aliases=("addr",),
    content="""\
Many mngr commands accept an agent address to specify which agent (and
optionally which host and provider) to target. The address format is:

  [NAME][@[HOST][.PROVIDER]]

All parts are optional:

  NAME                  Agent name only (searches all hosts; local in create)
  NAME@HOST             Agent on a specific existing host
  NAME@HOST.PROVIDER    Agent on a specific host with provider disambiguation
  NAME@.PROVIDER        Agent on a new host (auto-generated host name)
  @HOST                 Auto-named agent on an existing host
  @HOST.PROVIDER        Auto-named agent on an existing host with provider
  @.PROVIDER            Auto-named agent on a new auto-named host

COMPONENTS

  NAME
      The agent name. Must be a valid identifier (lowercase letters, digits,
      and hyphens). If omitted, a name is auto-generated. Without a host
      component, commands that target existing agents search across all
      hosts and providers. In 'mngr create', it defaults to the local host.

  HOST
      The host name. Refers to an existing host unless --new-host is specified.
      If omitted with a provider (e.g., @.modal), a new host with an
      auto-generated name is created.

  PROVIDER
      The provider backend name (e.g., local, docker, modal). Used to
      disambiguate when multiple providers have hosts with the same name,
      or to specify which provider should create a new host.

COMMANDS THAT ACCEPT ADDRESSES

  mngr create   Primary address argument for creating agents
  mngr connect  Agent identifier (supports @HOST.PROVIDER disambiguation)
  mngr destroy  Agent identifier(s)
  mngr exec     Agent identifier(s)
  mngr start    Agent identifier(s)
  mngr stop     Agent identifier(s)
  mngr list     --addrs flag outputs addresses for listed agents

EXAMPLES

  Create an agent locally:
      $ mngr create my-agent

  Create an agent in a new Docker container:
      $ mngr create my-agent@.docker

  Create an agent on an existing Modal host:
      $ mngr create my-agent@my-host.modal

  Create a new named host on Modal:
      $ mngr create my-agent@my-host.modal --new-host

  Connect to an agent, disambiguating by provider:
      $ mngr connect my-agent@my-host.docker

  Destroy an agent on a specific host:
      $ mngr destroy my-agent@my-host\
""",
    see_also=(
        ("create", "Create and run an agent"),
        ("connect", "Connect to an existing agent"),
    ),
)


@hookimpl(tryfirst=True)
def register_help_topics() -> Sequence[TopicHelpPage]:
    """Register mngr's built-in topic pages (the hand-authored address topic and
    every markdown file in the generic/ and concepts/ docs directories)."""
    topics: list[TopicHelpPage] = [_ADDRESS_TOPIC]
    for path_prefix, directory in _TOPIC_DOC_DIRECTORIES:
        topics.extend(build_topics_from_directory(path_prefix, directory))
    return topics
