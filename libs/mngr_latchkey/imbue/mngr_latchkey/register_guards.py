"""Resource guard registration for the node CLI.

Discovered via the resource_guards entry point group declared in
mngr_latchkey's pyproject.toml. Latchkey ships the ``.mjs`` gateway
extensions and runs them under node, so it owns the node guard.
"""

from imbue.resource_guards.resource_guards import register_resource_guard


def register_node_guard() -> None:
    """Register the node CLI binary guard."""
    register_resource_guard("node")
