"""Click group for ``mngr foreman <sub>``."""

from typing import Any

import click

from imbue.mngr_foreman.cli.serve import serve as serve_command


@click.group(name="foreman")
@click.pass_context
def foreman(ctx: click.Context, **kwargs: Any) -> None:
    """Always-on web remote control for your mngr agents [experimental].

    Runs a single Flask server on this box, over every agent in mngr's view:
    a chat UI for claude agents (live transcript + send) and a web terminal for
    any agent type, drivable from any device (including a phone). No code is
    deployed to target boxes and there is no auth -- bind to a tailnet IP or
    firewall the port. Create agents with plain ``mngr create``.

    \b
    Examples:
      mngr foreman serve --port 8700
      mngr foreman serve --host 100.64.0.1
    """


foreman.add_command(serve_command)
