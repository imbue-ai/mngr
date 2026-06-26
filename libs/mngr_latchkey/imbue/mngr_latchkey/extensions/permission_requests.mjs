/**
 * Latchkey gateway extension: HTTP endpoints for managing pending
 * permission requests.
 *
 * Endpoints:
 *   POST   /permission-requests
 *       Create a new pending permission request. Body is a JSON object
 *       with the fields:
 *         - ``agent_id``   (string, required)
 *         - ``rationale``  (string, required)
 *         - ``type``       (one of "predefined" | "file-sharing")
 *         - ``payload``    (object whose shape depends on ``type``)
 *       For ``type=="predefined"`` the payload is
 *         ``{scope: <string>, permissions: [<string>, ...]}``,
 *       matching the legacy detent-flavored scope/permission grant.
 *       The scope must be one of the Detent scopes named in the
 *       bundled ``services.json`` catalog, and every entry in
 *       ``permissions`` must be one of the permission-schema names
 *       the catalog lists under that scope; otherwise the request is
 *       rejected with HTTP 400.
 *       For ``type=="file-sharing"`` the payload is
 *         ``{path: <absolute_or_tilde_path>}``;
 *       the path must be absolute (start with ``/``) or use ``~`` /
 *       ``~/...`` to denote the current user's home directory (which is
 *       expanded to an absolute path before storage), and must not
 *       contain any ``..`` segments (rejected as a path-traversal
 *       attempt). ``~user`` notation for another user's home is
 *       rejected.
 *
 *       The extension also stores, alongside the user-supplied fields:
 *         - ``request_id`` (server-generated, UUIDv4 hex)
 *         - ``target``     (extension-context permissionsConfigPath: the
 *                          permissions.json file an approval would modify)
 *         - ``effect``     (precomputed ``{rules?, schemas?}`` object that
 *                          POST ``/permission-requests/approve/<id>``
 *                          will splice into the target permissions.json)
 *
 *   GET    /permission-requests
 *       List all pending permission requests as newline-delimited JSON
 *       (one object per line, ``application/x-ndjson``). Each line has
 *       the full persisted shape described above.
 *       With ``?follow=true`` the connection is kept open and every
 *       request newly created via this extension's POST endpoint is
 *       streamed as an additional JSONL line until the client
 *       disconnects. Filesystem changes made by anything other than
 *       the POST handler are not observed.
 *
 *   POST   /permission-requests/approve/<request_id>
 *       Approve the named request by splicing its ``effect`` into the
 *       stored ``target`` permissions.json (creating the file if it
 *       does not yet exist). Schemas are merged into the
 *       ``schemas`` object by name (overwriting on collision); rules
 *       are merged into the ``rules`` array by scope key (union of the
 *       permission list when the scope already had a rule, otherwise
 *       appended). After a successful write, the pending request file
 *       is deleted. Returns ``200`` with the freshly-applied
 *       permissions config in the body.
 *
 *   DELETE /permission-requests/<request_id>
 *       Remove the named pending request (used by the desktop client
 *       for the deny flow, and as a forget-without-grant escape hatch).
 *
 * Each pending request is stored as a single JSON file at
 * ``<latchkey-directory>/permission_requests/v2/<request_id>.json``,
 * where ``<latchkey-directory>`` is ``LATCHKEY_DIRECTORY`` if set,
 * otherwise ``~/.latchkey``. The ``v2`` segment is the on-disk schema
 * version; future shape changes get a new directory rather than
 * trying to migrate files in place.
 *
 * NOTE: extension requests still go through the gateway's permission
 * check, so callers must have a rule that allows them to talk to
 * ``latchkey-self.invalid`` on the relevant method/path. The agent
 * baseline grants ``POST /permission-requests`` only; the
 * ``/approve`` endpoint is meant to be reached from the desktop client
 * with admin-override credentials.
 *
 * There are potential race conditions but we ignore them for now.
 */

import { randomBytes, randomUUID } from 'node:crypto';
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, posix, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Bundled services catalog. The file is shipped alongside this
// extension and read fresh on every catalog-validating request so a
// catalog update lands without a gateway restart. The shape is the
// same one served by the ``permissions`` extension's
// ``/permissions/available`` endpoint: a JSON object keyed by raw
// service name, with each value an array of scope entries (a service
// may expose more than one Detent scope). Each entry carries a Detent
// ``scope`` schema name and a ``permissions`` array of
// ``{name, description}`` objects naming the permission schemas that
// may be granted under that scope.
const SERVICES_CATALOG_FILE = 'services.json';
const SERVICES_CATALOG_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  SERVICES_CATALOG_FILE,
);

const COLLECTION_PATH = '/permission-requests';
const APPROVE_PATH_PREFIX = '/permission-requests/approve/';
const ITEM_PATH_PREFIX = '/permission-requests/';
const REQUEST_FILE_SUFFIX = '.json';
const VALID_REQUEST_ID_PATTERN = /^[A-Za-z0-9._-]+$/;
// The canonical agent-id definition lives in Python:
// ``imbue.imbue_common.ids.RandomId._validate``, specialized by ``AgentId``
// (prefix ``agent``). The gateway is pure Node with no Python in the request
// path, so we mirror that rule here and MUST keep it in sync: an ``agent-``
// prefix followed by exactly 32 hex characters. Case-insensitive to match
// ``_validate``'s ``int(hex_part, 16)`` acceptance (real ids from
// ``uuid4().hex`` are lowercase). The cross-language drift guard lives in
// permission_requests_test.py, which posts a freshly generated ``AgentId``.
const VALID_AGENT_ID_PATTERN = /^agent-[0-9a-fA-F]{32}$/;

// Permission request types accepted by POST /permission-requests.
// ``predefined`` mirrors the original detent-flavored scope/permission
// grant (used for service catalog entries like ``slack-api``);
// ``file-sharing`` grants the agent access to a single absolute file
// path via the Minds API proxy.
const REQUEST_TYPE_PREDEFINED = 'predefined';
const REQUEST_TYPE_FILE_SHARING = 'file-sharing';
const VALID_REQUEST_TYPES = new Set([REQUEST_TYPE_PREDEFINED, REQUEST_TYPE_FILE_SHARING]);

// Detent's catch-all permission schema. The ``services.json``
// catalog never lists it explicitly (every scope implicitly
// admits it), so it is always a valid permission under any
// known scope.
const ALWAYS_AVAILABLE_PERMISSION = 'any';

// Names and constants used when generating the ``effect`` for a
// ``file-sharing`` request. The agent reaches the Minds API through
// the gateway's ``minds-api-proxy`` extension, which mounts under
// ``/minds-api-proxy/...``. ``/api/v1/files`` is the
// Minds-side WebDAV mount that actually serves files: a request for
// the file ``/abs/path`` lands at the URL path
// ``/api/v1/files/abs/path`` (the WebDAV share roots are mounted at
// their on-disk path, so the outward URL mirrors the absolute path
// one-to-one). Granting access to a specific file therefore means
// matching the URL path exactly via a per-file permission schema.
//
// We do *not* mint a scope schema here: the file-sharing rule reuses
// the agent baseline's ``latchkey-self`` scope (defined in
// ``agent_setup.py``), which already matches any request whose
// ``domain`` is ``latchkey-self.invalid``. The per-file permission
// schema is what restricts the grant to a single WebDAV URL + verb
// set; the scope just identifies which rule list the permission
// belongs to.
const FILE_SHARING_PROXY_PATH_PREFIX = '/minds-api-proxy/api/v1/files';
const FILE_SHARING_SCOPE_NAME = 'latchkey-self';
const FILE_SHARING_PERMISSION_PREFIX = 'minds-file-server-';

// Access modes the agent can request for a file. ``READ`` grants the
// non-mutating WebDAV verbs only; ``WRITE`` is a superset that also
// grants the verbs that change the file or its properties. Read-only
// and read-write grants live as distinct schemas in the user's
// permissions.json (the per-file schema name is prefixed with the
// access mode), so a user can independently hold either grant or both
// for the same path.
const FILE_SHARING_ACCESS_READ = 'READ';
const FILE_SHARING_ACCESS_WRITE = 'WRITE';
const VALID_FILE_SHARING_ACCESS_MODES = new Set([
  FILE_SHARING_ACCESS_READ,
  FILE_SHARING_ACCESS_WRITE,
]);

// WebDAV verbs that do not mutate the resource. ``GET``/``HEAD`` read
// the body / metadata; ``OPTIONS`` is the capability probe a client
// typically issues before anything else; ``PROPFIND`` reads
// properties (incl. listing a collection at the path).
const FILE_SHARING_READ_METHODS = ['GET', 'HEAD', 'OPTIONS', 'PROPFIND'];

// WebDAV verbs that mutate the resource (or, in the case of
// ``LOCK``/``UNLOCK``, server-side advisory state for it). Added on
// top of the read methods for ``WRITE`` grants. ``MKCOL`` is
// included for symmetry (the granted URL targets a single resource;
// if the user is sharing a path that doesn't yet exist, ``MKCOL``
// lets the agent create a collection there).
//
// ``COPY`` and ``MOVE`` are intentionally *not* in this list. Both
// carry a second path in the ``Destination`` HTTP header, and the
// per-file permission schema we emit only constrains the request URL.
// Granting ``COPY``/``MOVE`` on the source would therefore let the
// agent write to any path inside the WebDAV mount's share roots
// (``~/`` or ``/tmp/``) via the ``Destination`` header, regardless of
// what was actually shared. A single-file ``WRITE`` grant means "change
// this one file"; cross-path copy/move requires an explicit grant on
// the destination too. Agents that need copy semantics can ``GET``
// the source and ``PUT`` to a destination they have a separate grant
// for; likewise for move (``GET`` + ``PUT`` + ``DELETE``).
const FILE_SHARING_WRITE_ONLY_METHODS = [
  'PUT',
  'DELETE',
  'PROPPATCH',
  'MKCOL',
  'LOCK',
  'UNLOCK',
];

function allowedMethodsForAccess(access) {
  if (access === FILE_SHARING_ACCESS_READ) {
    return [...FILE_SHARING_READ_METHODS];
  }
  if (access === FILE_SHARING_ACCESS_WRITE) {
    return [...FILE_SHARING_READ_METHODS, ...FILE_SHARING_WRITE_ONLY_METHODS];
  }
  // Caller is responsible for having validated the access mode
  // earlier; this branch exists so the function is total.
  throw new InvalidRequestBodyError(`unhandled file-sharing access mode '${access}'.`);
}

class PermissionRequestsExtensionError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'PermissionRequestsExtensionError';
    this.statusCode = statusCode;
  }
}

class InvalidRequestBodyError extends PermissionRequestsExtensionError {
  constructor(detail) {
    super(400, `Invalid request body: ${detail}`);
    this.name = 'InvalidRequestBodyError';
  }
}

class InvalidRequestIdError extends PermissionRequestsExtensionError {
  constructor(detail) {
    super(400, `Invalid request_id: ${detail}`);
    this.name = 'InvalidRequestIdError';
  }
}

class RequestNotFoundError extends PermissionRequestsExtensionError {
  constructor(requestId) {
    super(404, `Permission request '${requestId}' not found.`);
    this.name = 'RequestNotFoundError';
  }
}

class ServicesCatalogUnavailableError extends PermissionRequestsExtensionError {
  constructor(detail) {
    super(500, `Could not load ${SERVICES_CATALOG_FILE}: ${detail}`);
    this.name = 'ServicesCatalogUnavailableError';
  }
}

class TargetNotConfiguredError extends PermissionRequestsExtensionError {
  constructor() {
    super(
      500,
      'Extension context did not provide permissionsConfigPath; cannot determine the target permissions.json.',
    );
    this.name = 'TargetNotConfiguredError';
  }
}

function resolveLatchkeyDirectory() {
  const directoryOverride = process.env.LATCHKEY_DIRECTORY;
  if (directoryOverride && directoryOverride.length > 0) {
    return directoryOverride;
  }
  return join(homedir(), '.latchkey');
}

function resolvePermissionRequestsDirectory() {
  // Bump the version when doing backwards incompatible changes to the data model so that we don't need to deal with old permission requests.
  return join(resolveLatchkeyDirectory(), 'permission_requests', 'v2');
}

function validateRequestId(rawValue) {
  if (typeof rawValue !== 'string' || rawValue.length === 0) {
    throw new InvalidRequestIdError('must be a non-empty string.');
  }
  if (!VALID_REQUEST_ID_PATTERN.test(rawValue)) {
    throw new InvalidRequestIdError(
      "must match /^[A-Za-z0-9._-]+$/ to be safe to use as a filename.",
    );
  }
  if (rawValue === '.' || rawValue === '..') {
    throw new InvalidRequestIdError("must not be '.' or '..'.");
  }
  return rawValue;
}

function requestFilePath(requestId) {
  return join(resolvePermissionRequestsDirectory(), `${requestId}${REQUEST_FILE_SUFFIX}`);
}

async function readRequestBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf-8');
}

function ensureNonEmptyString(parent, field, value) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new InvalidRequestBodyError(`${parent}'${field}' is required and must be a non-empty string.`);
  }
}

function ensureStringArray(parent, field, value) {
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
    throw new InvalidRequestBodyError(`${parent}'${field}' is required and must be an array of strings.`);
  }
}

function ensureNoExtraneousFields(parent, allowed, parsed) {
  const allowedSet = new Set(allowed);
  const extraneous = Object.keys(parsed).filter((name) => !allowedSet.has(name));
  if (extraneous.length > 0) {
    throw new InvalidRequestBodyError(
      `unknown ${parent}field(s): ${extraneous.map((name) => `'${name}'`).join(', ')}.`,
    );
  }
}

/**
 * The on-disk roots the Minds WebDAV server mounts: the current user's
 * home directory and the system temp directory. Computed from Node's
 * ``homedir()`` / ``tmpdir()`` which -- on the desktop host, where the
 * gateway and the Minds WebDAV server run as the same user in the same
 * environment -- match the ``Path.home()`` / ``tempfile.gettempdir()``
 * roots that ``webdav.py`` actually serves.
 *
 * Trailing slashes are stripped so the prefix test in
 * ``isPathWithinFileSharingRoot`` is uniform. A falsy root (which
 * ``homedir()`` / ``tmpdir()`` should never return on a real host) is
 * dropped rather than collapsed to ``/`` (which would match every
 * path).
 */
function fileSharingMountRoots() {
  const roots = [];
  for (const root of [homedir(), tmpdir()]) {
    if (typeof root !== 'string' || root.length === 0) {
      continue;
    }
    const trimmed = root.replace(/\/+$/, '');
    roots.push(trimmed.length > 0 ? trimmed : '/');
  }
  return roots;
}

/**
 * Whether ``filePath`` is at or beneath ``root``. The comparison is
 * case-insensitive to mirror WsgiDAV's share-prefix matching (it
 * lowercases both the share keys and the request path), and purely
 * lexical -- we do not resolve symlinks or require the path to exist,
 * matching how the WebDAV server mounts by string prefix.
 */
function isPathWithinFileSharingRoot(filePath, root) {
  const lowerPath = filePath.toLowerCase();
  const lowerRoot = root.toLowerCase();
  return lowerPath === lowerRoot || lowerPath.startsWith(`${lowerRoot}/`);
}

/**
 * Expand a leading ``~`` in a file-sharing path to the current user's
 * home directory, returning the path unchanged when it has no ``~``
 * prefix.
 *
 * Only the current user's home is supported: a bare ``~`` and a
 * ``~/...`` prefix both expand against Node's ``homedir()`` -- the same
 * root ``fileSharingMountRoots`` derives the home WebDAV mount from, so
 * an expanded path lands inside that mount and the root check below
 * accepts it. The ``~user`` form (some other user's home) cannot be
 * resolved here, so it is rejected with a clear error rather than
 * silently treated as a relative path.
 *
 * The expansion is a pure string splice -- we prepend the home
 * directory to the remainder verbatim rather than going through
 * ``path.join`` -- so any ``..`` segment in the remainder survives into
 * the expanded path and is still caught by the traversal checks in
 * ``validateAbsoluteFileSharingPath``. Joining would silently normalize
 * ``~/../foo`` into an escape past the home directory.
 */
function expandFileSharingHomePrefix(rawPath) {
  if (rawPath === '~' || rawPath.startsWith('~/')) {
    // ``slice(1)`` drops the leading ``~`` only: ``~`` -> ``<home>`` and
    // ``~/foo`` -> ``<home>/foo``.
    return `${homedir()}${rawPath.slice(1)}`;
  }
  if (rawPath.startsWith('~')) {
    throw new InvalidRequestBodyError(
      `payload.'path' uses unsupported '~user' notation; only '~' or '~/...' `
      + `(the current user's home) is accepted. Got ${rawPath}.`,
    );
  }
  return rawPath;
}

/**
 * Validate a filesystem path for the ``file-sharing`` payload and
 * return its canonical absolute form.
 *
 * A leading ``~`` / ``~/`` is first expanded to the current user's home
 * directory. The resulting path must start with ``/``, must not contain
 * any ``..`` segments (under either POSIX or Windows separators), and
 * must lie within one of the WebDAV mount roots (the user's home
 * directory or the system temp directory). We deliberately do not
 * resolve symlinks or check that the file exists here; that is a job
 * for the Minds API endpoint that ends up serving the file when the
 * request is approved. The goals are to reject obvious traversal
 * patterns (e.g. ``/etc/passwd/../shadow``) and to reject paths the
 * WebDAV server could never serve (anything outside the two mounts), so
 * the agent gets a clear error at request time -- or at approve time,
 * when this also guards a user-edited path override -- rather than an
 * approve-then-404 dead end.
 *
 * The returned value is the expanded path; callers persist it so the
 * stored payload, the per-file schema name, and the WebDAV pattern all
 * use a canonical absolute path rather than the ``~`` shorthand.
 */
function validateAbsoluteFileSharingPath(rawPath) {
  if (typeof rawPath !== 'string' || rawPath.length === 0) {
    throw new InvalidRequestBodyError("payload.'path' is required and must be a non-empty string.");
  }
  // Expand a leading ``~`` / ``~/`` to the current user's home before
  // any structural validation, so the checks below -- and the schema
  // name / WebDAV pattern derived downstream -- all operate on a
  // canonical absolute path.
  const expandedPath = expandFileSharingHomePrefix(rawPath);
  // POSIX rule -- absolute paths must start with '/'. The Minds host
  // is always POSIX (macOS or Linux), so we do not accept
  // Windows-style ``C:\\...`` paths.
  if (!expandedPath.startsWith('/')) {
    throw new InvalidRequestBodyError(
      `payload.'path' must be absolute (start with '/') or use '~' / '~/...': got ${rawPath}.`,
    );
  }
  // Reject any ``..`` segment regardless of where in the path it
  // appears (start, middle, end, or as the entire suffix). We split on
  // both POSIX and Windows separators so a payload smuggling
  // ``..\\foo`` past the unix check still gets refused.
  const segments = expandedPath.split(/[\\/]+/);
  for (const segment of segments) {
    if (segment === '..') {
      throw new InvalidRequestBodyError(`payload.'path' contains a '..' segment, refusing as a path-traversal attempt: ${rawPath}.`);
    }
  }
  // Normalising the path with posix.normalize collapses ``./`` and
  // multiple slashes. If the result still does not start with ``/`` or
  // changes the meaning (e.g. a hidden ``..`` snuck past the segment
  // scan), refuse.
  const normalized = posix.normalize(expandedPath);
  if (!normalized.startsWith('/') || normalized.includes('/../') || normalized.endsWith('/..')) {
    throw new InvalidRequestBodyError(`payload.'path' normalizes to a traversed path: ${rawPath} -> ${normalized}.`);
  }
  // Reject anything the Minds WebDAV server could never serve: it mounts
  // only the home and temp roots, so a grant elsewhere is inert (404).
  const roots = fileSharingMountRoots();
  if (!roots.some((root) => isPathWithinFileSharingRoot(normalized, root))) {
    throw new InvalidRequestBodyError(
      `payload.'path' must be within a shared root (${roots.join(', ')}); got ${rawPath}.`,
    );
  }
  return expandedPath;
}

/**
 * Read and validate the bundled ``services.json`` catalog, returning a
 * map from Detent scope schema name to the set of permission-schema
 * names that may be granted under that scope.
 *
 * Mirrors ``permissions.mjs``'s ``readAvailableServices`` -- duplicated
 * here rather than imported because the gateway loads extensions
 * independently and we cannot rely on cross-extension imports. The
 * file is trusted package data, so any structural problem is a
 * deployment bug and surfaces as HTTP 500.
 *
 * Each scope's set is seeded with the catch-all ``any`` permission,
 * which every scope implicitly admits.
 *
 * A service value is an array of scope entries, and each entry's
 * ``permissions`` are ``{name, description}`` objects; only the
 * ``name`` is collected here. Multiple entries that share a Detent
 * scope (across or within services) have their permission names
 * unioned so a shared-scope entry does not silently narrow the valid
 * permissions for one of the contributing entries.
 */
function loadValidPermissionsByScope() {
  let raw;
  try {
    raw = readFileSync(SERVICES_CATALOG_PATH, 'utf-8');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new ServicesCatalogUnavailableError(`cannot read file: ${message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new ServicesCatalogUnavailableError(`not valid JSON: ${message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ServicesCatalogUnavailableError(
      'top-level value is not a JSON object keyed by service name.',
    );
  }
  const permissionsByScope = new Map();
  for (const [serviceName, entries] of Object.entries(parsed)) {
    if (!Array.isArray(entries)) {
      throw new ServicesCatalogUnavailableError(
        `value for '${serviceName}' must be a JSON array of scope entries.`,
      );
    }
    entries.forEach((entry, index) => {
      if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
        throw new ServicesCatalogUnavailableError(
          `entry ${index} for '${serviceName}' must be a JSON object.`,
        );
      }
      if (typeof entry.scope !== 'string' || entry.scope.length === 0) {
        throw new ServicesCatalogUnavailableError(
          `entry ${index} for '${serviceName}': 'scope' must be a non-empty string.`,
        );
      }
      const permissions = entry.permissions;
      if (!Array.isArray(permissions)) {
        throw new ServicesCatalogUnavailableError(
          `entry ${index} for '${serviceName}': 'permissions' must be an array.`,
        );
      }
      // Every scope implicitly admits the catch-all ``any`` permission,
      // even when the catalog enumerates none, so seed each scope's set
      // with it.
      const existing = permissionsByScope.get(entry.scope) ?? new Set([ALWAYS_AVAILABLE_PERMISSION]);
      permissions.forEach((permission, permissionIndex) => {
        // Each permission is a ``{name, description}`` object; only the
        // ``name`` matters for validating an incoming request.
        if (
          typeof permission !== 'object' ||
          permission === null ||
          Array.isArray(permission) ||
          typeof permission.name !== 'string' ||
          permission.name.length === 0
        ) {
          throw new ServicesCatalogUnavailableError(
            `entry ${index} for '${serviceName}': permission ${permissionIndex} must be ` +
              `an object with a non-empty string 'name'.`,
          );
        }
        existing.add(permission.name);
      });
      permissionsByScope.set(entry.scope, existing);
    });
  }
  return permissionsByScope;
}

/**
 * Validate that ``scope`` is known to the bundled services catalog
 * and that every entry in ``permissions`` is a valid permission under
 * that scope. Throws ``InvalidRequestBodyError`` (HTTP 400) on any
 * mismatch so a caller that asks for a scope/permission combination
 * the catalog does not know about gets a clear rejection at request
 * creation time rather than producing an effect that approval would
 * happily splice into permissions.json.
 */
function validatePredefinedAgainstCatalog(scope, permissions) {
  const permissionsByScope = loadValidPermissionsByScope();
  const validPermissions = permissionsByScope.get(scope);
  if (validPermissions === undefined) {
    throw new InvalidRequestBodyError(
      `payload.'scope' '${scope}' is not a known service scope in the catalog.`,
    );
  }
  const unknown = permissions.filter((permission) => !validPermissions.has(permission));
  if (unknown.length > 0) {
    throw new InvalidRequestBodyError(
      `payload.'permissions' contains entries not valid for scope '${scope}': ${unknown
        .map((name) => `'${name}'`)
        .join(', ')}.`,
    );
  }
}

/**
 * Validate the payload object for a ``predefined`` permission request.
 * Returns the canonical payload shape (``{scope, permissions}``).
 *
 * Beyond structural type-checking, the scope and permissions are
 * cross-checked against the bundled services catalog so a request can
 * only ever name a (scope, permission) combination the catalog
 * actually exposes.
 */
function validatePredefinedPayload(payload) {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new InvalidRequestBodyError("'payload' must be a JSON object for type 'predefined'.");
  }
  ensureNonEmptyString('payload.', 'scope', payload.scope);
  ensureStringArray('payload.', 'permissions', payload.permissions);
  ensureNoExtraneousFields('payload ', ['scope', 'permissions'], payload);
  validatePredefinedAgainstCatalog(payload.scope, payload.permissions);
  return { scope: payload.scope, permissions: [...payload.permissions] };
}

/**
 * Validate the payload object for a ``file-sharing`` permission request.
 * Returns the canonical payload shape (``{path, access}``).
 *
 * ``access`` is required and must be one of the documented modes
 * (``READ`` / ``WRITE``). It is supplied by the agent rather than
 * picked by the user so the agent's stated need is captured at
 * request creation time -- a user who wants to grant something
 * narrower can deny and ask the agent to re-request with a smaller
 * mode (no downgrade-at-approval UI today).
 */
function validateFileSharingPayload(payload) {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new InvalidRequestBodyError("'payload' must be a JSON object for type 'file-sharing'.");
  }
  // ``validateAbsoluteFileSharingPath`` expands a leading ``~`` and
  // returns the canonical absolute path, which we persist in place of
  // the (possibly ``~``-prefixed) input.
  const path = validateAbsoluteFileSharingPath(payload.path);
  ensureNonEmptyString('payload.', 'access', payload.access);
  if (!VALID_FILE_SHARING_ACCESS_MODES.has(payload.access)) {
    throw new InvalidRequestBodyError(
      `payload.'access' must be one of ${[...VALID_FILE_SHARING_ACCESS_MODES]
        .map((name) => `'${name}'`)
        .join(', ')}; got '${payload.access}'.`,
    );
  }
  ensureNoExtraneousFields('payload ', ['path', 'access'], payload);
  return { path, access: payload.access };
}

/**
 * Parse the POST /permission-requests body. The body must contain
 * ``agent_id``, ``rationale``, ``type``, and ``payload``; the payload
 * shape depends on the request type. Any other top-level field
 * (including a caller-supplied ``request_id`` or ``target`` or
 * ``effect``) is rejected so the server side stays the single source
 * of truth on identity and on what the approval would do.
 */
async function parsePermissionRequestBody(request) {
  const raw = await readRequestBody(request);
  if (raw.trim().length === 0) {
    throw new InvalidRequestBodyError('expected a JSON object, got empty body.');
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new InvalidRequestBodyError(`not valid JSON: ${message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new InvalidRequestBodyError('expected a JSON object.');
  }
  ensureNonEmptyString('', 'agent_id', parsed.agent_id);
  // Reject a malformed agent_id at the gateway (the agent's tool call) rather
  // than persist it and let the consumer's ``AgentId(...)`` parse raise later --
  // an uncaught raise there killed the permission-requests consumer thread.
  if (!VALID_AGENT_ID_PATTERN.test(parsed.agent_id)) {
    throw new InvalidRequestBodyError(
      `'agent_id' must be a valid agent id ('agent-' followed by 32 hex characters); got '${parsed.agent_id}'.`,
    );
  }
  ensureNonEmptyString('', 'rationale', parsed.rationale);
  ensureNonEmptyString('', 'type', parsed.type);
  if (!VALID_REQUEST_TYPES.has(parsed.type)) {
    throw new InvalidRequestBodyError(
      `field 'type' must be one of ${[...VALID_REQUEST_TYPES].map((name) => `'${name}'`).join(', ')}; got '${parsed.type}'.`,
    );
  }
  if (parsed.payload === undefined) {
    throw new InvalidRequestBodyError("field 'payload' is required.");
  }
  ensureNoExtraneousFields('', ['agent_id', 'rationale', 'type', 'payload'], parsed);
  let payload;
  switch (parsed.type) {
    case REQUEST_TYPE_PREDEFINED:
      payload = validatePredefinedPayload(parsed.payload);
      break;
    case REQUEST_TYPE_FILE_SHARING:
      payload = validateFileSharingPayload(parsed.payload);
      break;
    default:
      // Unreachable because VALID_REQUEST_TYPES has already gated the
      // value; the default branch satisfies linting.
      throw new InvalidRequestBodyError(`unhandled request type '${parsed.type}'.`);
  }
  return {
    agent_id: parsed.agent_id,
    rationale: parsed.rationale,
    type: parsed.type,
    payload,
  };
}

/**
 * Compute the ``effect`` (the patch that approving the request will
 * splice into the target permissions.json). ``predefined`` requests
 * use built-in detent schemas (slack-api, github-rest-api, ...) so
 * the effect carries no ``schemas`` field; only the new
 * scope-to-permissions rule is included. ``file-sharing`` requests
 * need a custom per-path permission schema because the proxy-mediated
 * endpoint isn't in detent's built-in catalog -- that single schema
 * goes in ``effect.schemas``. The scope it activates under is the
 * pre-existing ``latchkey-self`` scope from the agent baseline, so
 * the effect does *not* emit a scope schema of its own; only
 * ``effect.rules`` carries the grant that wires the new permission
 * onto that scope.
 */
function computeEffect(type, payload) {
  switch (type) {
    case REQUEST_TYPE_PREDEFINED:
      return {
        rules: [{ [payload.scope]: [...payload.permissions] }],
      };
    case REQUEST_TYPE_FILE_SHARING:
      return computeFileSharingEffect(payload.path, payload.access);
    default:
      // Already validated by parsePermissionRequestBody; this branch
      // exists only to satisfy the type system / linters.
      throw new InvalidRequestBodyError(`unhandled request type '${type}'.`);
  }
}

/**
 * Derive a stable, human-readable schema name for a file-sharing
 * permission targeted at ``filePath`` at the named ``access`` mode.
 *
 * The name has the shape
 * ``minds-file-server-<access_lower>-<filePath>`` (e.g.
 * ``minds-file-server-read-/abs/path/to/file.txt``) so:
 *
 *  * read and write grants for the same path are *different* schemas
 *    -- both can coexist in a user's permissions.json, and a
 *    later write grant does not silently replace an earlier read
 *    grant (or vice versa);
 *  * the schema name is human-readable -- both the access mode and
 *    the full file path appear in plaintext, which makes
 *    permissions.json easy to audit by eye;
 *  * the per-mode-per-path mapping is deterministic *and* injective
 *    (the path is embedded verbatim, so distinct paths cannot
 *    collide), so idempotent re-approval of the same (path, access)
 *    pair merges cleanly through the schema-by-name merge in the
 *    approve handler.
 *
 * The schema name is only ever used as a JSON object key / string
 * value inside permissions.json, so it has no filename-safety or
 * length constraints beyond what JSON itself allows. ``filePath``
 * has already been validated by ``validateAbsoluteFileSharingPath``
 * to start with ``/`` and to be traversal-free, which makes the
 * ``<access_lower>-/<...>`` boundary in the resulting name
 * unambiguous.
 */
function fileSharingPermissionSchemaName(filePath, access) {
  if (!VALID_FILE_SHARING_ACCESS_MODES.has(access)) {
    throw new InvalidRequestBodyError(`unhandled file-sharing access mode '${access}'.`);
  }
  return `${FILE_SHARING_PERMISSION_PREFIX}${access.toLowerCase()}-${filePath}`;
}

/**
 * Escape a literal string for safe inclusion in a JavaScript regular
 * expression. Used to embed the WebDAV URL prefix into the per-file
 * permission schema's ``pattern`` without letting a literal ``.`` in
 * the file path (e.g. ``data.txt``) broaden the match.
 */
function escapeForRegex(literal) {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Build the JSON-Schema ``pattern`` for the per-file permission's
 * ``path`` property. Granting access to ``<base>`` (the WebDAV URL
 * for the shared file or directory) admits:
 *
 *   * ``<base>`` itself,
 *   * ``<base>/`` (the same resource with a trailing slash, which
 *     WebDAV clients commonly emit when treating the target as a
 *     collection), and
 *   * ``<base>/<sub-path>``: any resource nested below the shared
 *     one, so a grant on a directory transitively covers every file
 *     and sub-directory inside it.
 *
 * We deliberately do not try to reject ``..`` segments in the regex
 * itself: the gateway feeds the permission check a ``Request`` built
 * from a WHATWG URL, and the WHATWG URL parser collapses both literal
 * ``..`` and percent-encoded ``%2e%2e`` segments out of ``pathname``
 * before detent ever runs this pattern. By the time we see the path,
 * ``<base>/foo/../bar`` has already been normalised to
 * ``<base>/bar`` (and ``<base>/..`` to the parent of ``<base>``,
 * which doesn't start with ``<base>`` and so doesn't match anyway).
 * That makes the literal ``..``-stripping job belong upstream of the
 * permission check, not in this regex.
 *
 * The leading ``/`` anchors the sub-path: a path like ``<base>foo``
 * (no separator) does not match, so the grant cannot be used to
 * access a sibling whose path happens to share a prefix with the
 * granted resource.
 */
function fileSharingPermissionPathPattern(fileWebdavPath) {
  return `^${escapeForRegex(fileWebdavPath)}(/.*)?$`;
}

/**
 * Normalize ``urlPath`` exactly the way the WHATWG URL parser
 * normalizes an incoming request's ``pathname``, so a per-file
 * permission pattern built from an on-disk path matches the path
 * detent actually checks at request time.
 *
 * The gateway feeds the permission check a ``Request`` built from a
 * WHATWG URL, and detent matches the per-file schema's ``pattern``
 * against ``URL.pathname``. That pathname is percent-encoded by the
 * URL parser's path-percent-encode set: a space becomes ``%20``, each
 * non-ASCII byte its UTF-8 ``%XX`` sequence, and characters like
 * ``#``/``?`` (which would otherwise start the fragment/query) their
 * encoded forms -- while ``/`` separators, ``+``/``:``/``@`` and
 * already-encoded ``%XX`` triplets are left untouched. If we embedded
 * the raw on-disk path (with a literal space, accented letter, etc.)
 * into the regex, the encoded request pathname (``...%20...``) would
 * never match it -- which is exactly the bug a path containing a space
 * (e.g. an agent-requested or user-selected directory) used to hit.
 *
 * Assigning to ``URL.pathname`` applies precisely that path-percent-
 * encode set without truncating on ``#``/``?``, and -- verified against
 * the full-URL parse detent performs -- reproduces the identical
 * canonical pathname (including dot-segment collapsing). We use the
 * same ``placeholder.invalid`` origin the route parser uses for
 * symmetry.
 */
function normalizeWebdavUrlPath(urlPath) {
  const url = new URL('http://placeholder.invalid');
  url.pathname = urlPath;
  return url.pathname;
}

function computeFileSharingEffect(filePath, access) {
  const permissionSchemaName = fileSharingPermissionSchemaName(filePath, access);
  // WebDAV serves the on-disk path directly under the mount, so the
  // permission's URL path is the prefix + the absolute file path.
  // ``validateAbsoluteFileSharingPath`` has already guaranteed that
  // ``filePath`` starts with ``/`` and is traversal-free. We normalize
  // the combined path the same way the WHATWG URL parser normalizes the
  // incoming request's pathname so the regex matches even when the path
  // has characters the parser percent-encodes (spaces, non-ASCII, ...).
  const fileWebdavPath = normalizeWebdavUrlPath(`${FILE_SHARING_PROXY_PATH_PREFIX}${filePath}`);
  return {
    schemas: {
      [permissionSchemaName]: {
        properties: {
          method: { enum: allowedMethodsForAccess(access) },
          path: {
            type: 'string',
            pattern: fileSharingPermissionPathPattern(fileWebdavPath),
          },
        },
        required: ['method', 'path'],
      },
    },
    // The rule attaches the new per-file permission to the
    // pre-existing ``latchkey-self`` scope from the agent baseline
    // (defined in ``agent_setup.py``). We deliberately do not mint a
    // scope schema here -- the baseline already declares one that
    // matches any request with ``domain == latchkey-self.invalid``,
    // and the merge logic in ``handleApproveRequest`` unions the new
    // permission name into that scope's existing permission list.
    rules: [{ [FILE_SHARING_SCOPE_NAME]: [permissionSchemaName] }],
  };
}

function ensureDirectory(directory) {
  if (!existsSync(directory)) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
}

/**
 * Atomic write helper. Two callers: writing a pending-request file
 * under our own ``permission_requests/v2`` directory (mode 0600) and
 * writing the target permissions.json on approval (preserves existing
 * mode if any). ``mode`` is the unix mode for the *temp* file.
 *
 * If ``filePath`` is an existing symlink, the atomic rename targets
 * the symlink's realpath rather than the link itself. ``rename(2)``
 * operates on the link, not its target, so writing to the link path
 * directly would replace the symlink with a regular file -- breaking
 * the per-agent opaque symlinks that ``mngr latchkey link-permissions``
 * swings into the canonical host permissions file. Resolving the link
 * up front means the swap lands on the underlying file and the
 * symlink stays in place.
 */
function writeJsonFileAtomic(filePath, value, mode) {
  const destinationPath = resolveSymlinkTargetForWrite(filePath);
  const directory = dirname(destinationPath);
  ensureDirectory(directory);
  const tempPath = join(
    directory,
    `.tmp.permission-request.${randomBytes(6).toString('hex')}`,
  );
  const serialized = `${JSON.stringify(value, null, 2)}\n`;
  try {
    writeFileSync(tempPath, serialized, { encoding: 'utf-8', mode });
    renameSync(tempPath, destinationPath);
  } catch (error) {
    try {
      unlinkSync(tempPath);
    } catch {
      // best-effort cleanup
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Failed to write ${filePath}: ${message}`,
    );
  }
}

/**
 * If ``filePath`` exists and is a symlink, return its realpath; if it
 * does not exist or is a regular file, return ``filePath`` unchanged.
 * Any other error (e.g. EACCES on ``lstat``) surfaces as a 500 so we
 * don't silently fall back to clobbering the link.
 */
function resolveSymlinkTargetForWrite(filePath) {
  let stat;
  try {
    stat = lstatSync(filePath);
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      return filePath;
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Failed to stat ${filePath}: ${message}`,
    );
  }
  if (!stat.isSymbolicLink()) {
    return filePath;
  }
  try {
    return realpathSync(filePath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Failed to resolve symlink ${filePath}: ${message}`,
    );
  }
}

/**
 * Validate the persisted shape of a single permission-request record.
 * Returns the parsed JSON object if it conforms, otherwise ``null`` so
 * the caller can skip stray non-conforming files in the directory.
 */
function validatePersistedRequest(parsed) {
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  for (const field of ['request_id', 'agent_id', 'rationale', 'request_type', 'target']) {
    if (typeof parsed[field] !== 'string' || parsed[field].length === 0) {
      return null;
    }
  }
  if (!VALID_REQUEST_TYPES.has(parsed.request_type)) {
    return null;
  }
  if (typeof parsed.payload !== 'object' || parsed.payload === null || Array.isArray(parsed.payload)) {
    return null;
  }
  if (typeof parsed.effect !== 'object' || parsed.effect === null || Array.isArray(parsed.effect)) {
    return null;
  }
  return parsed;
}

/**
 * Read a single permission-request file. Returns ``null`` when the file
 * is unreadable or its contents do not match the expected shape, so a
 * stray non-JSON file in the directory cannot break listing.
 */
function readRequestFile(filePath) {
  let raw;
  try {
    raw = readFileSync(filePath, 'utf-8');
  } catch {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  return validatePersistedRequest(parsed);
}

function listPermissionRequests() {
  const directory = resolvePermissionRequestsDirectory();
  if (!existsSync(directory)) {
    return [];
  }
  const fileNames = readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .filter((entry) => entry.name.endsWith(REQUEST_FILE_SUFFIX))
    .filter((entry) => !entry.name.startsWith('.'))
    .map((entry) => entry.name)
    .sort();
  const result = [];
  for (const fileName of fileNames) {
    const value = readRequestFile(join(directory, fileName));
    if (value !== null) {
      result.push(value);
    }
  }
  return result;
}

function decodeRequestIdSegment(rawSegment) {
  try {
    return decodeURIComponent(rawSegment);
  } catch {
    return rawSegment;
  }
}

function parseRequestUrl(requestUrl) {
  // The request URL is path-and-query only, so use a placeholder origin to
  // make WHATWG URL parsing happy.
  return new URL(requestUrl ?? '', 'http://placeholder.invalid');
}

function parseRoute(requestUrl) {
  const pathOnly = parseRequestUrl(requestUrl).pathname;
  if (pathOnly === COLLECTION_PATH || pathOnly === `${COLLECTION_PATH}/`) {
    return { kind: 'collection' };
  }
  // The approve prefix is a longer match than ITEM_PATH_PREFIX, so we
  // check it first to avoid an "approve" item-id ever shadowing the
  // approve route. (``validateRequestId`` would reject ``approve``
  // anyway, but checking order matters when the prefixes overlap.)
  if (pathOnly.startsWith(APPROVE_PATH_PREFIX)) {
    const remainder = pathOnly.slice(APPROVE_PATH_PREFIX.length);
    if (remainder.length === 0 || remainder.includes('/')) {
      return { kind: 'unhandled' };
    }
    return { kind: 'approve', requestIdFromPath: decodeRequestIdSegment(remainder) };
  }
  if (pathOnly.startsWith(ITEM_PATH_PREFIX)) {
    const remainder = pathOnly.slice(ITEM_PATH_PREFIX.length);
    if (remainder.length === 0 || remainder.includes('/')) {
      return { kind: 'unhandled' };
    }
    return { kind: 'item', requestIdFromPath: decodeRequestIdSegment(remainder) };
  }
  return { kind: 'unhandled' };
}

function parseBooleanQueryParam(rawValue) {
  if (rawValue === null || rawValue === undefined) return false;
  const normalized = rawValue.toLowerCase();
  return normalized === '1' || normalized === 'true' || normalized === 'yes';
}

function sendJson(response, statusCode, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body, 'utf-8'),
  });
  response.end(body);
}

function sendError(response, statusCode, message) {
  sendJson(response, statusCode, { error: message });
}

function writeJsonLine(response, value) {
  response.write(`${JSON.stringify(value)}\n`);
}

/**
 * In-process subscribers that get invoked synchronously after each
 * successful POST to /permission-requests. Each follow-stream adds itself
 * to this set on connect and removes itself on disconnect.
 */
const newRequestListeners = new Set();

/**
 * Cleanup callbacks for every active follow stream. The ``stop`` lifecycle
 * hook iterates this set to terminate streams promptly on gateway
 * shutdown.
 */
const activeFollowStreamCleanups = new Set();

function subscribeToNewRequests(listener) {
  newRequestListeners.add(listener);
  return () => {
    newRequestListeners.delete(listener);
  };
}

function notifyNewRequest(value) {
  for (const listener of newRequestListeners) {
    try {
      listener(value);
    } catch {
      // A misbehaving listener must not break the POST handler or other
      // listeners.
    }
  }
}

/**
 * Stream permission requests until the client disconnects.
 */
function streamPermissionRequests(request, response) {
  response.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
  response.flushHeaders();

  const unsubscribe = subscribeToNewRequests((value) => {
    writeJsonLine(response, value);
  });
  for (const value of listPermissionRequests()) {
    writeJsonLine(response, value);
  }

  return new Promise((resolve) => {
    let cleanedUp = false;
    const cleanup = () => {
      if (cleanedUp) return;
      cleanedUp = true;
      activeFollowStreamCleanups.delete(cleanup);
      unsubscribe();
      if (!response.writableEnded) {
        response.end();
      }
      resolve();
    };
    activeFollowStreamCleanups.add(cleanup);
    request.on('close', cleanup);
  });
}

/**
 * Gateway lifecycle hook: end every active follow stream so the gateway
 * can shut down promptly.
 */
export function stop() {
  for (const cleanup of [...activeFollowStreamCleanups]) {
    cleanup();
  }
}

/**
 * Generate a server-side request_id. We use a UUIDv4 hex so the result
 * is filename-safe under VALID_REQUEST_ID_PATTERN and trivially unique
 * across concurrent POSTs.
 */
function generateRequestId() {
  return randomUUID().replace(/-/g, '');
}

/**
 * Resolve and validate the ``permissionsConfigPath`` carried on the
 * extension context. The gateway always sets it to an absolute path,
 * but we double-check the shape so a misconfigured context surfaces as
 * a 500 rather than a confusing write failure later on.
 */
function requireTargetFromContext(context) {
  if (
    typeof context !== 'object' ||
    context === null ||
    typeof context.permissionsConfigPath !== 'string' ||
    context.permissionsConfigPath.length === 0
  ) {
    throw new TargetNotConfiguredError();
  }
  return context.permissionsConfigPath;
}

async function handleCreateRequest(request, response, context) {
  const body = await parsePermissionRequestBody(request);
  const target = requireTargetFromContext(context);
  const effect = computeEffect(body.type, body.payload);
  // Loop on the astronomically rare UUID collision so we never overwrite
  // an existing pending request.
  let requestId = generateRequestId();
  let filePath = requestFilePath(requestId);
  while (existsSync(filePath)) {
    requestId = generateRequestId();
    filePath = requestFilePath(requestId);
  }
  validateRequestId(requestId);
  // We rename the wire field ``type`` to ``request_type`` here. The
  // POST input keeps ``type`` (as documented), but the persisted and
  // streamed shapes use ``request_type`` to avoid shadowing the
  // ``type`` Python builtin in the consumer's pydantic model.
  const persisted = {
    request_id: requestId,
    agent_id: body.agent_id,
    rationale: body.rationale,
    request_type: body.type,
    payload: body.payload,
    target,
    effect,
  };
  writeJsonFileAtomic(filePath, persisted, 0o600);
  notifyNewRequest(persisted);
  sendJson(response, 201, persisted);
}

function handleListRequests(request, response, follow) {
  if (follow) {
    return streamPermissionRequests(request, response);
  }
  response.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
  for (const value of listPermissionRequests()) {
    writeJsonLine(response, value);
  }
  response.end();
  return Promise.resolve();
}

function handleDeleteRequest(response, rawRequestId) {
  const requestId = validateRequestId(rawRequestId);
  const filePath = requestFilePath(requestId);
  if (!existsSync(filePath)) {
    throw new RequestNotFoundError(requestId);
  }
  try {
    unlinkSync(filePath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Failed to delete ${filePath}: ${message}`,
    );
  }
  response.writeHead(204);
  response.end();
}

/**
 * Read an existing permissions.json into a plain object, or return a
 * fresh ``{rules: []}`` if the file does not yet exist. Anything we
 * cannot parse cleanly surfaces as a 500 -- approving against a
 * corrupted permissions file would risk silently dropping unrelated
 * rules.
 */
function readPermissionsFileOrEmpty(filePath) {
  if (!existsSync(filePath)) {
    return { rules: [], schemas: {} };
  }
  let raw;
  try {
    raw = readFileSync(filePath, 'utf-8');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Failed to read permissions config at ${filePath}: ${message}`,
    );
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Permissions config at ${filePath} is not valid JSON: ${message}`,
    );
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new PermissionRequestsExtensionError(
      500,
      `Permissions config at ${filePath} must be a JSON object.`,
    );
  }
  return parsed;
}

/**
 * Return the (single) scope key of a rule entry, or ``null`` when the
 * entry is not a single-key object. Mirrors ``permissions.mjs``'s
 * ``ruleKeyOf`` -- duplicated here rather than imported because the
 * gateway loads extensions independently and we cannot rely on
 * cross-extension imports.
 */
function ruleKeyOf(rule) {
  if (typeof rule !== 'object' || rule === null || Array.isArray(rule)) {
    return null;
  }
  const keys = Object.keys(rule);
  return keys.length === 1 ? keys[0] : null;
}

/**
 * Merge ``effect.rules`` into the target's ``rules`` array. For each
 * effect rule, look up an existing rule under the same scope key:
 * found -> union the permission lists (preserving the existing order
 * and appending only previously-unseen permissions); not found ->
 * append the new rule. The merged rules array is returned.
 */
function mergeRules(existingRules, effectRules) {
  const rules = Array.isArray(existingRules) ? [...existingRules] : [];
  for (const effectRule of effectRules) {
    const scopeKey = ruleKeyOf(effectRule);
    if (scopeKey === null) {
      throw new PermissionRequestsExtensionError(
        500,
        `Internal error: effect rule is not a single-key object: ${JSON.stringify(effectRule)}`,
      );
    }
    const effectPermissions = effectRule[scopeKey];
    if (!Array.isArray(effectPermissions) || !effectPermissions.every((item) => typeof item === 'string')) {
      throw new PermissionRequestsExtensionError(
        500,
        `Internal error: effect rule permissions for scope '${scopeKey}' must be an array of strings.`,
      );
    }
    const existingIndex = rules.findIndex((rule) => ruleKeyOf(rule) === scopeKey);
    if (existingIndex === -1) {
      rules.push({ [scopeKey]: [...effectPermissions] });
      continue;
    }
    const existingPermissions = rules[existingIndex][scopeKey];
    const merged = Array.isArray(existingPermissions) ? [...existingPermissions] : [];
    for (const permission of effectPermissions) {
      if (!merged.includes(permission)) {
        merged.push(permission);
      }
    }
    rules[existingIndex] = { [scopeKey]: merged };
  }
  return rules;
}

/**
 * Merge ``effect.schemas`` into the target's ``schemas`` object,
 * overwriting on collision. Schema definitions for the same name are
 * expected to be functionally equivalent (the file-sharing scope
 * schema is constant across requests; per-path permission schema names
 * are uniquely derived from the path), so overwriting is safe.
 */
function mergeSchemas(existingSchemas, effectSchemas) {
  const base =
    typeof existingSchemas === 'object' && existingSchemas !== null && !Array.isArray(existingSchemas)
      ? { ...existingSchemas }
      : {};
  for (const [name, schema] of Object.entries(effectSchemas)) {
    base[name] = schema;
  }
  return base;
}

/**
 * Parse the optional JSON body of a ``POST /permission-requests/approve/<id>``
 * call. The desktop client sends a body only when the user edited the
 * shared path in the approval dialog before approving; the body then
 * carries a single ``path`` field with the (possibly new) absolute
 * filesystem path the grant should target. An empty body (the common
 * case -- the user approved the request as-is) yields ``null``.
 *
 * The override path is validated here with the same traversal-rejection
 * rules that guard request *creation* (``validateAbsoluteFileSharingPath``),
 * so a user-edited path is held to exactly the same security bar as an
 * agent-supplied one. Any field other than ``path`` is rejected so the
 * server stays the single source of truth on identity, target, and
 * access mode.
 */
async function parseApproveOverrideBody(request) {
  const raw = await readRequestBody(request);
  if (raw.trim().length === 0) {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new InvalidRequestBodyError(`approve body is not valid JSON: ${message}`);
  }
  // A literal JSON ``null`` body means "no override", same as an empty
  // body -- some HTTP clients serialize an absent payload that way.
  if (parsed === null) {
    return null;
  }
  if (typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new InvalidRequestBodyError('approve body must be a JSON object.');
  }
  ensureNoExtraneousFields('approve body ', ['path'], parsed);
  if (parsed.path === undefined) {
    return null;
  }
  // Expand ``~`` and canonicalize the user-edited path the same way a
  // request-creation path is handled, then carry the absolute form.
  const path = validateAbsoluteFileSharingPath(parsed.path);
  return { path };
}

/**
 * Resolve the effect that approving ``requestRecord`` should splice in.
 *
 * With no override this is just the precomputed ``requestRecord.effect``.
 * When the user edited the shared path in the dialog (``override`` is
 * non-null), the effect is recomputed for the new path so the grant
 * targets exactly what the user chose -- but only for ``file-sharing``
 * requests, which are the only kind whose effect is path-derived. A
 * path override on any other request type is a programming error in the
 * caller and is rejected.
 */
function resolveEffectForApproval(requestRecord, override) {
  if (override === null) {
    return requestRecord.effect;
  }
  if (requestRecord.request_type !== REQUEST_TYPE_FILE_SHARING) {
    throw new InvalidRequestBodyError(
      `a 'path' override is only valid for '${REQUEST_TYPE_FILE_SHARING}' requests; `
      + `request ${requestRecord.request_id} is '${requestRecord.request_type}'.`,
    );
  }
  // The access mode is fixed at request-creation time and is not
  // user-editable: the user may narrow/redirect *where* the grant
  // applies, but not escalate read-only to read-write.
  return computeFileSharingEffect(override.path, requestRecord.payload.access);
}

function handleApproveRequest(response, rawRequestId, override = null) {
  const requestId = validateRequestId(rawRequestId);
  const filePath = requestFilePath(requestId);
  const requestRecord = readRequestFile(filePath);
  if (requestRecord === null) {
    throw new RequestNotFoundError(requestId);
  }
  const target = requestRecord.target;
  const effect = resolveEffectForApproval(requestRecord, override);
  const permissionsFile = readPermissionsFileOrEmpty(target);

  const updatedRules = effect.rules ? mergeRules(permissionsFile.rules, effect.rules) : permissionsFile.rules;
  const updatedSchemas = effect.schemas
    ? mergeSchemas(permissionsFile.schemas, effect.schemas)
    : permissionsFile.schemas;
  const updated = {
    ...permissionsFile,
    rules: Array.isArray(updatedRules) ? updatedRules : [],
  };
  if (updatedSchemas && Object.keys(updatedSchemas).length > 0) {
    updated.schemas = updatedSchemas;
  } else if ('schemas' in updated && (!updated.schemas || Object.keys(updated.schemas).length === 0)) {
    delete updated.schemas;
  }
  // Use mode 0644 here rather than 0600 -- the permissions config is
  // readable by the gateway process and by the desktop client; it
  // does not need to be locked to the writing user the way the
  // pending-request files are.
  writeJsonFileAtomic(target, updated, 0o644);
  // Delete the pending request only after the grant has landed on
  // disk; if the write fails the request stays pending so the user
  // can retry.
  try {
    unlinkSync(filePath);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionRequestsExtensionError(
      500,
      `Approved ${requestId} but failed to remove pending request file ${filePath}: ${message}`,
    );
  }
  sendJson(response, 200, { request_id: requestId, target, applied: updated });
}

export default async function permissionRequestsExtension(request, response, context) {
  const route = parseRoute(request.url);
  const method = (request.method ?? 'GET').toUpperCase();

  try {
    if (route.kind === 'collection' && method === 'GET') {
      const followParam = parseRequestUrl(request.url).searchParams.get('follow');
      await handleListRequests(request, response, parseBooleanQueryParam(followParam));
      return true;
    }
    if (route.kind === 'collection' && method === 'POST') {
      await handleCreateRequest(request, response, context);
      return true;
    }
    if (route.kind === 'approve' && method === 'POST') {
      const override = await parseApproveOverrideBody(request);
      handleApproveRequest(response, route.requestIdFromPath, override);
      return true;
    }
    if (route.kind === 'item' && method === 'DELETE') {
      handleDeleteRequest(response, route.requestIdFromPath);
      return true;
    }
  } catch (error) {
    if (error instanceof PermissionRequestsExtensionError) {
      sendError(response, error.statusCode, error.message);
      return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    sendError(response, 500, `Internal error: ${message}`);
    return true;
  }
  return false;
}
