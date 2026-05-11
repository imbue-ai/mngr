"""The top-level `mngr imbue_cloud` click group."""

import click

from imbue.mngr_imbue_cloud.cli.admin import admin
from imbue.mngr_imbue_cloud.cli.auth import auth
from imbue.mngr_imbue_cloud.cli.hosts import hosts
from imbue.mngr_imbue_cloud.cli.keys import keys
from imbue.mngr_imbue_cloud.cli.tunnels import tunnels


@click.group(name="imbue_cloud")
def imbue_cloud() -> None:
    """Imbue Cloud (auth, host leasing, keys, tunnels, pool admin)."""


imbue_cloud.add_command(auth)
imbue_cloud.add_command(hosts)
imbue_cloud.add_command(keys)
imbue_cloud.add_command(tunnels)
imbue_cloud.add_command(admin)
