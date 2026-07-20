"""Click group for ``mngr foreman <sub>``."""

from typing import Any

import click

from imbue.mngr_foreman.cli.create import create as create_command
from imbue.mngr_foreman.cli.serve import serve as serve_command


@click.group(name="foreman")
@click.pass_context
def foreman(ctx: click.Context, **kwargs: Any) -> None:
    """Always-on web remote control for your mngr agents [experimental].

    Runs a single Flask server on this box that lists every mngr agent and
    streams each claude agent's transcript to a web UI you can drive from any
    device (including a phone). No code is deployed to target boxes and there
    is no auth -- bind to a tailnet IP or firewall the port.

    \b
    Examples:
      mngr foreman serve --port 8700
      mngr foreman serve --host 100.64.0.1 --foreman-only
      mngr foreman create my-agent --new-host --in modal
    """


foreman.add_command(serve_command)
foreman.add_command(create_command)
