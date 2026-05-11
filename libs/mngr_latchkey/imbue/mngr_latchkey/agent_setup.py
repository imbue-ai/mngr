"""High-level helpers for wiring latchkey into a freshly-created agent.

The lifecycle for a new agent has three latchkey-aware steps:

1. *Before* ``mngr create``: allocate an opaque permissions handle,
   materialize it with a deny-all baseline, mint a permissions-override
   JWT pointing at it, and assemble the env vars the agent needs
   (``LATCHKEY_GATEWAY``, ``LATCHKEY_GATEWAY_PASSWORD``,
   ``LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE``,
   ``LATCHKEY_DISABLE_COUNTING``). See :func:`prepare_agent_latchkey`.

2. *After* ``mngr create`` returns the canonical agent id: replace the
   opaque handle with a symlink to the canonical agent-keyed
   ``latchkey_permissions.json`` so the desktop's permission-grant flow
   writes to the canonical path while the gateway reads through the
   symlink. See :func:`finalize_agent_permissions`.

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
agent-side gateway URL but skip the password / JWT / opaque-handle
steps that need a working ``Latchkey``.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import link_opaque_permissions_to_agent
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import save_permissions

# Env-var names baked into the upstream latchkey CLI's wire contract.
# Kept as constants so callers building ``--env`` flags or ``mngr provision``
# arguments do not have to repeat them.
ENV_LATCHKEY_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
ENV_LATCHKEY_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
# Suppresses the per-workspace daily ping latchkey emits otherwise; we
# always set it so each agent does not get counted as a separate user.
ENV_LATCHKEY_DISABLE_COUNTING: Final[str] = "LATCHKEY_DISABLE_COUNTING"


class AgentLatchkeySetup(FrozenModel):
    """Outputs of :func:`prepare_agent_latchkey`.

    The caller is expected to:

    * Inject every ``env`` entry into the agent's environment (typically
      via ``mngr create --env KEY=VALUE`` flags).
    * Pass ``opaque_permissions_path`` back to
      :func:`finalize_agent_permissions` once the canonical agent id is
      known (skipped when ``opaque_permissions_path`` is ``None``, which
      happens only in the no-``Latchkey`` degraded mode).
    """

    env: Mapping[str, str] = Field(
        description=(
            "Environment variables to inject into the agent. Contains "
            f"``{ENV_LATCHKEY_GATEWAY}`` and ``{ENV_LATCHKEY_DISABLE_COUNTING}`` "
            "whenever a gateway URL is available, plus "
            f"``{ENV_LATCHKEY_GATEWAY_PASSWORD}`` and "
            f"``{ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE}`` whenever a real "
            "``Latchkey`` is supplied. Empty only in the on-host degraded "
            "mode (``latchkey=None`` with ``is_tunneled=False``) where no "
            "live gateway port is knowable."
        ),
    )
    opaque_permissions_path: Path | None = Field(
        default=None,
        description=(
            "Path to the agent's freshly-allocated opaque permissions handle "
            "(``<plugin_data_dir>/permissions/<uuid>.json``), materialized "
            "with deny-all baseline rules. Pass to "
            ":func:`finalize_agent_permissions` once the canonical agent id "
            "is known. ``None`` when no ``Latchkey`` was supplied; otherwise "
            "always set on a successful return."
        ),
    )


def prepare_agent_latchkey(
    latchkey: Latchkey | None,
    *,
    is_tunneled: bool,
) -> AgentLatchkeySetup:
    """Pre-create env vars + opaque permissions handle for a new agent.

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
        LatchkeyStoreError: when materializing the opaque permissions
            file fails.
    """
    if is_tunneled:
        gateway_url = f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    elif latchkey is None:
        # No live port to inject and no Latchkey to ask -- caller asked
        # for the empty case.
        return AgentLatchkeySetup(env={}, opaque_permissions_path=None)
    else:
        info = latchkey.ensure_gateway_started()
        gateway_url = f"http://{info.host}:{info.port}"

    env: dict[str, str] = {ENV_LATCHKEY_GATEWAY: gateway_url}
    opaque_path: Path | None = None

    if latchkey is not None:
        env[ENV_LATCHKEY_GATEWAY_PASSWORD] = latchkey.derive_gateway_password()
        opaque_path = new_opaque_permissions_path(latchkey.plugin_data_dir)
        save_permissions(opaque_path, LatchkeyPermissionsConfig())
        env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] = latchkey.create_permissions_override_jwt(opaque_path)

    # Always set the disable-counting flag whenever we're injecting a
    # gateway URL, so each agent doesn't get counted as a separate user
    # against the latchkey usage cap.
    env[ENV_LATCHKEY_DISABLE_COUNTING] = "1"

    return AgentLatchkeySetup(env=env, opaque_permissions_path=opaque_path)


def finalize_agent_permissions(
    latchkey: Latchkey,
    opaque_permissions_path: Path | None,
    agent_id: AgentId,
) -> None:
    """Replace the opaque permissions handle with a symlink to the canonical agent path.

    No-op when ``opaque_permissions_path`` is ``None`` -- that's the
    sentinel :func:`prepare_agent_latchkey` returns in the
    no-``Latchkey`` degraded mode, in which case there is nothing to
    finalize.

    Raises :class:`LatchkeyStoreError` if the linking fails. Callers
    decide whether to surface the failure or carry on: if the link
    isn't established, the agent's gateway requests still evaluate
    against the deny-all baseline file the JWT references directly
    (the opaque file itself, which already exists), but subsequent
    UI-driven permission grants will not take effect because the UI
    writes to the canonical agent-keyed path that this function would
    have linked.
    """
    if opaque_permissions_path is None:
        return
    link_opaque_permissions_to_agent(latchkey.plugin_data_dir, opaque_permissions_path, agent_id)
