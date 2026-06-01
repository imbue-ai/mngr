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
from imbue.mngr.cli.doc_links import imbue_mngr_doc_url
from imbue.mngr.interfaces.help_topic import DocFile
from imbue.mngr.interfaces.help_topic import TopicHelpPage

# Docs root resolution. In a wheel the docs are force-included under the package
# at imbue/mngr/docs (parents[1]); in a source/editable checkout they live at
# libs/mngr/docs (parents[3]) -- the top-level docs/ tree is not otherwise
# shipped (see CLAUDE.md). Prefer the packaged copy, else the source tree.
_PACKAGED_DOCS_ROOT = Path(__file__).resolve().parents[1] / "docs"
_SOURCE_DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"
_DOCS_ROOT = _PACKAGED_DOCS_ROOT if _PACKAGED_DOCS_ROOT.is_dir() else _SOURCE_DOCS_ROOT


def _doc_topic(
    key: str,
    one_line_description: str,
    rel_path: str,
    *,
    aliases: tuple[str, ...] = (),
    see_also: tuple[tuple[str, str], ...] = (),
) -> TopicHelpPage:
    """Build a topic whose body is the markdown file at ``rel_path`` (docs-root-relative).

    The metadata is declared explicitly; the body is the whole file, rendered as
    markdown at display time -- nothing is inferred by parsing the file.
    """
    return TopicHelpPage(
        key=key,
        one_line_description=one_line_description,
        aliases=aliases,
        see_also=see_also,
        docs_path=rel_path,
        body=DocFile(path=_DOCS_ROOT / rel_path, source_url=imbue_mngr_doc_url(f"libs/mngr/docs/{rel_path}")),
    )


# mngr's built-in topics, each backed by a markdown doc file under docs/.
# Keep this list in sync with the topic docs; adding a doc here is what makes it
# show up in `mngr help`.
_DOC_TOPICS: tuple[TopicHelpPage, ...] = (
    _doc_topic(
        "address",
        "Agent address syntax for targeting agents and hosts",
        "concepts/address.md",
        aliases=("addr",),
        see_also=(
            ("create", "Create and run an agent"),
            ("connect", "Connect to an existing agent"),
        ),
    ),
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


@hookimpl(tryfirst=True)
def register_help_topics() -> Sequence[TopicHelpPage]:
    """Register mngr's built-in topic pages (the doc-backed topics in _DOC_TOPICS)."""
    return _DOC_TOPICS
