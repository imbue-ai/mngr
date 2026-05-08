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

The helpers degrade gracefully: each one logs a warning and returns
sentinels (``None`` env values, ``None`` paths) on failure so a
latchkey misconfiguration does not abort agent creation. The agent
will simply lack working latchkey wiring.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
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
      known.

    Both fields are populated independently. If JWT minting fails but
    the gateway is up, ``env`` will still carry ``LATCHKEY_GATEWAY``
    (and ``LATCHKEY_GATEWAY_PASSWORD`` if available) while
    ``opaque_permissions_path`` is ``None``; the agent will then fall
    back to the gateway's deny-all default permissions instead of its
    own.
    """

    env: Mapping[str, str] = Field(
        description=(
            "Environment variables to inject into the agent. Always contains "
            f"``{ENV_LATCHKEY_GATEWAY}`` when latchkey wiring succeeds, plus "
            f"``{ENV_LATCHKEY_GATEWAY_PASSWORD}`` when password derivation "
            f"succeeds, plus ``{ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE}`` "
            "when JWT minting succeeds. ``LATCHKEY_DISABLE_COUNTING=1`` is "
            "set whenever any latchkey env var is set. Empty when latchkey "
            "wiring is unavailable for this agent (e.g. gateway failed to "
            "start)."
        ),
    )
    opaque_permissions_path: Path | None = Field(
        default=None,
        description=(
            "Path to the agent's freshly-allocated opaque permissions handle "
            "(``<data_dir>/latchkey/permissions/<uuid>.json``), materialized "
            "with deny-all baseline rules. Pass to "
            ":func:`finalize_agent_permissions` once the canonical agent id "
            "is known. ``None`` when JWT minting failed or no Latchkey was "
            "supplied."
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

    ``latchkey=None`` is a degraded mode where we still inject the
    gateway URL for tunneled agents (since that URL is a fixed constant
    and useful in tests / non-password-protected setups) but skip the
    password and JWT. For ``is_tunneled=False`` with ``latchkey=None``
    there is no live port to inject and we return empty env entirely.

    Failures (no Latchkey, gateway start failed, password derivation
    failed, JWT mint failed) degrade rather than raise: the returned
    ``env`` carries whichever vars succeeded, and missing pieces are
    simply absent.
    """
    gateway_url = _resolve_gateway_url(latchkey, is_tunneled=is_tunneled)
    if gateway_url is None:
        return AgentLatchkeySetup(env={}, opaque_permissions_path=None)

    env: dict[str, str] = {ENV_LATCHKEY_GATEWAY: gateway_url}
    opaque_path: Path | None = None

    if latchkey is not None:
        password = _derive_gateway_password_or_warn(latchkey)
        if password is not None:
            env[ENV_LATCHKEY_GATEWAY_PASSWORD] = password

        opaque_path, jwt = _prepare_opaque_permissions_handle(latchkey)
        if jwt is not None:
            env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] = jwt

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

    Idempotent / no-op when ``opaque_permissions_path`` is ``None``
    (which is what :func:`prepare_agent_latchkey` returns on the JWT-mint
    failure path -- there is nothing to finalize then).

    Failures here are logged but do not raise. The agent's gateway
    requests will still be evaluated against the deny-all baseline file
    that the JWT references directly (i.e. the opaque file itself); the
    only consequence of a failed finalize is that subsequent
    user-driven permission grants will not take effect because the UI
    writes to the canonical agent-keyed path which is not yet linked.
    """
    if opaque_permissions_path is None:
        return
    try:
        link_opaque_permissions_to_agent(latchkey.plugin_data_dir, opaque_permissions_path, agent_id)
    except LatchkeyStoreError as e:
        logger.warning("Failed to link latchkey permissions handle for agent {}: {}", agent_id, e)


# -- Internals ----------------------------------------------------------------


def _resolve_gateway_url(latchkey: Latchkey | None, *, is_tunneled: bool) -> str | None:
    """Return the URL the agent should use as ``LATCHKEY_GATEWAY``.

    Tunneled agents always get the constant agent-side loopback URL
    regardless of what port the host-side gateway happens to listen on,
    even when ``latchkey is None`` -- the URL alone is meaningful for
    tests and non-password-protected gateways. DEV / on-host agents need
    the gateway's live dynamic port, so we must ensure the gateway is up
    here, which requires a real ``Latchkey``.
    """
    if is_tunneled:
        return f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    if latchkey is None:
        return None
    try:
        info = latchkey.ensure_gateway_started()
    except LatchkeyError as e:
        logger.warning("Failed to start latchkey gateway for on-host agent: {}", e)
        return None
    return f"http://{info.host}:{info.port}"


def _derive_gateway_password_or_warn(latchkey: Latchkey) -> str | None:
    """Wrap :meth:`Latchkey.derive_gateway_password`, downgrading errors to warnings."""
    try:
        return latchkey.derive_gateway_password()
    except (LatchkeyError, LatchkeyJwtMintError) as e:
        logger.warning("Failed to derive latchkey gateway password: {}", e)
        return None


def _prepare_opaque_permissions_handle(
    latchkey: Latchkey,
) -> tuple[Path | None, str | None]:
    """Allocate an opaque permissions handle and mint its override JWT.

    Returns ``(opaque_path, jwt)`` on success. On JWT-mint failure the
    just-created file is unlinked (best-effort) so we don't litter
    the plugin data directory with orphan handles for agents that will
    never be able to use them, and ``(None, None)`` is returned.
    """
    opaque_path = new_opaque_permissions_path(latchkey.plugin_data_dir)
    save_permissions(opaque_path, LatchkeyPermissionsConfig())
    try:
        jwt = latchkey.create_permissions_override_jwt(opaque_path)
    except (LatchkeyError, LatchkeyJwtMintError) as e:
        logger.warning("Failed to mint latchkey permissions-override JWT: {}", e)
        try:
            opaque_path.unlink()
        except OSError:
            pass
        return None, None
    return opaque_path, jwt
