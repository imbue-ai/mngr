"""The permissions file a freshly-created host starts life with.

Every host that minds creates gets a ``latchkey_permissions.json`` seeded from
:data:`AGENT_BASELINE_PERMISSIONS`: the gateway-self endpoints every agent may
reach (filing a permission request, reading its own permissions, browsing the
catalog, the Minds API schema), plus the per-agent Minds API proxy gate that
:func:`imbue.mngr_latchkey.agent_setup.register_agent_for_host` later extends
with each agent registered on the host.

It lives in its own module -- rather than inside ``agent_setup`` where it is
used -- so that code which must not depend on the agent-creation machinery (in
particular the data-format migrations, which ``core`` imports and which
``agent_setup`` transitively imports back) can still ask "what does a
freshly-created host's permissions file look like?".
"""

from typing import Final

from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import SHARED_SCHEMAS_FILENAME

# Detent schema names and host string for the gateway-self baseline that
# every agent inherits. Defined inline (in the agent's permissions file)
# rather than relying on detent's built-in catalog so the names are
# self-contained and the grant is exactly the endpoints we want.
_GATEWAY_SELF_HOST: Final[str] = "latchkey-self.invalid"
SCOPE_LATCHKEY_SELF: Final[str] = "latchkey-self"
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
MINDS_API_PROXY_PER_AGENT_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/agents/"
_MINDS_API_PROXY_PER_AGENT_PATH_PATTERN: Final[str] = rf"^{MINDS_API_PROXY_PER_AGENT_PATH_PREFIX}[^/]+(/.*)?$"

SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: Final[str] = "minds-api-proxy-per-agent-unauthorized"
_PERM_MINDS_API_PROXY_PER_AGENT: Final[str] = "minds-api-proxy-per-agent"

# The per-agent bug-report route is reachable by ANY in-workspace agent without
# per-agent registration. An agent escalates a bug report by POSTing its
# diagnosis here; the effect is that the desktop app pops the report-a-bug modal
# pre-filled for a human to review and submit (the agent never sends to Sentry
# itself). Because detent stops at the first matching scope, a rule allowing this
# exact path must come before the unauthorized gate below. This is an interim
# bypass pending the broader minds-API-surface latchkey work; the route's
# bearer-key auth still applies, so only requests that came through the gateway
# reach it.
_MINDS_API_PROXY_REPORT_PATH_PATTERN: Final[str] = rf"^{MINDS_API_PROXY_PER_AGENT_PATH_PREFIX}[^/]+/report$"
_SCOPE_MINDS_API_PROXY_REPORT: Final[str] = "minds-api-proxy-report"
_PERM_MINDS_API_PROXY_REPORT: Final[str] = "minds-api-proxy-report-allow"

# The version-agnostic, read-only API schema endpoint (an OpenAPI document
# describing every gateway-reachable ``/api/v*`` route). Granted to every agent
# by default -- unlike the per-agent endpoints it is not agent-scoped (the schema
# is identical for all callers) and carries no per-target data, so a workspace
# can always discover the Minds API surface. This is the *inbound* path the
# gateway matches on; the proxy strips ``/minds-api-proxy`` before forwarding to
# the desktop client's ``/api/schema``. It lives as a permission on the
# ``latchkey-self`` scope (like ``read-self-permissions``). That scope is
# domain-only, so it matches every gateway-self request; within the single
# matching rule detent allows a request that matches any one of the rule's
# listed permission schemas. All gateway-self grants minds produces -- schema
# read, file-sharing, accounts, and the cross-workspace verbs -- attach as
# permissions on this one scope, so there is only ever a single gateway-self
# rule and rule order never affects the verdict.
_MINDS_API_SCHEMA_INBOUND_PATH: Final[str] = "/minds-api-proxy/api/schema"
_PERM_MINDS_API_SCHEMA: Final[str] = "minds-api-schema-read"

# The newest workspace-template ref the minds desktop app supports. Granted by
# default for the same reasons as the schema endpoint above -- not agent-scoped,
# read-only, identical for every caller -- and additionally because a workspace's
# ``update-self`` flow reads it *unattended*, from a background worker, where a
# permission dialog has nobody to answer it. Pinned to the version subpath rather
# than ``/app`` so the ungated grant cannot widen to whatever app state a later
# route hangs off that prefix.
_MINDS_APP_VERSION_INBOUND_PATH: Final[str] = "/minds-api-proxy/api/v1/app/version"
_PERM_MINDS_APP_VERSION: Final[str] = "minds-app-version-read"

# The read-only host-timezone endpoint (the desktop client reports the IANA
# timezone of the machine it runs on). Granted to every agent by default for
# the same reasons as the API schema document: not agent-scoped (the timezone
# is identical for all callers) and carries no per-target data, so a workspace
# (e.g. its scheduler resolving "3 AM" in the user's local time) can always
# fetch it. Rides the same domain-only ``latchkey-self`` scope.
_MINDS_API_TIMEZONE_INBOUND_PATH: Final[str] = "/minds-api-proxy/api/v1/timezone"
_PERM_MINDS_API_TIMEZONE: Final[str] = "minds-api-timezone-read"

# The minds desktop client's cross-workspace management API
# (``/api/v1/workspaces/...``) attaches its per-verb permission schemas to the
# ``latchkey-self`` scope, just like file-sharing and accounts. Those schemas are
# NOT part of this agent baseline: they are self-contained in the ``workspace``
# permission request's precomputed effect and unioned onto the ``latchkey-self``
# rule when the user approves a grant (see ``permission_requests.mjs``'s
# ``computeWorkspaceEffect`` and the ``workspace_permissions`` module). Nothing
# about them needs to exist here.


AGENT_BASELINE_PERMISSIONS: Final[LatchkeyPermissionsConfig] = LatchkeyPermissionsConfig(
    rules=(
        # The bug-report route is allowed for any agent (see note above), so it must be matched
        # before the unauthorized gate -- detent stops at the first matching scope.
        {_SCOPE_MINDS_API_PROXY_REPORT: [_PERM_MINDS_API_PROXY_REPORT]},
        # Unauthorized agents trying to access agent-scoped Minds API endpoint get an empty list of permissions, leading to immediate rejection.
        {SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: []},
        {
            SCOPE_LATCHKEY_SELF: [
                _PERM_CREATE_PERMISSION_REQUEST,
                _PERM_READ_SELF_PERMISSIONS,
                _PERM_READ_AVAILABLE_PERMISSIONS,
                # Requests that made it through the first rule (= not unauthorized agents) can now access the agent-scoped Minds API endpoint.
                _PERM_MINDS_API_PROXY_PER_AGENT,
                # Every agent may read the (non-agent-scoped) API schema document.
                _PERM_MINDS_API_SCHEMA,
                # ... and the app's version, which caps update-self.
                _PERM_MINDS_APP_VERSION,
                # Every agent may read the (non-agent-scoped) host timezone.
                _PERM_MINDS_API_TIMEZONE,
            ],
        },
    ),
    schemas={
        _SCOPE_MINDS_API_PROXY_REPORT: {
            "properties": {
                "domain": {"const": _GATEWAY_SELF_HOST},
                "method": {"const": "POST"},
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_REPORT_PATH_PATTERN,
                },
            },
            "required": ["domain", "method", "path"],
        },
        _PERM_MINDS_API_PROXY_REPORT: {
            "properties": {
                "method": {"const": "POST"},
                "path": {
                    "type": "string",
                    "pattern": _MINDS_API_PROXY_REPORT_PATH_PATTERN,
                },
            },
            "required": ["method", "path"],
        },
        SCOPE_MINDS_API_PROXY_PER_AGENT_UNAUTHORIZED: {
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
        SCOPE_LATCHKEY_SELF: {
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
        _PERM_MINDS_API_SCHEMA: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"const": _MINDS_API_SCHEMA_INBOUND_PATH},
            },
            "required": ["method", "path"],
        },
        _PERM_MINDS_APP_VERSION: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"const": _MINDS_APP_VERSION_INBOUND_PATH},
            },
            "required": ["method", "path"],
        },
        _PERM_MINDS_API_TIMEZONE: {
            "properties": {
                "method": {"const": "GET"},
                "path": {"const": _MINDS_API_TIMEZONE_INBOUND_PATH},
            },
            "required": ["method", "path"],
        },
    },
    # Every host file references the shared additional-services schemas file, so a
    # granted custom scope (e.g. ``claude-ai``) resolves without inlining its
    # schema here. The bare name resolves relative to the file's directory (see
    # ``SHARED_SCHEMAS_FILENAME``).
    include=(SHARED_SCHEMAS_FILENAME,),
)
