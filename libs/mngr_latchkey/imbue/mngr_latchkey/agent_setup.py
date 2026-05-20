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

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
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

# Inline detent scope + named permissions for the minds desktop client's
# peer-mind management endpoints. Mirrors the ``_SCOPE_LATCHKEY_SELF``
# paradigm above: the scope schema constrains the domain + path prefix,
# each named permission constrains a specific ``(method, path)`` pair. To
# grow the scope (e.g. when destroy / list endpoints opt into bearer
# auth), add a new permission entry and widen ``_MINDS_SCOPE_PATH_PATTERN``
# accordingly. Defining everything in every per-agent permissions file
# (rather than upstreaming to detent's built-in catalog) keeps the schema
# self-contained: a user grant like ``{"minds": ["any"]}`` will match
# exactly the endpoints declared here, on whatever port the desktop
# client picked this session. The scope is materialized but NOT pre-
# granted -- agents go through the standard permission-request dialog
# before their first spawn, and subsequent spawns reuse the rule the
# user wrote on approval.
_SCOPE_MINDS: Final[str] = "minds"
_PERM_CREATE_MIND: Final[str] = "minds-create"
_PERM_MIND_STATUS: Final[str] = "minds-status"
_PERM_MIND_LOGS: Final[str] = "minds-logs"

_MINDS_HOST: Final[str] = "127.0.0.1"
_MINDS_CREATE_AGENT_PATH: Final[str] = "/api/create-agent"
# Creation id segment is exactly ``creation-<32 hex chars>`` per
# ``imbue.minds.primitives.CreationId``; pinning it that tightly here
# avoids admitting traversal-shaped segments or unrelated id schemes.
_MINDS_STATUS_PATH_PATTERN: Final[str] = r"^/api/create-agent/creation-[0-9a-f]{32}/status$"
_MINDS_LOGS_PATH_PATTERN: Final[str] = r"^/api/create-agent/creation-[0-9a-f]{32}/logs$"
# Scope-level path-prefix gate. Necessary because detent ``any`` matches
# every request satisfying the scope schema -- without this gate,
# ``{"minds": ["any"]}`` would escape into every other ``127.0.0.1``
# endpoint (the latchkey gateway itself, cookie-gated
# ``/api/destroy-agent/...``, etc.).
_MINDS_SCOPE_PATH_PATTERN: Final[str] = r"^/api/create-agent(/|$)"

_AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        {
            _SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
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
        _SCOPE_MINDS: {
            "properties": {
                "domain": {"const": _MINDS_HOST},
                "path": {"type": "string", "pattern": _MINDS_SCOPE_PATH_PATTERN},
            },
            "required": ["domain", "path"],
        },
        _PERM_CREATE_MIND: {
            "properties": {
                "method": {"const": "POST"},
                "path": {"const": _MINDS_CREATE_AGENT_PATH},
            },
            "required": ["method", "path"],
        },
        _PERM_MIND_STATUS: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"type": "string", "pattern": _MINDS_STATUS_PATH_PATTERN},
            },
            "required": ["method", "path"],
        },
        _PERM_MIND_LOGS: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"type": "string", "pattern": _MINDS_LOGS_PATH_PATTERN},
            },
            "required": ["method", "path"],
        },
    },
)


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


_MINDS_SCHEMA_KEYS: Final[tuple[str, ...]] = (
    _SCOPE_MINDS,
    _PERM_CREATE_MIND,
    _PERM_MIND_STATUS,
    _PERM_MIND_LOGS,
)


def ensure_minds_schema_in_existing_host_files(plugin_data_dir: Path) -> int:
    """Inject the ``minds`` scope + permission schemas into existing host permission files.

    Agents created before this feature shipped have a host
    ``latchkey_permissions.json`` that lacks the ``minds`` scope and
    its named permission schemas. Without those schemas, detent cannot
    match a ``{"minds": [...]}`` rule that the user grants via the
    dialog, so peer-mind requests would be silently denied.

    This idempotent migration walks every
    ``<plugin_data_dir>/hosts/<host_id>/latchkey_permissions.json``,
    parses it, and rewrites it whenever any of the four ``minds`` schema
    keys is missing or stale. Files whose schemas already match are
    skipped. Files that cannot be parsed as a
    :class:`LatchkeyPermissionsConfig` (malformed JSON, or a shape that
    fails model validation -- e.g. a non-dict ``schemas`` block) are
    logged at warning level and left untouched.

    Must run *before* the shared gateway starts so the gateway's
    ``permissions.mjs`` extension does not race with us. Returns the
    number of files actually rewritten.
    """
    hosts_dir = plugin_data_dir / "hosts"
    if not hosts_dir.is_dir():
        return 0
    expected_schemas = {key: _AGENT_BASELINE_PERMISSIONS.schemas[key] for key in _MINDS_SCHEMA_KEYS}
    migrated = 0
    for host_dir in hosts_dir.iterdir():
        if not host_dir.is_dir():
            continue
        path = host_dir / "latchkey_permissions.json"
        if not path.is_file():
            continue
        try:
            raw = path.read_text()
        except OSError as e:
            logger.warning("Could not read {} during minds schema migration: {}", path, e)
            continue
        try:
            parsed = LatchkeyPermissionsConfig.model_validate_json(raw)
        except ValueError as e:
            logger.warning(
                "Skipping {} during minds schema migration; cannot parse as LatchkeyPermissionsConfig: {}",
                path,
                e,
            )
            continue
        if all(parsed.schemas.get(key) == expected_schemas[key] for key in _MINDS_SCHEMA_KEYS):
            continue
        new_schemas = {**parsed.schemas, **expected_schemas}
        updated = parsed.model_copy_update(to_update(parsed.field_ref().schemas, new_schemas))
        save_permissions(path, updated)
        migrated += 1
    return migrated
