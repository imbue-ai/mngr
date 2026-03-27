from typing import Any

import click

from imbue.mngr.config.pre_readers import read_default_command


class DefaultCommandGroup(click.Group):
    """A click.Group that defaults to a specific subcommand when none is given.

    When no subcommand is provided, or when an unrecognized subcommand is given,
    the arguments are forwarded to the default command.

    Subclasses can set `_default_command` to change the compile-time default
    (defaults to `""`, i.e. no defaulting -- bare invocation shows help).

    Subclasses can also set `_config_key` to enable runtime configuration of
    the default via `[commands.<config_key>].default_subcommand` in config
    files.  When `_config_key` is set, the config value is read at the start
    of each invocation (in `make_context`) and written to `_default_command`.
    An empty string in config disables defaulting entirely (the group shows
    help / "No such command" instead).
    """

    _default_command: str = ""
    _config_key: str | None = None

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        if self._config_key is not None:
            configured = read_default_command(self._config_key)
            if configured is not None:
                self._default_command = configured
        return super().make_context(info_name, args, parent=parent, **extra)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args and self._default_command and not ctx.resilient_parsing:
            args = [self._default_command]
        return super().parse_args(ctx, args)

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args:
            cmd = self.get_command(ctx, args[0])
            if cmd is None and self._default_command:
                return super().resolve_command(ctx, [self._default_command] + args)
        return super().resolve_command(ctx, args)
