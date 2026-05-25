"""High-level helpers for wiring latchkey into a freshly-created agent.

The lifecycle for a new agent has three latchkey-aware steps:

1. *Before* ``mngr create``: allocate an opaque permissions handle,
   materialize it with a deny-all baseline, mint a permissions-override
   JWT pointing at it, and assemble the env vars the agent needs
   (``LATCHKEY_GATEWAY``, ``LATCHKEY_GATEWAY_PASSWORD``,
   ``LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE``,
   ``LATCHKEY_DISABLE_COUNTING``). See :func:`prepare_agent_latchkey`.

2. *After* ``mngr create`` returns the canonical host id: replace the
   opaque handle with a symlink to the canonical host-keyed
   ``latchkey_permissions.json`` so the desktop's permission-grant flow
   writes to the canonical path while the gateway reads through the
   symlink. See :func:`finalize_host_permissions`.

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

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import JsonValue

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import save_permissions

# Env-var names baked into the upstream latchkey CLI's wire contract.
# Kept as constants so callers building ``--env`` flags do not have to repeat them.
ENV_LATCHKEY_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
ENV_LATCHKEY_GATEWAY_PASSWORD: Final[str] = "LATCHKEY_GATEWAY_PASSWORD"
ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE: Final[str] = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
# Suppresses the per-workspace daily ping latchkey emits otherwise; we
# always set it so each agent does not get counted as a separate user.
ENV_LATCHKEY_DISABLE_COUNTING: Final[str] = "LATCHKEY_DISABLE_COUNTING"

# Detent schema names and host string for the gateway-self baseline that
# every agent inherits. Defined inline (in the agent's permissions file)
# rather than relying on detent's built-in catalog so the names are
# self-contained and the grant is exactly the endpoints we want.
_GATEWAY_SELF_HOST: Final[str] = "latchkey-self.invalid"
_SCOPE_LATCHKEY_SELF: Final[str] = "latchkey-self"
_PERM_CREATE_PERMISSION_REQUEST: Final[str] = "latchkey-self-create-permission-request"
_PERM_READ_SELF_PERMISSIONS: Final[str] = "latchkey-self-read-self-permissions"
_PERM_READ_AVAILABLE_PERMISSIONS: Final[str] = "latchkey-self-read-available-permissions"

# Regex matching ``/permissions/available/<service_name>`` where the
# service name segment is one or more lowercase letters, digits, and
# hyphens (starting with a letter or digit). Mirrors the gateway
# ``permissions.mjs`` extension's own ``VALID_SERVICE_NAME_PATTERN`` so
# the agent baseline cannot reach paths the extension itself would
# refuse to serve. The trailing ``$`` rules out the collection endpoint
# at ``/permissions/available`` (no segment): the baseline only opens
# up the per-service catalog endpoint.
_AVAILABLE_PERMISSIONS_PATH_PATTERN: Final[str] = r"^/permissions/available/[a-z0-9][a-z0-9-]*$"

# Path pattern for the ``minds-api-proxy`` gateway extension. This is
# the *only* path under ``latchkey-self.invalid`` an agent may reach
# via the proxy by default; per-agent rules added at finalize-host-
# permissions time narrow this further to the specific
# ``/api/v1/agents/<agent_id>/`` subtree the agent is allowed to talk
# about. The notifications endpoint lives under that subtree and is
# therefore reachable by every agent the desktop client provisions
# without any extra grant -- it carries no per-user-grant decision and
# should always be available.
_MINDS_API_PROXY_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/agents/"
_PERM_MINDS_API_PROXY_NOTIFICATIONS: Final[str] = "minds-api-proxy-notifications"

_AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        {
            _SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
                _PERM_MINDS_API_PROXY_NOTIFICATIONS,
            ],
        },
    ),
    schemas={
        _SCOPE_LATCHKEY_SELF: {
            "properties": {"domain": {"const": _GATEWAY_SELF_HOST}},
            "required": ["domain"],
        },
        _PERM_CREATE_PERMISSION_REQUEST: {
            "properties": {
                "method": {"const": "POST"},
                "path": {"const": "/permission-requests"},
            },
            "required": ["method", "path"],
        },
        _PERM_READ_SELF_PERMISSIONS: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"const": "/permissions/self"},
            },
            "required": ["method", "path"],
        },
        _PERM_READ_AVAILABLE_PERMISSIONS: {
            "properties": {
                "method": {"const": "GET"},
                "path": {
                    "type": "string",
                    "pattern": _AVAILABLE_PERMISSIONS_PATH_PATTERN,
                },
            },
            "required": ["method", "path"],
        },
        # Notifications endpoint is reachable by every agent the
        # desktop client provisions; the ``{agent_id}`` segment is not
        # constrained here so the schema is shared across all agents.
        # Per-agent isolation (this caller may only POST to
        # ``/api/v1/agents/<its own id>/notifications``) is enforced by
        # the per-agent permission schema + rule that
        # :func:`agent_minds_api_proxy_schema_name` describes; this
        # baseline grant is intentionally narrower than that (it does
        # not cover Telegram or future routes) so the only blanket
        # baseline grant we hand out is for the notification path.
        _PERM_MINDS_API_PROXY_NOTIFICATIONS: {
            "properties": {
                "method": {"const": "POST"},
                "path": {
                    "type": "string",
                    "pattern": rf"^{_MINDS_API_PROXY_PATH_PREFIX}[^/]+/notifications/?$",
                },
            },
            "required": ["method", "path"],
        },
    },
)


def agent_minds_api_proxy_scope_name(agent_id: str) -> str:
    """Return the scope-schema (rule_key) name reserved for ``agent_id``.

    Each minds agent gets its own per-agent rule in the host's
    permissions file, keyed by a scope schema name unique to that agent.
    The scope schema itself is just ``latchkey-self.invalid`` (the
    gateway-self host); naming it per-agent is what lets us POST a
    fresh rule without disturbing other agents' rules or the baseline.
    """
    return f"minds-api-self-{agent_id}"


def agent_minds_api_proxy_permission_name(agent_id: str) -> str:
    """Return the permission-schema name reserved for ``agent_id``.

    The permission schema encodes the path pattern that constrains the
    agent to its own ``/api/v1/agents/<agent_id>/`` subtree on the
    Minds API proxy.
    """
    return f"minds-api-proxy-call-{agent_id}"


def build_agent_minds_api_proxy_schemas(agent_id: str) -> dict[str, JsonValue]:
    """Return the two inline schemas a per-agent rule references.

    * ``scope`` -- mirrors ``latchkey-self`` (domain ==
      ``latchkey-self.invalid``); named uniquely per agent so the rule
      keyed on it doesn't collide with the baseline ``latchkey-self``
      rule.
    * ``permission`` -- matches every method on every path under
      ``/minds-api-proxy/api/v1/agents/<agent_id>/`` (with or without
      a trailing path). The path is anchored on the agent id literal
      so an agent on host A cannot reach ``/api/v1/agents/<B's id>/...``
      even though A and B share the gateway: B's id only appears in
      *B's host*'s permissions file.
    """
    # ``re.escape`` keeps us safe against any character classes the
    # detent path pattern would otherwise interpret; agent ids are
    # UUID-shaped (hex + dashes) today, but encoding it through the
    # escape removes any future surprise.
    safe_agent_id = re.escape(agent_id)
    return {
        agent_minds_api_proxy_scope_name(agent_id): {
            "properties": {"domain": {"const": _GATEWAY_SELF_HOST}},
            "required": ["domain"],
        },
        agent_minds_api_proxy_permission_name(agent_id): {
            "properties": {
                "method": {"type": "string", "pattern": r"^[A-Z]+$"},
                "path": {
                    "type": "string",
                    "pattern": rf"^{_MINDS_API_PROXY_PATH_PREFIX}{safe_agent_id}(/.*)?$",
                },
            },
            "required": ["method", "path"],
        },
    }


class AgentLatchkeySetup(FrozenModel):
    """Outputs of :func:`prepare_agent_latchkey`.

    The caller is expected to:

    * Inject every ``env`` entry into the agent's *host* environment
      (typically via ``mngr create --host-env KEY=VALUE`` flags so every
      agent that ever runs on the host inherits the same wiring).
    * Pass ``opaque_permissions_path`` back to
      :func:`finalize_host_permissions` once the canonical host id is
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
            "Path to the freshly-allocated opaque permissions handle "
            "(``<plugin_data_dir>/permissions/<uuid>.json``), materialized "
            "with deny-all baseline rules. Pass to "
            ":func:`finalize_host_permissions` once the canonical host id "
            "is known. ``None`` when no ``Latchkey`` was supplied; otherwise "
            "always set on a successful return."
        ),
    )


def prepare_agent_latchkey(
    latchkey: Latchkey | None,
    *,
    is_tunneled: bool,
    concurrency_group: ConcurrencyGroup | None = None,
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
      ``concurrency_group`` is ignored in this mode.
    * ``False`` (DEV / on-bare-host agents): the agent runs on the same
      host as the gateway and reaches it directly. We have to start the
      gateway *now* to learn its dynamic port and bake it into
      ``LATCHKEY_GATEWAY``. ``concurrency_group`` becomes the owner of
      the spawned gateway subprocess; it must be supplied whenever
      ``latchkey is not None``.

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
        LatchkeyError: when ``is_tunneled=False`` with a real ``Latchkey``
            but no ``concurrency_group`` is supplied (we need one to
            own the spawned gateway subprocess).
    """
    if is_tunneled:
        gateway_url = f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    elif latchkey is None:
        # No live port to inject and no Latchkey to ask -- caller asked
        # for the empty case.
        return AgentLatchkeySetup(env={}, opaque_permissions_path=None)
    elif concurrency_group is None:
        raise LatchkeyError(
            "prepare_agent_latchkey(is_tunneled=False) needs a concurrency_group to own the spawned gateway subprocess"
        )
    else:
        gateway_port = latchkey.start_gateway(concurrency_group)
        gateway_url = f"http://{latchkey.listen_host}:{gateway_port}"

    env: dict[str, str] = {ENV_LATCHKEY_GATEWAY: gateway_url}
    opaque_path: Path | None = None

    if latchkey is not None:
        env[ENV_LATCHKEY_GATEWAY_PASSWORD] = latchkey.derive_gateway_password()
        opaque_path = new_opaque_permissions_path(latchkey.plugin_data_dir)
        save_permissions(opaque_path, _AGENT_BASELINE_PERMISSIONS)
        env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] = latchkey.create_permissions_override_jwt(opaque_path)

    # Always set the disable-counting flag whenever we're injecting a
    # gateway URL, so each agent doesn't get counted as a separate user
    # against the latchkey usage cap.
    env[ENV_LATCHKEY_DISABLE_COUNTING] = "1"

    return AgentLatchkeySetup(env=env, opaque_permissions_path=opaque_path)


def finalize_host_permissions(
    latchkey: Latchkey,
    opaque_permissions_path: Path | None,
    host_id: HostId,
) -> None:
    """Replace the opaque permissions handle with a symlink to the canonical host path.

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
    writes to the canonical host-keyed path that this function would
    have linked.
    """
    if opaque_permissions_path is None:
        return
    link_opaque_permissions_to_host(latchkey.plugin_data_dir, opaque_permissions_path, host_id)
