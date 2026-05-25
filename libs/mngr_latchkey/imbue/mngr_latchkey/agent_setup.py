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
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import link_opaque_permissions_to_host
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import new_opaque_permissions_path
from imbue.mngr_latchkey.store import permissions_path_for_host
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

# Detent schema names + URL prefix the Minds API proxy lives under.
# The first rule in :data:`_AGENT_BASELINE_PERMISSIONS` uses these two
# schemas to gate every ``/minds-api-proxy/api/v1/agents/<id>/...``
# request: the scope schema matches *any* such request, and the single
# permission schema's path pattern constrains the ``<id>`` segment to a
# regex-alternation enum of allowed agent ids (initially empty -- no
# agent allowed). New agents are added via :func:`allow_agent_for_host`
# (or its CLI wrapper, ``mngr latchkey allow-agent``).
#
# Because detent evaluates rules top-to-bottom and stops at the first
# rule whose scope matches, this guarantees that an unauthorized
# agent_id is rejected by the first rule and never accidentally inherits
# any subsequent rule's grant. (The remaining baseline rules are scoped
# to ``/permission-requests``, ``/permissions/self``, etc., so they
# don't match minds-api-proxy paths and don't interfere.)
_MINDS_API_PROXY_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/agents/"
_SCOPE_MINDS_API_PROXY: Final[str] = "minds-api-proxy"
_PERM_MINDS_API_PROXY_ALLOWED_AGENT: Final[str] = "minds-api-proxy-allowed-agent"

# Scope schema's path pattern. Matches any request under the proxy's
# ``/api/v1/agents/<id>/...`` subtree (with or without a trailing
# segment), so the rule that uses this scope fires for every such
# request regardless of method or endpoint.
_MINDS_API_PROXY_SCOPE_PATH_PATTERN: Final[str] = rf"^{_MINDS_API_PROXY_PATH_PREFIX}[^/]+(/.*)?$"

# Sentinel that ``_build_allowed_agent_path_pattern`` writes when the
# enum is empty. ``(?!)`` is a negative-empty-lookahead -- it matches no
# string of any length, so the resulting path pattern rejects every
# request that gets past the scope schema. Encoded as a stand-alone
# constant so the round-trip ``build -> parse -> build`` is provably
# stable even with no ids in the list.
_ALLOWED_AGENT_EMPTY_SENTINEL: Final[str] = "(?!)"

# Anchored regex that parses a populated allowed-agent path pattern
# back into the list of agent ids encoded into its alternation group.
# Agent ids are validated to ``[A-Za-z0-9_-]+`` (mngr's ``RandomId``
# format: ``<prefix>-<32 hex>``) so the alternation body is restricted
# to those characters plus the ``|`` separator. The non-capturing
# ``(?:...)`` wrapper around the alternation is part of the format
# this module writes; tolerating a wider grammar invites the file's
# regex to drift from what the writer produces. The empty sentinel is
# handled separately as a whole-string equality check below.
_ALLOWED_AGENT_PATTERN_RE: Final[re.Pattern[str]] = re.compile(
    r"^\^/minds-api-proxy/api/v1/agents/\(\?:(?P<body>[A-Za-z0-9_\-|]+)\)\(/\.\*\)\?\$$"
)


def _empty_allowed_agent_path_pattern() -> str:
    """Whole-string form of the path pattern when the allowed-agent enum is empty."""
    return rf"^/minds-api-proxy/api/v1/agents/(?:{_ALLOWED_AGENT_EMPTY_SENTINEL})(/.*)?$"


def _build_allowed_agent_path_pattern(agent_ids: tuple[str, ...]) -> str:
    """Build the path-pattern regex that gates the ``minds-api-proxy`` permission.

    Format: ``^/minds-api-proxy/api/v1/agents/(?:<id1>|<id2>|...|<idN>)(/.*)?$``
    when ``agent_ids`` is non-empty, or
    ``^/minds-api-proxy/api/v1/agents/(?:(?!))(/.*)?$`` when it is empty.
    The empty form uses a negative-empty-lookahead inside the
    non-capturing group so the surrounding regex shape stays uniform
    (which keeps the round-trip parser simple) while still matching no
    real agent id.
    """
    if agent_ids:
        body = "|".join(agent_ids)
    else:
        body = _ALLOWED_AGENT_EMPTY_SENTINEL
    return rf"^/minds-api-proxy/api/v1/agents/(?:{body})(/.*)?$"


def _parse_allowed_agent_path_pattern(pattern: str) -> tuple[str, ...]:
    """Inverse of :func:`_build_allowed_agent_path_pattern`.

    Raises :class:`LatchkeyStoreError` if ``pattern`` does not match the
    format this module writes. Callers should treat that as "the
    permissions file was hand-edited into a shape we cannot safely
    extend" and surface the error rather than silently rebuilding the
    list from scratch (which would discard the hand-edit).
    """
    # The empty form contains a ``(?!)`` whose embedded ``)`` upsets the
    # ``[A-Za-z0-9_\-|]+`` body regex; check for it as a whole-string
    # equality first.
    if pattern == _empty_allowed_agent_path_pattern():
        return ()
    match = _ALLOWED_AGENT_PATTERN_RE.match(pattern)
    if match is None:
        raise LatchkeyStoreError(
            f"Unrecognized {_PERM_MINDS_API_PROXY_ALLOWED_AGENT!r} path pattern; "
            "refusing to overwrite a hand-edited permissions file. "
            f"Got: {pattern!r}"
        )
    body = match.group("body")
    return tuple(body.split("|"))


_AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        # First rule: gate the Minds API proxy. The scope schema
        # matches every ``/minds-api-proxy/api/v1/agents/<id>/...``
        # request, and the single permission schema constrains <id>
        # to the allowed-agent enum (initially empty -- no agent
        # allowed). Because detent stops at the first matching
        # scope, an unauthorized agent_id is rejected here and does
        # NOT fall through to the gateway-self rule below.
        {_SCOPE_MINDS_API_PROXY: [_PERM_MINDS_API_PROXY_ALLOWED_AGENT]},
        # Remaining rules: gateway-self endpoints (permission requests,
        # self-permissions read, services catalog). These scopes do NOT
        # match minds-api-proxy paths, so they are reached only by
        # requests that targeted ``/permission-requests`` /
        # ``/permissions/self`` / ``/permissions/available/<service>``
        # in the first place.
        {
            _SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
            ],
        },
    ),
    schemas={
        _SCOPE_MINDS_API_PROXY: {
            "properties": {
                "domain": {"const": _GATEWAY_SELF_HOST},
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_SCOPE_PATH_PATTERN,
                },
            },
            "required": ["domain", "path"],
        },
        _PERM_MINDS_API_PROXY_ALLOWED_AGENT: {
            "properties": {
                "path": {
                    "type": "string",
                    "pattern": _build_allowed_agent_path_pattern(()),
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


def allow_agent_for_host(
    plugin_data_dir: Path,
    host_id: HostId,
    agent_id: AgentId,
) -> None:
    """Add ``agent_id`` to the host's allowed-agent enum on the Minds API proxy.

    Reads the host's ``latchkey_permissions.json`` (writing a fresh baseline
    if it does not yet exist), parses the current allowed-agent list out of
    the ``minds-api-proxy-allowed-agent`` schema's path pattern, appends
    ``agent_id`` if it is not already there, deduplicates + sorts, and
    writes the updated config back atomically. Idempotent: re-allowing an
    agent already in the list is a no-op write.

    This is the *only* public way to grant a minds agent access to the
    Minds API proxy. The matching CLI wrapper is ``mngr latchkey
    allow-agent --host-id ID --agent-id ID``; the desktop client and any
    other Python caller goes through this function directly. The previous
    per-agent rule/schema dance and the corresponding low-level
    ``POST /permissions/schemas`` gateway-extension endpoint are gone --
    the only knob to turn is the allowed-agent enum.

    Raises :class:`LatchkeyStoreError` if the on-disk permissions file
    has been hand-edited into a shape we cannot safely extend (the
    parse of the existing pattern fails); callers should surface the
    error and let the operator fix the file manually rather than
    overwrite an arbitrary edit.
    """
    path = permissions_path_for_host(plugin_data_dir, host_id)
    if path.is_file():
        config = load_permissions(path)
    else:
        # First agent on this host: start from the baseline so the
        # gateway-self rules and the minds-api-proxy gate are present.
        config = _AGENT_BASELINE_PERMISSIONS

    schemas: dict[str, JsonValue] = dict(config.schemas)
    permission_schema = schemas.get(_PERM_MINDS_API_PROXY_ALLOWED_AGENT)
    # Use ``dict`` (concrete type) rather than ``Mapping`` because
    # ``JsonValue``'s mapping arm is ``dict[str, JsonValue]``
    # specifically; ``isinstance(x, Mapping)`` lets the type checker
    # narrow to ``Mapping[Unknown, Unknown]`` which then can't be
    # subscripted with a ``str`` key.
    if not isinstance(permission_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path} is missing the "
            f"{_PERM_MINDS_API_PROXY_ALLOWED_AGENT!r} schema; cannot extend the allowed-agent enum."
        )
    properties = permission_schema.get("properties")
    if not isinstance(properties, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_PERM_MINDS_API_PROXY_ALLOWED_AGENT!r} schema is malformed: "
            f"missing or non-object ``properties``."
        )
    path_schema = properties.get("path")
    if not isinstance(path_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_PERM_MINDS_API_PROXY_ALLOWED_AGENT!r} schema is malformed: "
            f"missing or non-object ``properties.path``."
        )
    pattern = path_schema.get("pattern")
    if not isinstance(pattern, str):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_PERM_MINDS_API_PROXY_ALLOWED_AGENT!r} schema is malformed: "
            f"missing or non-string ``properties.path.pattern``."
        )

    existing_ids = _parse_allowed_agent_path_pattern(pattern)
    if str(agent_id) in existing_ids:
        # No-op: agent already allowed.
        return
    updated_ids = tuple(sorted(set(existing_ids) | {str(agent_id)}))
    new_pattern = _build_allowed_agent_path_pattern(updated_ids)

    # Build the updated schema body by copy-on-write so we do not
    # mutate the input config in place (it might be the shared baseline
    # constant). The explicit ``dict[str, JsonValue]`` annotations
    # keep the type checker happy as we rebuild the nested structure.
    new_path_schema: dict[str, JsonValue] = dict(path_schema)
    new_path_schema["pattern"] = new_pattern
    new_properties: dict[str, JsonValue] = dict(properties)
    new_properties["path"] = new_path_schema
    new_permission_schema: dict[str, JsonValue] = dict(permission_schema)
    new_permission_schema["properties"] = new_properties
    schemas[_PERM_MINDS_API_PROXY_ALLOWED_AGENT] = new_permission_schema
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
