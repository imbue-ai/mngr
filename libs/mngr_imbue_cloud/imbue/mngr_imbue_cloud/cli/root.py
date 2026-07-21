"""The top-level `mngr imbue_cloud` click group."""

import click

from imbue.mngr_imbue_cloud.cli.account import account
from imbue.mngr_imbue_cloud.cli.accounts_admin import account_admin
from imbue.mngr_imbue_cloud.cli.admin import admin
from imbue.mngr_imbue_cloud.cli.auth import auth
from imbue.mngr_imbue_cloud.cli.buckets import bucket
from imbue.mngr_imbue_cloud.cli.hosts import hosts
from imbue.mngr_imbue_cloud.cli.keys import keys
from imbue.mngr_imbue_cloud.cli.paid import paid
from imbue.mngr_imbue_cloud.cli.server import server
from imbue.mngr_imbue_cloud.cli.sync import sync
from imbue.mngr_imbue_cloud.cli.tunnels import tunnels

# Operator-only paid-list, account-entitlements, and bare-metal server/slice
# management live under the existing `admin` group.
admin.add_command(paid)
admin.add_command(account_admin)
admin.add_command(server)


@click.group(name="imbue_cloud")
def imbue_cloud() -> None:
    """Imbue Cloud (auth, account plans/quotas, host leasing, keys, buckets, tunnels, pool admin)."""


imbue_cloud.add_command(auth)
imbue_cloud.add_command(account)
imbue_cloud.add_command(hosts)
imbue_cloud.add_command(keys)
imbue_cloud.add_command(bucket)
imbue_cloud.add_command(tunnels)
imbue_cloud.add_command(sync)
imbue_cloud.add_command(admin)
