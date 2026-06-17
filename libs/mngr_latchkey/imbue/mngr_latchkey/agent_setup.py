"""High-level helpers for wiring latchkey into a freshly-created agent.

The lifecycle for a new agent has three latchkey-aware steps:

1. *Before* ``mngr create``: allocate an opaque permissions handle,
   materialize it with a deny-all baseline, mint a permissions-override
   JWT pointing at it, and assemble the env vars the agent needs
   (``LATCHKEY_GATEWAY``, ``LATCHKEY_GATEWAY_SECONDARY``,
   ``LATCHKEY_GATEWAY_PASSWORD``,
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
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.remote_gateway import INNER_PORT
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import point_opaque_handle_at_host
from imbue.mngr_latchkey.store import save_permissions

# Env-var names baked into the upstream latchkey CLI's wire contract.
# Kept as constants so callers building ``--env`` flags do not have to repeat them.
ENV_LATCHKEY_GATEWAY: Final[str] = "LATCHKEY_GATEWAY"
# URL of the per-VPS "secondary" gateway as seen from *inside* the agent's
# workspace container: the reverse tunnel set up at discovery time binds the
# VPS-resident gateway onto the container's ``127.0.0.1:INNER_PORT``. Only live
# for genuinely-remote (VPS-backed) hosts, but the URL is the agent's view
# either way.
ENV_LATCHKEY_GATEWAY_SECONDARY: Final[str] = "LATCHKEY_GATEWAY_SECONDARY"
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

# Paths under this prefix are only allowed if the agent ID in the path is in the allow list (expressed via anyOf below).
_MINDS_API_PROXY_PER_AGENT_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/agents/"
_MINDS_API_PROXY_PER_AGENT_PATH_PATTERN: Final[str] = rf"^{_MINDS_API_PROXY_PER_AGENT_PATH_PREFIX}[^/]+(/.*)?$"

_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: Final[str] = "minds-api-proxy-per-agent-unauthorized"
_PERM_MINDS_API_PROXY_PER_AGENT: Final[str] = "minds-api-proxy-per-agent"

# Exact prefix/suffix wrapping each agent id inside an ``anyOf`` entry's
# path pattern. Shared by the build + extract helpers so the two cannot
# drift apart.
_ALLOWED_AGENT_PATTERN_PREFIX: Final[str] = rf"^{_MINDS_API_PROXY_PER_AGENT_PATH_PREFIX}"
_ALLOWED_AGENT_PATTERN_SUFFIX: Final[str] = "/(.*)$"

# Characters allowed verbatim in an ``agent_id`` when we embed it into
# a regex pattern body. mngr's ``RandomId`` format -- ``<prefix>-<32
# hex>`` -- only uses ``[a-z0-9-]``, all of which are regex-safe
# outside a character class. We validate explicitly (rather than
# trusting the caller's typing) so a future id-shape change cannot
# silently inject regex metacharacters into the on-disk pattern.
_SAFE_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def _build_allowed_agent_anyof_entry(agent_id: str) -> dict[str, JsonValue]:
    """Build the ``anyOf`` entry that allows ``agent_id`` past the unauthorized scope."""
    if not _SAFE_AGENT_ID_RE.match(agent_id):
        raise LatchkeyStoreError(f"agent_id contains characters that are not safe to embed in a regex: {agent_id!r}")
    return {"pattern": f"{_ALLOWED_AGENT_PATTERN_PREFIX}{agent_id}{_ALLOWED_AGENT_PATTERN_SUFFIX}"}


def _extract_agent_id_from_anyof_entry(entry: JsonValue) -> str:
    """Recover the agent id encoded into one ``anyOf`` entry."""
    if not isinstance(entry, dict):
        raise LatchkeyStoreError(f"Allowed-agent ``anyOf`` entry must be a JSON object; got: {entry!r}")
    pattern = entry.get("pattern")
    if not isinstance(pattern, str):
        raise LatchkeyStoreError(f"Allowed-agent ``anyOf`` entry is missing or has non-string ``pattern``: {entry!r}")
    if not (pattern.startswith(_ALLOWED_AGENT_PATTERN_PREFIX) and pattern.endswith(_ALLOWED_AGENT_PATTERN_SUFFIX)):
        raise LatchkeyStoreError(
            f"Unrecognized allowed-agent ``anyOf`` entry pattern; refusing to overwrite a "
            f"hand-edited permissions file. Got: {pattern!r}"
        )
    return pattern.removeprefix(_ALLOWED_AGENT_PATTERN_PREFIX).removesuffix(_ALLOWED_AGENT_PATTERN_SUFFIX)


_AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        # Unauthorized agents trying to access agent-scoped Minds API endpoint get an empty list of permissions, leading to immediate rejection.
        {_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: []},
        {
            _SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
                # Requests that made it through the first rule (= not unauthorized agents) can now access the agent-scoped Minds API endpoint.
                _PERM_MINDS_API_PROXY_PER_AGENT,
            ],
        },
    ),
    schemas={
        _SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: {
            "properties": {
                "domain": {"const": _GATEWAY_SELF_HOST},
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_PER_AGENT_PATH_PATTERN,
                    # As we create agents running on the host whose permissions
                    # file this is, we'll add their IDs to the list below, thus
                    # excluding them from the unauthorized rejection shortcut.
                    "not": {"anyOf": []},
                },
            },
            "required": ["domain", "path"],
        },
        _PERM_MINDS_API_PROXY_PER_AGENT: {
            "properties": {
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_PER_AGENT_PATH_PATTERN,
                },
            },
            "required": ["path"],
        },
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
    },
)


def register_agent_for_host(
    plugin_data_dir: Path,
    host_id: HostId,
    agent_id: AgentId,
) -> None:
    """Register ``agent_id`` for the given host, granting it access to the Minds API proxy.

    Reads the host's ``latchkey_permissions.json`` (writing a fresh
    baseline if it does not yet exist), extracts the current allowed-agent
    list out of the ``minds-api-proxy-unauthorized`` scope's ``not.anyOf``
    block, appends a per-agent entry if ``agent_id`` is not already there,
    and writes the updated config back atomically. Idempotent:
    re-registering an agent already in the list is a no-op write.

    This is the *only* public way to grant a minds agent access to the
    Minds API proxy. The matching CLI wrapper is ``mngr latchkey
    register-agent --host-id ID --agent-id ID``; the desktop client and
    any other Python caller goes through this function directly.

    """
    path = permissions_path_for_host(plugin_data_dir, host_id)
    if path.is_file():
        config = load_permissions(path)
    else:
        # First agent on this host: start from the baseline so the
        # gateway-self rules and the minds-api-proxy gate are present.
        config = _AGENT_BASELINE_PERMISSIONS

    schemas: dict[str, JsonValue] = dict(config.schemas)
    scope_schema = schemas.get(_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED)
    # Use ``dict`` (concrete type) rather than ``Mapping`` because
    # ``JsonValue``'s mapping arm is ``dict[str, JsonValue]``
    # specifically; ``isinstance(x, Mapping)`` lets the type checker
    # narrow to ``Mapping[Unknown, Unknown]`` which then can't be
    # subscripted with a ``str`` key.
    if not isinstance(scope_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path} is missing the "
            f"{_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED!r} scope schema; cannot extend the allowed-agent list."
        )
    properties = scope_schema.get("properties")
    if not isinstance(properties, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties``."
        )
    path_schema = properties.get("path")
    if not isinstance(path_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties.path``."
        )
    not_block = path_schema.get("not")
    if not isinstance(not_block, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties.path.not``."
        )
    any_of = not_block.get("anyOf")
    if not isinstance(any_of, list):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-list ``properties.path.not.anyOf``."
        )

    existing_ids = [_extract_agent_id_from_anyof_entry(entry) for entry in any_of]
    if str(agent_id) in existing_ids:
        # No-op: agent already allowed.
        return
    new_any_of: list[JsonValue] = list(any_of) + [_build_allowed_agent_anyof_entry(str(agent_id))]

    schemas[_SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED] = {
        **scope_schema,
        "properties": {
            **properties,
            "path": {
                **path_schema,
                "not": {
                    **not_block,
                    "anyOf": new_any_of,
                },
            },
        },
    }
    new_config = LatchkeyPermissionsConfig(rules=config.rules, schemas=schemas)
    save_permissions(path, new_config)


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
            "whenever a gateway URL is available (plus "
            f"``{ENV_LATCHKEY_GATEWAY_SECONDARY}`` in tunneled mode), plus "
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
    if is_tunneled:
        # Tunneled agents (containers/VMs/VPS) also get the secondary VPS-gateway
        # URL: when the host turns out to be a remote VPS, the discovery handler
        # reverse-tunnels that gateway onto the container's ``127.0.0.1:INNER_PORT``.
        # For tunneled hosts that aren't VPS-backed nothing listens there, but the
        # URL is the agent's view either way and consumers treat it as optional.
        env[ENV_LATCHKEY_GATEWAY_SECONDARY] = f"http://127.0.0.1:{INNER_PORT}"
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


def maybe_recover_host_permissions_for_agent(
    latchkey: Latchkey,
    host_id: HostId,
    agent_id: AgentId,
    opaque_permissions_path: Path,
) -> bool:
    """Repair a host's permissions file (and re-register ``agent_id``) when needed.

    Cheap in the common case: when the canonical file already exists this
    only re-registers the agent (an idempotent read, usually a no-op),
    which is why the name carries the ``maybe_`` hedge -- the expensive
    link / write paths run only on the rare missing-file case.

    A best-effort safety net for the rare case where an agent is live and
    able to file permission requests (its gateway JWT resolves to the
    opaque handle ``opaque_permissions_path``) but the canonical
    host-keyed permissions file was never materialized -- e.g. agent
    creation's :func:`finalize_host_permissions` step was skipped or
    failed. In that state a UI-driven grant would write to a non-existent
    ``hosts/<host_id>/`` directory, and even once that directory exists
    the agent would not see grants written there because its JWT still
    points at the standalone opaque handle.

    Two repairs happen here:

    1. If the canonical permissions file is missing, it is materialized
       from the opaque handle (moving the deny-all baseline into the
       canonical path and swinging the handle to a symlink pointing at
       it, via :func:`finalize_host_permissions`).
    2. ``agent_id`` is (idempotently) registered for the host via
       :func:`register_agent_for_host`. This always runs -- even when the
       file already existed -- to close the gap where the discovery-time
       auto-register saw this agent while the host file was missing: it
       skips (and de-dups) such agents, so they would otherwise never get
       added to the host's ``minds-api-proxy`` allowlist.

    ``opaque_permissions_path`` is the path the agent's
    permissions-override JWT resolves to; minds reads it from the
    gateway-streamed permission request's ``target`` field. It must live
    under the plugin's opaque permissions directory (the only place this
    function is willing to move from / symlink), otherwise
    :class:`LatchkeyStoreError` is raised.

    Returns ``True`` if the canonical file had to be materialized,
    ``False`` if it already existed (the common case -- a cheap check on
    the hot path). The return value reflects only the file repair;
    registration is idempotent and runs in both cases.

    Raises:
        LatchkeyStoreError: if ``opaque_permissions_path`` is outside the
            opaque permissions directory, or the underlying link / write /
            registration fails.
    """
    plugin_data_dir = latchkey.plugin_data_dir
    host_path = permissions_path_for_host(plugin_data_dir, host_id)
    # ``is_file`` follows symlinks, so a finalized opaque->host symlink whose
    # target exists also counts as "already present" and needs no file repair.
    did_repair = False
    if not host_path.is_file():
        opaque_root = opaque_permissions_dir(plugin_data_dir)
        if opaque_permissions_path.parent != opaque_root:
            raise LatchkeyStoreError(
                f"Refusing to recover host {host_id} permissions from {opaque_permissions_path}: "
                f"it is not under the opaque permissions directory {opaque_root}."
            )
        is_standalone_handle = opaque_permissions_path.is_file() and not opaque_permissions_path.is_symlink()
        if is_standalone_handle:
            # The common recovery: the opaque handle is the deny-all baseline
            # written at agent-creation time. ``finalize_host_permissions`` moves
            # those baseline rules to the canonical host path and swings the
            # opaque handle to a symlink pointing at it, so subsequent grants the
            # UI writes to the canonical path are visible to the agent.
            finalize_host_permissions(latchkey, opaque_permissions_path, host_id)
        else:
            # The opaque handle is missing or is a (dangling) symlink -- both are
            # unexpected for a request the gateway just accepted. Materialize
            # the baseline at the canonical path and (re)point the opaque
            # handle at it, so the agent's JWT (which resolves to the handle)
            # starts working again and later grants are visible to it.
            save_permissions(host_path, _AGENT_BASELINE_PERMISSIONS)
            point_opaque_handle_at_host(plugin_data_dir, opaque_permissions_path, host_id)
        did_repair = True

    # Always ensure the requesting agent is in the host's allowlist. This is a
    # no-op when it already is, and repairs the auto-register de-dup gap when
    # it is not.
    register_agent_for_host(plugin_data_dir, host_id, agent_id)
    return did_repair
