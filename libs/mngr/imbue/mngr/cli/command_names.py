"""Authoritative registry of the ``command_name`` values used across the CLI.

``command_name`` is the identifier each command passes to
``setup_command_context`` / ``handle_common_options_and_run`` (``cli/common_opts.py``).
It is the key under which a command draws its ``[commands.<name>]`` parameter
defaults and runs its ``[pre_command_scripts.<name>]`` hooks.

These values are *not* the click command names. Group subcommands are namespaced
``<group>_<subcommand>`` (e.g. ``snapshot_create``, ``git_pull``) so that their
flat, single-string config key cannot collide with a top-level command of the
same leaf name. The ``config`` and ``plugin`` groups deliberately share one
group-level bucket (``config`` / ``plugin``) across all their subcommands, since
per-subcommand parameter defaults are not meaningful for those meta-commands.

Because the mapping is a per-call-site convention with deliberate exceptions, it
cannot be derived from the click tree. This frozenset is the single source of
truth; ``command_names_test.py`` asserts it stays in exact sync with the
``command_name="..."`` literals in the (non-test) source, so adding, renaming, or
removing a command forces an update here. The completion writer consumes it to
offer ``pre_command_scripts.<name>`` keys and to recognise which tree commands own
a ``[commands.<name>]`` defaults bucket (so it does not offer keys for the
group-level ``config`` / ``plugin`` subcommands, whose derived ``<group>_<sub>``
names are absent here).
"""

from typing import Final

import click

from imbue.mngr.cli.default_command_group import DefaultCommandGroup
from imbue.mngr.utils.click_utils import detect_alias_to_canonical

KNOWN_CONFIG_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {
        "archive",
        "ask",
        "capture",
        "cleanup",
        "clone",
        "config",
        "connect",
        "create",
        "destroy",
        "event",
        "exec",
        "gc",
        "git_pull",
        "git_push",
        "label",
        "limit",
        "list",
        "message",
        "migrate",
        "observe",
        "plugin",
        "rename",
        "rsync",
        "snapshot_create",
        "snapshot_destroy",
        "snapshot_list",
        "start",
        "stop",
        "transcript",
    }
)


def build_default_subcommand_choices(cli_group: click.Group) -> dict[str, list[str]]:
    """Map each ``DefaultCommandGroup._config_key`` to its canonical subcommand names.

    These are the ``[commands.<config_key>].default_subcommand`` completion
    targets: a group that supports a configurable default subcommand, keyed by the
    config key it reads (the root group's key is ``mngr``), with its alias-free
    subcommand names as the value choices. This lives in the cli layer -- where
    ``DefaultCommandGroup`` is defined -- so the config-layer completion writer can
    receive it as plain data rather than importing the class.
    """
    groups: list[click.Group] = [cli_group]
    groups.extend(cmd for cmd in cli_group.commands.values() if isinstance(cmd, click.Group))
    choices: dict[str, list[str]] = {}
    for group in groups:
        if isinstance(group, DefaultCommandGroup) and group._config_key is not None:
            aliases = detect_alias_to_canonical(group)
            choices[group._config_key] = sorted(name for name in group.commands.keys() if name not in aliases)
    return choices
