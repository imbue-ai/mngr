from collections.abc import Callable
from collections.abc import MutableMapping

import click

from imbue.imbue_common.pure import pure


class LazyProviderCliGroup(click.Group):
    """A ``click.Group`` placeholder that imports its real subcommands on demand.

    A provider plugin registers its ``mngr <name> ...`` operator commands through
    this so the group appears in ``mngr --help`` (rendered from the static ``help``
    text) without importing the module that defines the real command group -- and
    the provider's cloud SDK -- until the user actually runs ``mngr <name> ...``.
    Deferring that import is a large part of ``mngr``'s CLI cold-start budget.

    ``load`` imports and returns the real ``click.Group``; it is called and its
    result cached the first time the group's subcommands are introspected -- via
    ``list_commands``/``get_command`` (``--help``, dispatch, shell completion) or the
    ``commands`` mapping (doc generation). ``mngr --help`` renders the top-level entry
    from the static ``help`` text alone and never touches these, so the real group --
    and the provider SDK -- stays unimported until the provider is actually used.
    """

    def __init__(self, name: str, load: Callable[[], click.Group], help: str) -> None:
        self._load = load
        self._loaded_group: click.Group | None = None
        super().__init__(name=name, help=help)

    def _real_group(self) -> click.Group:
        if self._loaded_group is None:
            self._loaded_group = self._load()
        return self._loaded_group

    @property
    def commands(self) -> MutableMapping[str, click.Command]:
        # Delegating keeps the lazy group transparent to code that reads ``.commands``
        # directly (e.g. scripts/make_cli_docs.py) instead of the list/get API.
        return self._real_group().commands

    @commands.setter
    def commands(self, value: MutableMapping[str, click.Command]) -> None:
        # ``click.Group.__init__`` assigns an empty mapping here; ignore it -- the real
        # subcommands live on the lazily-loaded group and must not force the import now.
        del value

    def list_commands(self, ctx: click.Context) -> list[str]:
        return self._real_group().list_commands(ctx)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return self._real_group().get_command(ctx, cmd_name)


@pure
def detect_alias_to_canonical(cli_group: click.Group) -> dict[str, str]:
    """Detect command aliases by comparing registered names to canonical cmd.name.

    When a command is registered under a name different from its cmd.name
    (e.g. via ``cli.add_command(create, name="c")``), the registered name is
    an alias. Returns a dict mapping alias -> canonical name.
    """
    alias_to_canonical: dict[str, str] = {}
    for registered_name, cmd in cli_group.commands.items():
        if cmd.name is not None and registered_name != cmd.name:
            alias_to_canonical[registered_name] = cmd.name
    return alias_to_canonical


@pure
def detect_aliases_by_command(cli_group: click.Group) -> dict[str, list[str]]:
    """Detect command aliases, grouped by canonical name.

    Returns a dict mapping canonical command name -> list of aliases.
    For example: {"create": ["c"], "list": ["ls"], ...}.
    """
    aliases_by_command: dict[str, list[str]] = {}
    for registered_name, cmd in cli_group.commands.items():
        if cmd.name is not None and registered_name != cmd.name:
            aliases_by_command.setdefault(cmd.name, []).append(registered_name)
    return aliases_by_command
