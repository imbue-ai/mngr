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
# Two rules in :data:`_AGENT_BASELINE_PERMISSIONS` cooperate to gate
# every ``/minds-api-proxy/api/v1/agents/<id>/...`` request:
#
# 1. The FIRST rule's scope matches any such request whose ``<id>`` is
#    NOT in the allowed list (encoded as ``not + anyOf`` on the path).
#    Its permission list is empty, so detent rejects the request
#    immediately when the scope fires -- and detent stops at the first
#    matching scope, so the rule below never gets a chance to allow it.
# 2. The SECOND rule (the existing gateway-self baseline) carries a
#    generic ``minds-api-proxy`` permission that matches every path
#    under the proxy prefix without enumerating ids. Authorized agents
#    fall through to this rule (their id is in the allow list, so the
#    first rule's ``not + anyOf`` doesn't fire) and the generic
#    permission lets them through.
#
# The source-of-truth list of allowed agent ids is the ``anyOf`` array
# inside the first rule's scope schema -- a plain JSON list of
# per-agent path-pattern objects, populated by :func:`allow_agent_for_host`
# (or its CLI wrapper, ``mngr latchkey allow-agent``).
_MINDS_API_PROXY_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/agents/"
_SCOPE_MINDS_API_PROXY_UNAUTHORIZED: Final[str] = "minds-api-proxy-unauthorized"
_PERM_MINDS_API_PROXY: Final[str] = "minds-api-proxy"

# Path-prefix pattern shared by the unauthorized scope ("any request
# under the proxy's agents subtree") and the authorized-side
# permission ("any path under the proxy's agents subtree").
_MINDS_API_PROXY_PATH_PATTERN: Final[str] = rf"^{_MINDS_API_PROXY_PATH_PREFIX}[^/]+(/.*)?$"

# Characters allowed verbatim in an ``agent_id`` when we embed it into
# a regex pattern body. mngr's ``RandomId`` format -- ``<prefix>-<32
# hex>`` -- only uses ``[a-z0-9-]``, all of which are regex-safe
# outside a character class. We validate explicitly (rather than
# trusting the caller's typing) so a future id-shape change cannot
# silently inject regex metacharacters into the on-disk pattern.
_SAFE_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")

# Anchored regex that extracts an agent id from one ``anyOf`` entry's
# path pattern. Each entry that :func:`_build_allowed_agent_anyof_entry`
# writes has the form
# ``{"pattern": "^/minds-api-proxy/api/v1/agents/<id>(/.*)?$"}``;
# this regex recovers ``<id>``.
_ALLOWED_AGENT_ENTRY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\^/minds-api-proxy/api/v1/agents/(?P<id>[A-Za-z0-9_\-]+)\(/\.\*\)\?\$$"
)


def _build_allowed_agent_anyof_entry(agent_id: str) -> dict[str, JsonValue]:
    r"""Build the ``anyOf`` entry that allows ``agent_id`` past the unauthorized scope.

    The entry's pattern matches any path of the form
    ``/minds-api-proxy/api/v1/agents/<agent_id>(/.*)?``. We validate
    that ``agent_id`` only contains regex-safe characters and embed it
    verbatim instead of going through :func:`re.escape` -- the escape
    would turn ``-`` (regex-safe in the body of a pattern) into ``\-``,
    breaking the symmetric :func:`_extract_agent_id_from_anyof_entry`
    that recovers the id back out.
    """
    if not _SAFE_AGENT_ID_RE.match(agent_id):
        raise LatchkeyStoreError(f"agent_id contains characters that are not safe to embed in a regex: {agent_id!r}")
    return {"pattern": rf"^{_MINDS_API_PROXY_PATH_PREFIX}{agent_id}(/.*)?$"}


def _extract_agent_id_from_anyof_entry(entry: JsonValue) -> str:
    """Recover the agent id encoded into one ``anyOf`` entry.

    Raises :class:`LatchkeyStoreError` if ``entry`` is not the
    ``{"pattern": ...}`` shape this module writes, or if its pattern
    does not match the format :func:`_build_allowed_agent_anyof_entry`
    produces. Callers should treat that as "the permissions file was
    hand-edited into a shape we cannot safely extend" and surface the
    error rather than silently rebuilding the list from scratch (which
    would discard the hand-edit).
    """
    if not isinstance(entry, dict):
        raise LatchkeyStoreError(f"Allowed-agent ``anyOf`` entry must be a JSON object; got: {entry!r}")
    pattern = entry.get("pattern")
    if not isinstance(pattern, str):
        raise LatchkeyStoreError(f"Allowed-agent ``anyOf`` entry is missing or has non-string ``pattern``: {entry!r}")
    match = _ALLOWED_AGENT_ENTRY_RE.match(pattern)
    if match is None:
        raise LatchkeyStoreError(
            "Unrecognized allowed-agent ``anyOf`` entry pattern; refusing to overwrite a "
            f"hand-edited permissions file. Got: {pattern!r}"
        )
    return match.group("id")


_AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        # First rule: reject minds-api-proxy requests for unauthorized
        # agent ids. The scope's path schema combines the proxy-prefix
        # ``pattern`` with a ``not + anyOf`` whose entries are the
        # per-agent path patterns of *allowed* ids (initially none --
        # no agent allowed). When the agent id is in the allow list,
        # one of the anyOf entries matches, the ``not`` is false, the
        # combined path schema fails, and the rule's scope doesn't
        # fire -- so detent moves on to the next rule. When the agent
        # id is NOT in the allow list, none of the anyOf entries
        # matches, the ``not`` is true, the path schema passes, the
        # scope fires, and the empty permission list causes an
        # immediate reject (with no fall-through to subsequent rules).
        {_SCOPE_MINDS_API_PROXY_UNAUTHORIZED: []},
        # Second rule: gateway-self endpoints + a generic
        # ``minds-api-proxy`` permission that lets *any* path under the
        # proxy's ``/agents/<id>/`` subtree through. Authorized agents
        # only ever reach this rule after the first one's
        # ``not + anyOf`` filtered them past; unauthorized agents are
        # already rejected by Rule 1 and never get here.
        {
            _SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
                _PERM_MINDS_API_PROXY,
            ],
        },
    ),
    schemas={
        _SCOPE_MINDS_API_PROXY_UNAUTHORIZED: {
            "properties": {
                "domain": {"const": _GATEWAY_SELF_HOST},
                # ``pattern`` (must-match-prefix) and ``not + anyOf``
                # (must-not-match-any-allowed-agent) at the same level
                # combine with implicit AND, per JSON Schema. The
                # ``anyOf`` list is the per-host source of truth for
                # which agent ids are allowed -- empty at first, grown
                # by :func:`allow_agent_for_host`.
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_PATH_PATTERN,
                    "not": {"anyOf": []},
                },
            },
            "required": ["domain", "path"],
        },
        _PERM_MINDS_API_PROXY: {
            "properties": {
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_PATH_PATTERN,
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
    """Add ``agent_id`` to the host's allowed-agent list on the Minds API proxy.

    Reads the host's ``latchkey_permissions.json`` (writing a fresh
    baseline if it does not yet exist), extracts the current allowed-agent
    list out of the ``minds-api-proxy-unauthorized`` scope's ``not.anyOf``
    block, appends a per-agent entry if ``agent_id`` is not already there,
    and writes the updated config back atomically. Idempotent: re-allowing
    an agent already in the list is a no-op write.

    This is the *only* public way to grant a minds agent access to the
    Minds API proxy. The matching CLI wrapper is ``mngr latchkey
    allow-agent --host-id ID --agent-id ID``; the desktop client and any
    other Python caller goes through this function directly.

    Raises :class:`LatchkeyStoreError` if the on-disk permissions file
    has been hand-edited into a shape we cannot safely extend (an
    ``anyOf`` entry doesn't match the format this module writes); callers
    should surface the error and let the operator fix the file manually
    rather than overwrite an arbitrary edit.
    """
    path = permissions_path_for_host(plugin_data_dir, host_id)
    if path.is_file():
        config = load_permissions(path)
    else:
        # First agent on this host: start from the baseline so the
        # gateway-self rules and the minds-api-proxy gate are present.
        config = _AGENT_BASELINE_PERMISSIONS

    schemas: dict[str, JsonValue] = dict(config.schemas)
    scope_schema = schemas.get(_SCOPE_MINDS_API_PROXY_UNAUTHORIZED)
    # Use ``dict`` (concrete type) rather than ``Mapping`` because
    # ``JsonValue``'s mapping arm is ``dict[str, JsonValue]``
    # specifically; ``isinstance(x, Mapping)`` lets the type checker
    # narrow to ``Mapping[Unknown, Unknown]`` which then can't be
    # subscripted with a ``str`` key.
    if not isinstance(scope_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path} is missing the "
            f"{_SCOPE_MINDS_API_PROXY_UNAUTHORIZED!r} scope schema; cannot extend the allowed-agent list."
        )
    properties = scope_schema.get("properties")
    if not isinstance(properties, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties``."
        )
    path_schema = properties.get("path")
    if not isinstance(path_schema, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties.path``."
        )
    not_block = path_schema.get("not")
    if not isinstance(not_block, dict):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-object ``properties.path.not``."
        )
    any_of = not_block.get("anyOf")
    if not isinstance(any_of, list):
        raise LatchkeyStoreError(
            f"Permissions file {path}'s {_SCOPE_MINDS_API_PROXY_UNAUTHORIZED!r} schema is malformed: "
            f"missing or non-list ``properties.path.not.anyOf``."
        )

    existing_ids = [_extract_agent_id_from_anyof_entry(entry) for entry in any_of]
    if str(agent_id) in existing_ids:
        # No-op: agent already allowed.
        return
    new_any_of: list[JsonValue] = list(any_of) + [_build_allowed_agent_anyof_entry(str(agent_id))]

    # Build the updated schema body by copy-on-write so we do not
    # mutate the input config in place (it might be the shared baseline
    # constant). The explicit ``dict[str, JsonValue]`` / ``list[JsonValue]``
    # annotations keep the type checker happy as we rebuild the nested
    # structure.
    new_not_block: dict[str, JsonValue] = dict(not_block)
    new_not_block["anyOf"] = new_any_of
    new_path_schema: dict[str, JsonValue] = dict(path_schema)
    new_path_schema["not"] = new_not_block
    new_properties: dict[str, JsonValue] = dict(properties)
    new_properties["path"] = new_path_schema
    new_scope_schema: dict[str, JsonValue] = dict(scope_schema)
    new_scope_schema["properties"] = new_properties
    schemas[_SCOPE_MINDS_API_PROXY_UNAUTHORIZED] = new_scope_schema
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
