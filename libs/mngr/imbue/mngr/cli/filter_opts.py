"""Shared agent filter CLI options.

Commands that operate on a filtered set of agents (`mngr list`, `mngr kanpan`, ...)
share three pieces:

1. ``AgentFilterCliOptions`` -- a ``FrozenModel`` mixin holding the parsed flag
   values. Mix it into a command's options class alongside ``CommonCliOptions``.
2. ``add_agent_filter_options`` -- a click decorator that adds the matching
   ``--include/--exclude`` flags plus alias flags (``--running``, ``--stopped``,
   etc.) under a "Filtering" option group.
3. ``build_agent_filter_cel`` -- translates an ``AgentFilterCliOptions`` instance
   into a ``(include_filters, exclude_filters)`` pair of CEL string tuples,
   suitable for passing directly to ``list_agents()`` /
   ``fetch_board_snapshot()`` and any other API that accepts CEL filters.

To add a new filter flag, edit all three pieces in this module and every command
using ``add_agent_filter_options`` inherits the new flag with no per-command
glue.
"""

from collections.abc import Callable
from typing import Any
from typing import TypeVar

import click
from click_option_group import optgroup

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import HostState

TDecorated = TypeVar("TDecorated", bound=Callable[..., Any])

FILTER_OPTIONS_GROUP_NAME = "Filtering"


class AgentFilterCliOptions(FrozenModel):
    """Filter options shared by commands operating on a filtered set of agents.

    Field names and types intentionally mirror the ``add_agent_filter_options``
    decorator so ``command_class(**click_kwargs)`` works without per-command glue.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    running: bool = False
    stopped: bool = False
    archived: bool = False
    active: bool = False
    local: bool = False
    remote: bool = False
    project: tuple[str, ...] = ()
    label: tuple[str, ...] = ()
    host_label: tuple[str, ...] = ()


def add_agent_filter_options(command: TDecorated) -> TDecorated:
    """Add the shared agent filter flags under a "Filtering" option group."""
    # Decorators apply bottom-up, so the visible help order matches reverse order here.
    command = optgroup.option(
        "--host-label",
        multiple=True,
        help="Show only agents on hosts with this host label (format: KEY=VALUE, repeatable)",
    )(command)
    command = optgroup.option(
        "--label",
        multiple=True,
        help="Show only agents with this label (format: KEY=VALUE, repeatable) [experimental]",
    )(command)
    command = optgroup.option(
        "--project",
        multiple=True,
        help="Show only agents with this project label (repeatable)",
    )(command)
    command = optgroup.option(
        "--remote",
        is_flag=True,
        help="Show only remote agents (alias for --exclude 'host.provider == \"local\"')",
    )(command)
    command = optgroup.option(
        "--local",
        is_flag=True,
        help="Show only local agents (alias for --include 'host.provider == \"local\"')",
    )(command)
    command = optgroup.option(
        "--active",
        is_flag=True,
        help="Show only active agents (anything not archived/destroyed/crashed/failed)",
    )(command)
    command = optgroup.option(
        "--archived",
        is_flag=True,
        help="Show only archived agents (alias for --include 'has(labels.archived_at)')",
    )(command)
    command = optgroup.option(
        "--stopped",
        is_flag=True,
        help="Show only stopped agents (alias for --include 'state == \"STOPPED\"')",
    )(command)
    command = optgroup.option(
        "--running",
        is_flag=True,
        help="Show only running agents (alias for --include 'state == \"RUNNING\"')",
    )(command)
    command = optgroup.option(
        "--exclude",
        multiple=True,
        help="Exclude agents matching CEL expression (repeatable)",
    )(command)
    command = optgroup.option(
        "--include",
        multiple=True,
        help="Include agents matching CEL expression (repeatable)",
    )(command)
    command = optgroup.group(FILTER_OPTIONS_GROUP_NAME)(command)
    return command


def _key_value_filter(specs: tuple[str, ...], cel_prefix: str, flag_name: str) -> str:
    """Build an OR-joined CEL fragment from KEY=VALUE specs against ``cel_prefix.KEY``."""
    parts: list[str] = []
    for spec in specs:
        if "=" not in spec:
            raise click.BadParameter(f"Label must be in KEY=VALUE format, got: {spec}", param_hint=flag_name)
        key, value = spec.split("=", 1)
        parts.append(f'{cel_prefix}.{key} == "{value}"')
    return " || ".join(parts)


def build_agent_filter_cel(
    opts: AgentFilterCliOptions,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Translate parsed filter flags into ``(include_filters, exclude_filters)`` CEL tuples."""
    include: list[str] = list(opts.include)
    exclude: list[str] = list(opts.exclude)

    if opts.running:
        include.append(f'state == "{AgentLifecycleState.RUNNING.value}"')
    if opts.stopped:
        include.append(f'state == "{AgentLifecycleState.STOPPED.value}"')
    if opts.archived:
        include.append("has(labels.archived_at)")
    if opts.local:
        include.append('host.provider == "local"')
    if opts.remote:
        exclude.append('host.provider == "local"')
    if opts.active:
        exclude.append("has(labels.archived_at)")
        include.append(f'host.state != "{HostState.CRASHED.value}"')
        include.append(f'host.state != "{HostState.FAILED.value}"')
        include.append(f'host.state != "{HostState.DESTROYED.value}"')
    if opts.project:
        include.append(" || ".join(f'labels.project == "{p}"' for p in opts.project))
    if opts.label:
        include.append(_key_value_filter(opts.label, "labels", "--label"))
    if opts.host_label:
        include.append(_key_value_filter(opts.host_label, "host.tags", "--host-label"))

    return tuple(include), tuple(exclude)
