"""High-level helpers for wiring latchkey into a freshly-created agent.

The lifecycle for a new agent has three latchkey-aware steps:

1. *Before* ``mngr create``: materialize the host's permissions file
   (with a deny-all baseline if it doesn't already exist), mint a
   permissions-override JWT pointing at it, and assemble the env vars
   the agent's host needs (``LATCHKEY_GATEWAY``,
   ``LATCHKEY_GATEWAY_PASSWORD``,
   ``LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE``,
   ``LATCHKEY_DISABLE_COUNTING``). See :func:`prepare_agent_latchkey`.

2. *After* ``mngr create`` returns the canonical host id: cross-check
   it against the recorded ``host-id`` file for ``host_name``. If the
   file is absent or its content does not match, the host with that
   name has been recreated and any previously-granted permissions are
   stale -- clear the permissions file and overwrite ``host-id`` with
   the freshly-reported value. See :func:`finalize_agent_permissions`.

3. (Out of scope here.) When the agent is later discovered, the
   :class:`LatchkeyDiscoveryHandler` ensures the shared gateway is up
   and reverse-tunnels it into the agent for non-DEV launches.

Both helpers raise on failure (``LatchkeyError`` from the upstream
CLI, ``LatchkeyStoreError`` from on-disk persistence). Callers decide
whether to fail agent creation or just surface a warning -- the helpers
themselves don't make that policy call.

The one place ``prepare_agent_latchkey`` *does* tolerate an absent
dependency is when ``latchkey`` itself is ``None``: that's a degraded
test / no-password-gateway mode where we still produce the constant
agent-side gateway URL but skip the password / JWT / permissions-file
steps that need a working ``Latchkey``.
"""

from collections.abc import Mapping
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import read_stored_host_id
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import write_stored_host_id

# Env-var names baked into the upstream latchkey CLI's wire contract.
# Kept as constants so callers building ``--host-env`` flags or ``mngr
# provision`` arguments do not have to repeat them.
ENV_LATCHKEY_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
ENV_LATCHKEY_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
# Suppresses the per-workspace daily ping latchkey emits otherwise; we
# always set it so each agent does not get counted as a separate user.
ENV_LATCHKEY_DISABLE_COUNTING: Final[str] = "LATCHKEY_DISABLE_COUNTING"


class AgentLatchkeySetup(FrozenModel):
    """Outputs of :func:`prepare_agent_latchkey`.

    The caller is expected to inject every ``env`` entry into the
    agent's host environment (typically via ``mngr create --host-env
    KEY=VALUE`` flags). Once ``mngr create`` returns the canonical
    ``HostId``, the caller invokes :func:`finalize_agent_permissions`
    to reconcile it with the per-host ``host-id`` file.
    """

    env: Mapping[str, str] = Field(
        description=(
            "Environment variables to inject into the agent's host. Contains "
            f"``{ENV_LATCHKEY_GATEWAY}`` and ``{ENV_LATCHKEY_DISABLE_COUNTING}`` "
            "whenever a gateway URL is available, plus "
            f"``{ENV_LATCHKEY_GATEWAY_PASSWORD}`` and "
            f"``{ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE}`` whenever a real "
            "``Latchkey`` is supplied. Empty only in the on-host degraded "
            "mode (``latchkey=None`` with ``is_tunneled=False``) where no "
            "live gateway port is knowable."
        ),
    )


def prepare_agent_latchkey(
    latchkey: Latchkey | None,
    host_name: HostName,
    *,
    is_tunneled: bool,
) -> AgentLatchkeySetup:
    """Pre-create env vars + per-host permissions file for a new agent.

    ``host_name`` is the canonical name of the host that will run the
    agent. The permissions-override JWT is minted against
    ``{plugin_data_dir}/hosts/{host_name}/latchkey_permissions.json``;
    if that file does not exist yet it is materialized with deny-all
    baseline rules. An existing file is left untouched so that
    re-deploying the same host (same ``host_id``) preserves prior
    grants -- :func:`finalize_agent_permissions` clears it later if
    ``mngr create`` reports a fresh ``host_id``.

    ``is_tunneled`` selects how the agent reaches the gateway:

    * ``True`` (containers, VMs, VPS, leased hosts): the agent's
      ``LATCHKEY_GATEWAY`` points at the constant agent-side loopback
      ``http://127.0.0.1:<AGENT_SIDE_LATCHKEY_PORT>``. A reverse SSH
      tunnel set up at agent-discovery time bridges this to the
      gateway's dynamic host port. The gateway is *not* started here --
      the discovery handler does that on its own when the agent shows
      up. (Starting the gateway eagerly would force every ``mngr create``
      to spawn a gateway even for tests that never exercise latchkey.)
    * ``False`` (DEV / on-bare-host agents): the agent runs on the same
      host as the gateway and reaches it directly. We have to start the
      gateway *now* to learn its dynamic port and bake it into
      ``LATCHKEY_GATEWAY``.

    ``latchkey=None`` is a degraded mode for tests / no-password-gateway
    setups: we still inject the constant agent-side gateway URL when
    ``is_tunneled=True`` (the URL alone is meaningful) but skip the
    password and JWT entirely. For ``is_tunneled=False`` with
    ``latchkey=None`` there is no live port to inject either, so we
    return empty env.

    Raises:
        LatchkeyError: when ``ensure_gateway_started`` /
            ``derive_gateway_password`` / ``create_permissions_override_jwt``
            fails on the supplied ``Latchkey``. Callers that want graceful
            degradation should catch and fall back to an empty
            :class:`AgentLatchkeySetup` themselves -- this function does
            not make that policy call.
        LatchkeyStoreError: when materializing the per-host permissions
            file fails.
    """
    if is_tunneled:
        gateway_url = f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    elif latchkey is None:
        # No live port to inject and no Latchkey to ask -- caller asked
        # for the empty case.
        return AgentLatchkeySetup(env={})
    else:
        info = latchkey.ensure_gateway_started()
        gateway_url = f"http://{info.host}:{info.port}"

    env: dict[str, str] = {ENV_LATCHKEY_GATEWAY: gateway_url}

    if latchkey is not None:
        env[ENV_LATCHKEY_GATEWAY_PASSWORD] = latchkey.derive_gateway_password()
        permissions_path = permissions_path_for_host(latchkey.plugin_data_dir, host_name)
        if not permissions_path.is_file():
            save_permissions(permissions_path, LatchkeyPermissionsConfig())
        env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] = latchkey.create_permissions_override_jwt(permissions_path)

    # Always set the disable-counting flag whenever we're injecting a
    # gateway URL, so each agent doesn't get counted as a separate user
    # against the latchkey usage cap.
    env[ENV_LATCHKEY_DISABLE_COUNTING] = "1"

    return AgentLatchkeySetup(env=env)


def finalize_agent_permissions(
    latchkey: Latchkey,
    host_name: HostName,
    host_id: HostId,
) -> None:
    """Reconcile the per-host permissions file with the canonical ``host_id``.

    Reads ``{plugin_data_dir}/hosts/{host_name}/host-id``. If it is
    missing or its content does not match ``host_id``, the host with
    that name has been recreated since the last permissions grant --
    any rules in ``latchkey_permissions.json`` are stale and would
    grant the new host privileges its operator never approved. To
    avoid that, the permissions file is cleared (rewritten with empty
    rules) and ``host-id`` is overwritten with the new value.

    Raises :class:`LatchkeyStoreError` if writing fails. Callers
    decide whether to surface the failure or carry on: a write
    failure here means the next agent on the same ``host_name`` may
    inherit stale grants, but the agent that just came up is
    unaffected because its JWT keeps resolving against whatever rules
    are on disk.
    """
    plugin_data_dir = latchkey.plugin_data_dir
    stored = read_stored_host_id(plugin_data_dir, host_name)
    if stored == host_id:
        return
    # Clear potentially-stale permissions before recording the new
    # host_id so a crash between the two steps leaves us with an
    # over-restrictive (empty) policy rather than an over-permissive
    # stale one.
    permissions_path = permissions_path_for_host(plugin_data_dir, host_name)
    save_permissions(permissions_path, LatchkeyPermissionsConfig())
    write_stored_host_id(plugin_data_dir, host_name, host_id)
