"""Which latchkey store answers for a given machine.

Credentials belong to the machine that uses them: the user's computer keeps one
store, shared by every local workspace, and each remote workspace's VPS has one
of its own. So "is Slack connected?" has no single answer any more -- it depends
on which machine is being asked about, and this module is where that is decided.

A remote host's credentials live in its *machine store*
(:mod:`imbue.mngr_latchkey.remote._mirror`), a `LATCHKEY_DIRECTORY` of its own
that shares the desktop's config, browser session and encryption key. Everything
that reads or writes credentials on a workspace's behalf -- the Permissions tab,
the Add connection pane, the permission dialog's sign-in -- goes through the
store this module hands back, so a sign-in for one machine never lands in
another machine's connectors.

A machine is recognized by the encryption key recorded for it when its gateway
was first provisioned. Local hosts never have one, so they resolve to the
desktop's store, which is exactly right: their agents *run* on this computer.
A remote host whose first provisioning pass has not finished yet resolves there
too, and moves to its own store once it has one -- the same answer the desktop
gave before any of this existed.
"""

from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.permission_overview import PermissionOverviewError
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.remote.credentials import latchkey_for_machine
from imbue.mngr_latchkey.remote.credentials import stored_machine_encryption_key
from imbue.mngr_latchkey.store import LatchkeyStoreError


def machine_latchkey_for_host(desktop_latchkey: Latchkey, host_id: HostId) -> Latchkey:
    """Return the latchkey store holding the credentials of the machine ``host_id`` runs on.

    Never hand the result to anything that reads ``plugin_data_dir`` (permission
    file paths, the gateway client): that is the desktop's, whichever machine the
    credentials belong to.

    Raises:
        PermissionOverviewError: when the machine's store cannot be prepared.
    """
    data_dir = desktop_latchkey.plugin_data_dir
    try:
        if stored_machine_encryption_key(data_dir, host_id) is None:
            return desktop_latchkey
        return latchkey_for_machine(desktop_latchkey, data_dir, host_id)
    except LatchkeyStoreError as e:
        raise PermissionOverviewError(f"Could not open the credentials of host '{host_id}': {e}") from e


def is_machine_store_of_its_own(desktop_latchkey: Latchkey, machine_latchkey: Latchkey) -> bool:
    """Whether ``machine_latchkey`` is a machine's own store rather than this computer's.

    ``False`` for the store this computer keeps -- which every local host reads,
    and which answers for a remote host whose gateway has not been provisioned
    yet -- so a credential written or cleared there is written or cleared for
    all of them at once.
    """
    return machine_latchkey.latchkey_directory != desktop_latchkey.latchkey_directory


def machine_latchkey_for_workspace(
    desktop_latchkey: Latchkey,
    backend_resolver: BackendResolverInterface,
    workspace_agent_id: str,
) -> Latchkey:
    """Return the latchkey store holding one workspace's machine's credentials.

    Raises:
        PermissionOverviewError: when the workspace's host cannot be resolved --
            answering from the desktop's store instead would show, and connect,
            the wrong machine's accounts.
    """
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionOverviewError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot reach its credentials."
        )
    logger.trace("Resolving credentials for workspace {} against host {}", workspace_agent_id, host_id)
    return machine_latchkey_for_host(desktop_latchkey, host_id)
