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
from imbue.mngr.interfaces.help_topic import TopicHelpPage

# Docs root resolution. In a source/editable checkout the docs live at
# libs/mngr/docs (three parents up from this file). In a wheel they are
# force-included under the package at imbue/mngr/docs (the top-level docs/ tree
# is not otherwise shipped -- see CLAUDE.md). Prefer the packaged copy, falling
# back to the source tree.
#   this file: .../imbue/mngr/cli/builtin_help_topics.py
# _PACKAGED_DOCS_ROOT is imbue/mngr/docs (force-included in the wheel);
# _SOURCE_DOCS_ROOT is libs/mngr/docs (the source/editable checkout).
_PACKAGED_DOCS_ROOT = Path(__file__).resolve().parents[1] / "docs"
_SOURCE_DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"
_DOCS_ROOT = _PACKAGED_DOCS_ROOT if _PACKAGED_DOCS_ROOT.is_dir() else _SOURCE_DOCS_ROOT


def _doc_topic(key: str, one_line_description: str, rel_path: str) -> TopicHelpPage:
    """Build a topic whose body is the markdown file at ``rel_path`` (docs-root-relative).

    The metadata is declared explicitly; the body is the whole file, rendered as
    markdown at display time -- nothing is inferred by parsing the file.
    """
    return TopicHelpPage(
        key=key,
        one_line_description=one_line_description,
        docs_path=rel_path,
        body_path=_DOCS_ROOT / rel_path,
    )


# mngr's built-in topics, each backed by a markdown doc file under docs/.
# Keep this list in sync with the topic docs; adding a doc here is what makes it
# show up in `mngr help`.
_DOC_TOPICS: tuple[TopicHelpPage, ...] = (
    _doc_topic("common", "Common Options", "commands/generic/common.md"),
    _doc_topic("multi_target", "Commands that target from multiple hosts/agents", "commands/generic/multi_target.md"),
    _doc_topic("resource_cleanup", "Resource Cleanup", "commands/generic/resource_cleanup.md"),
    _doc_topic("agent_types", "Agent Types", "concepts/agent_types.md"),
    _doc_topic("agents", "Agents", "concepts/agents.md"),
    _doc_topic("api", "mngr Plugin API", "concepts/api.md"),
    _doc_topic("docker_usage", "Using Docker", "concepts/docker_usage.md"),
    _doc_topic("environment_variables", "Environment Variables", "concepts/environment_variables.md"),
    _doc_topic("hosts", "Hosts", "concepts/hosts.md"),
    _doc_topic("idle_detection", "Idle Detection", "concepts/idle_detection.md"),
    _doc_topic("modal_usage", "Using Modal", "concepts/modal_usage.md"),
    _doc_topic("plugins", "Plugins", "concepts/plugins.md"),
    _doc_topic("provider_backends", "Provider Backends", "concepts/provider_backends.md"),
    _doc_topic("providers", "Provider Instances", "concepts/providers.md"),
    _doc_topic("provisioning", "Provisioning", "concepts/provisioning.md"),
    _doc_topic("snapshot", "Snapshots", "concepts/snapshot.md"),
)

# The address topic is hand-authored inline (not backed by a docs file) so that
# it can carry aliases and "See Also" references; its content is preformatted
# terminal text, shown verbatim in man-page format.
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
    """Register mngr's built-in topic pages (the hand-authored address topic plus
    the doc-backed topics declared in _DOC_TOPICS)."""
    return (_ADDRESS_TOPIC, *_DOC_TOPICS)
