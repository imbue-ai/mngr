/**
 * Latchkey gateway extension: HTTP endpoints for inspecting and editing
 * a Detent permissions config file at a caller-supplied path.
 *
 * Endpoints (the target file is always selected by the ``path`` query
 * param, except for ``/permissions/self`` which uses the gateway-supplied
 * config path from the extension context; the rule key is selected by
 * ``rule_key``):
 *
 *   GET    /permissions?path=<path>
 *       Return the full permissions.json at <path>.
 *   GET    /permissions/self
 *       Return the full permissions.json that the gateway applied to
 *       the caller for this request (from the extension context's
 *       ``permissionsConfigPath``). Takes no query parameters.
 *   GET    /permissions/available/<service_name>
 *       Return the permission catalog entries for ``<service_name>``
 *       (e.g. ``slack``, ``google-gmail``) as an array. Each entry has
 *       four fields: ``scope`` (the Detent scope schema name as a
 *       string), ``display_name`` (a human-readable label),
 *       ``description`` (the scope's plain-English summary, from
 *       Detent's ``$comment``), and ``permissions`` (an array of
 *       ``{name, description}`` objects -- the Detent permission-schema
 *       name plus its own plain-English summary -- that may be granted
 *       under the scope). The catch-all ``any`` permission is always
 *       injected at index 0 of every scope's ``permissions`` array, so
 *       a caller can *       always request unrestricted access under
 *       a known scope. Returns 404 when the service is unknown. Backed
 *       by the ``services.json`` file that ships alongside this extension,
 *       which is keyed by raw service name.
 *   GET    /permissions/rules?path=<path>&rule_key=<key>
 *       Return the rule whose scope key is <key>.
 *   POST   /permissions/rules?path=<path>&rule_key=<key>
 *       Add or replace the rule for <key>. Body: JSON array of
 *       permission-schema names.
 *   DELETE /permissions/rules?path=<path>&rule_key=<key>
 *       Remove the rule for <key>.
 *
 * Security model: for the endpoints that accept ``path``, ``path``
 * must resolve (after symlink-aware
 * normalization of its existing parent directory and `..` segments)
 * underneath the directory named in the ``LATCHKEY_EXTENSION_PERMISSIONS_ROOT``
 * environment variable, and must not equal the root itself. Any path
 * outside the root, any ``path`` query param that fails to normalize,
 * and any request received when the env var is unset / empty are
 * rejected with HTTP 403. This is the only thing standing between a
 * caller and arbitrary read/write on the gateway host's filesystem,
 * so we are deliberately strict about it. ``/permissions/self`` does
 * not consult the env var because its path is supplied by the gateway
 * (via the extension context) rather than the caller.
 *
 * NOTE: extension requests still go through the gateway's permission
 * check, so callers must have a rule that allows them to talk to
 * `latchkey-self.invalid` on the relevant method/path.
 *
 * There are potential race conditions but we ignore them for now.
 */

import { randomBytes } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const COLLECTION_PATH = '/permissions';
const SELF_PATH = '/permissions/self';
const AVAILABLE_ITEM_PATH_PREFIX = '/permissions/available/';
const RULES_COLLECTION_PATH = '/permissions/rules';
const PERMISSIONS_ROOT_ENV_VAR = 'LATCHKEY_EXTENSION_PERMISSIONS_ROOT';
const AVAILABLE_SERVICES_FILE = 'services.json';
const AVAILABLE_SERVICES_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  AVAILABLE_SERVICES_FILE,
);
// Service names in services.json are URL-path segments; constrain them
// to lowercase letters, digits, and ``-`` so a caller cannot smuggle
// path-traversal segments or other surprises into the lookup key.
const VALID_SERVICE_NAME_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

// Detent's catch-all *permission* schema. It matches every request, so a
// rule like ``{"linear-api": ["any"]}`` grants all access under that
// scope. The ``services.json`` catalog never lists it explicitly (every
// scope implicitly admits it).
const ALWAYS_AVAILABLE_PERMISSION = 'any';
const ALWAYS_AVAILABLE_PERMISSION_DESCRIPTION =
  'Unrestricted access: every request permitted under this scope. Use only ' +
  'when no narrower permission covers what you need.';

class PermissionsExtensionError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'PermissionsExtensionError';
    this.statusCode = statusCode;
  }
}

class PermissionsRootNotConfiguredError extends PermissionsExtensionError {
  constructor() {
    super(
      403,
      `Permissions extension refuses requests because ${PERMISSIONS_ROOT_ENV_VAR} is not set.`,
    );
    this.name = 'PermissionsRootNotConfiguredError';
  }
}

class MissingPathParamError extends PermissionsExtensionError {
  constructor() {
    super(400, "Missing required 'path' query parameter.");
    this.name = 'MissingPathParamError';
  }
}

class PathOutsideRootError extends PermissionsExtensionError {
  constructor(rawPath) {
    super(403, `Path '${rawPath}' resolves outside ${PERMISSIONS_ROOT_ENV_VAR}.`);
    this.name = 'PathOutsideRootError';
  }
}

class PermissionsFileMissingError extends PermissionsExtensionError {
  constructor(filePath) {
    super(404, `permissions.json not found at ${filePath}`);
    this.name = 'PermissionsFileMissingError';
  }
}

class PermissionsFileMalformedError extends PermissionsExtensionError {
  constructor(filePath, detail) {
    super(500, `permissions.json at ${filePath} is malformed: ${detail}`);
    this.name = 'PermissionsFileMalformedError';
  }
}

class AvailableServicesUnavailableError extends PermissionsExtensionError {
  constructor(detail) {
    super(500, `Could not load ${AVAILABLE_SERVICES_FILE}: ${detail}`);
    this.name = 'AvailableServicesUnavailableError';
  }
}

class InvalidRequestBodyError extends PermissionsExtensionError {
  constructor(detail) {
    super(400, `Invalid request body: ${detail}`);
    this.name = 'InvalidRequestBodyError';
  }
}

class RuleNotFoundError extends PermissionsExtensionError {
  constructor(ruleKey) {
    super(404, `No rule with key '${ruleKey}' in permissions.json`);
    this.name = 'RuleNotFoundError';
  }
}

class MissingRuleKeyError extends PermissionsExtensionError {
  constructor() {
    super(400, "Missing required 'rule_key' query parameter.");
    this.name = 'MissingRuleKeyError';
  }
}

class InvalidServiceNameError extends PermissionsExtensionError {
  constructor(rawValue) {
    super(
      400,
      `Invalid service name '${rawValue}': must match /^[a-z0-9][a-z0-9-]*$/.`,
    );
    this.name = 'InvalidServiceNameError';
  }
}

class ServiceNotFoundError extends PermissionsExtensionError {
  constructor(serviceName) {
    super(404, `No permission catalog entry for service '${serviceName}'.`);
    this.name = 'ServiceNotFoundError';
  }
}

function resolveRootDirectory() {
  const rootOverride = process.env[PERMISSIONS_ROOT_ENV_VAR];
  if (!rootOverride || rootOverride.length === 0) {
    throw new PermissionsRootNotConfiguredError();
  }
  // The root itself must exist as a real directory; resolving its real
  // path lets a symlinked root (e.g. on macOS where /var -> /private/var)
  // match the real-path of files placed under it.
  let realRoot;
  try {
    realRoot = realpathSync(rootOverride);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionsExtensionError(
      500,
      `${PERMISSIONS_ROOT_ENV_VAR} (${rootOverride}) is unreadable: ${message}`,
    );
  }
  return realRoot;
}

/**
 * Resolve and root-check a caller-supplied path query param. Resolves
 * symlinks on the *existing* portion of the path (so a write to a
 * not-yet-existing leaf file is still allowed, as long as the parent
 * directory is real and under the root), then ensures the result is
 * strictly underneath the root.
 */
function resolvePathParamUnderRoot(rawPath) {
  if (typeof rawPath !== 'string' || rawPath.length === 0) {
    throw new MissingPathParamError();
  }
  const root = resolveRootDirectory();
  const absolute = isAbsolute(rawPath) ? rawPath : resolve(root, rawPath);

  // Resolve symlinks on whichever ancestor of `absolute` already
  // exists, then append the not-yet-existing tail. This lets us check
  // the real-path of a *target* file even when it has not been written
  // yet.
  let existing = absolute;
  const tail = [];
  while (!existsSync(existing)) {
    const parent = dirname(existing);
    if (parent === existing) {
      // Walked off the top of the filesystem without finding an
      // existing ancestor. The path cannot possibly be under the root.
      throw new PathOutsideRootError(rawPath);
    }
    tail.unshift(existing.slice(parent.length + 1));
    existing = parent;
  }
  let realExisting;
  try {
    realExisting = realpathSync(existing);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionsExtensionError(
      500,
      `Could not resolve real path of ${existing}: ${message}`,
    );
  }
  const normalized = tail.length === 0 ? realExisting : resolve(realExisting, ...tail);

  const relativeToRoot = relative(root, normalized);
  if (
    relativeToRoot === '' ||
    relativeToRoot.startsWith('..') ||
    isAbsolute(relativeToRoot)
  ) {
    throw new PathOutsideRootError(rawPath);
  }
  return normalized;
}

function readPermissionsFile(filePath) {
  if (!existsSync(filePath)) {
    throw new PermissionsFileMissingError(filePath);
  }
  let raw;
  try {
    raw = readFileSync(filePath, 'utf-8');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionsFileMalformedError(filePath, `cannot read file: ${message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionsFileMalformedError(filePath, `not valid JSON: ${message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new PermissionsFileMalformedError(filePath, 'top-level value is not an object');
  }
  return parsed;
}

function writePermissionsFileAtomic(filePath, value) {
  const directory = dirname(filePath);
  const tempPath = `${filePath}.tmp.${randomBytes(6).toString('hex')}`;
  const serialized = `${JSON.stringify(value, null, 2)}\n`;
  try {
    // Materialize the parent directory before writing. POST /permissions/rules
    // creates the target file if it does not yet exist, and the minds desktop
    // client points it at ``<root>/hosts/<host_id>/latchkey_permissions.json``
    // -- a per-host directory that may not exist yet (e.g. when agent
    // creation's finalize/link step was skipped or failed). Without this, the
    // atomic write of the temp sibling fails with ENOENT and the grant
    // surfaces as a confusing 500.
    mkdirSync(directory, { recursive: true });
    writeFileSync(tempPath, serialized, 'utf-8');
    renameSync(tempPath, filePath);
  } catch (error) {
    try {
      unlinkSync(tempPath);
    } catch {
      // best-effort cleanup
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new PermissionsExtensionError(500, `Failed to write ${filePath}: ${message}`);
  }
}

/**
 * Return the (single) scope key of a rule entry, or null when the entry
 * is not a single-key object.
 */
function ruleKeyOf(rule) {
  if (typeof rule !== 'object' || rule === null || Array.isArray(rule)) {
    return null;
  }
  const keys = Object.keys(rule);
  return keys.length === 1 ? keys[0] : null;
}

function findRule(permissionsFile, ruleKey) {
  const rules = Array.isArray(permissionsFile.rules) ? permissionsFile.rules : [];
  for (const rule of rules) {
    if (ruleKeyOf(rule) === ruleKey) {
      return rule;
    }
  }
  return null;
}

function parseRequestUrl(requestUrl) {
  // Path-and-query only; use a placeholder origin to make WHATWG URL
  // parsing happy.
  return new URL(requestUrl ?? '', 'http://placeholder.invalid');
}

function decodeServiceNameSegment(rawSegment) {
  try {
    return decodeURIComponent(rawSegment);
  } catch {
    return rawSegment;
  }
}

function parseRoute(requestUrl) {
  const pathOnly = parseRequestUrl(requestUrl).pathname;
  if (pathOnly === COLLECTION_PATH || pathOnly === `${COLLECTION_PATH}/`) {
    return { kind: 'collection' };
  }
  if (pathOnly === SELF_PATH || pathOnly === `${SELF_PATH}/`) {
    return { kind: 'self' };
  }
  // Only the per-service item endpoint (``/permissions/available/<service>``)
  // is served; the bare collection (``/permissions/available[/]``) is
  // deliberately unhandled (an empty or slash-containing remainder), so
  // a request for the whole catalog falls through rather than enumerating
  // every service.
  if (pathOnly.startsWith(AVAILABLE_ITEM_PATH_PREFIX)) {
    const remainder = pathOnly.slice(AVAILABLE_ITEM_PATH_PREFIX.length);
    if (remainder.length === 0 || remainder.includes('/')) {
      return { kind: 'unhandled' };
    }
    return { kind: 'available-item', serviceNameFromPath: decodeServiceNameSegment(remainder) };
  }
  if (pathOnly === RULES_COLLECTION_PATH || pathOnly === `${RULES_COLLECTION_PATH}/`) {
    return { kind: 'rule' };
  }
  return { kind: 'unhandled' };
}

function requireQueryParam(searchParams, name, errorClass) {
  const value = searchParams.get(name);
  if (value === null || value.length === 0) {
    throw new errorClass();
  }
  return value;
}

async function readRequestBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf-8');
}

async function parseRulePermissionsBody(request) {
  const raw = await readRequestBody(request);
  if (raw.trim().length === 0) {
    throw new InvalidRequestBodyError('expected a JSON array, got empty body');
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new InvalidRequestBodyError(`not valid JSON: ${message}`);
  }
  if (!Array.isArray(parsed)) {
    throw new InvalidRequestBodyError('expected a JSON array of permission-schema names');
  }
  for (const entry of parsed) {
    if (typeof entry !== 'string') {
      throw new InvalidRequestBodyError('every array element must be a string');
    }
  }
  return parsed;
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

function handleGetCollection(response, filePath) {
  const file = readPermissionsFile(filePath);
  sendJson(response, 200, file);
}

/**
 * Read and validate the bundled ``services.json`` catalog. The file is
 * trusted package data (it ships alongside this extension and is copied
 * verbatim into ``LATCHKEY_DIRECTORY/extensions`` at gateway-spawn time),
 * so a malformed file is a deployment bug rather than a caller error;
 * we surface it as HTTP 500.
 *
 * The file is a JSON object keyed by raw service name (``slack``,
 * ``google-gmail``, ...). Each value is an array of ``{scope:
 * <schema_name>, display_name: <label>, description: <summary>,
 * permissions: [{name: <schema_name>, description: <summary>}, ...]}``
 * objects (a single service may expose more than one scope) where
 * ``scope`` is the Detent scope schema name as a plain string,
 * ``display_name`` is a human-readable label, the optional scope-level
 * ``description`` is the scope's plain-English summary (Detent's
 * ``$comment``), and each permission carries its own name plus an
 * optional ``description``.
 */
function readAvailableServices() {
  let raw;
  try {
    raw = readFileSync(AVAILABLE_SERVICES_PATH, 'utf-8');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new AvailableServicesUnavailableError(`cannot read file: ${message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new AvailableServicesUnavailableError(`not valid JSON: ${message}`);
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new AvailableServicesUnavailableError(
      'top-level value is not a JSON object keyed by service name',
    );
  }
  for (const [serviceName, entries] of Object.entries(parsed)) {
    if (!Array.isArray(entries)) {
      throw new AvailableServicesUnavailableError(
        `value for '${serviceName}' must be a JSON array of scope entries`,
      );
    }
    entries.forEach((entry, index) => {
      if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}' must be a JSON object`,
        );
      }
      if (typeof entry.scope !== 'string' || entry.scope.length === 0) {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}': 'scope' must be a non-empty string`,
        );
      }
      if (typeof entry.display_name !== 'string' || entry.display_name.length === 0) {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}': 'display_name' must be a non-empty string`,
        );
      }
      // The scope-level ``description`` is optional but must be a string.
      if (entry.description !== undefined && typeof entry.description !== 'string') {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}': 'description' must be a string`,
        );
      }
      // Each permission is a ``{name, description?}`` object: ``name`` is a
      // non-empty string and ``description``, when present, is a string.
      const permissions = entry.permissions;
      if (!Array.isArray(permissions)) {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}': 'permissions' must be an array`,
        );
      }
      const isEveryPermissionWellFormed = permissions.every(
        (item) =>
          typeof item === 'object' &&
          item !== null &&
          !Array.isArray(item) &&
          typeof item.name === 'string' &&
          item.name.length > 0 &&
          (item.description === undefined || typeof item.description === 'string'),
      );
      if (!isEveryPermissionWellFormed) {
        throw new AvailableServicesUnavailableError(
          `entry ${index} for '${serviceName}': each 'permissions' item must be ` +
            `an object with a non-empty string 'name' and optional string 'description'`,
        );
      }
    });
  }
  return parsed;
}

function handleGetAvailableForService(response, rawServiceName) {
  if (
    typeof rawServiceName !== 'string' ||
    rawServiceName.length === 0 ||
    !VALID_SERVICE_NAME_PATTERN.test(rawServiceName)
  ) {
    throw new InvalidServiceNameError(rawServiceName);
  }
  const catalog = readAvailableServices();
  // Guard against prototype-chain hits (``constructor``, ``__proto__``,
  // ...) since ``catalog`` is a plain ``JSON.parse`` object literal.
  if (!Object.prototype.hasOwnProperty.call(catalog, rawServiceName)) {
    throw new ServiceNotFoundError(rawServiceName);
  }
  sendJson(response, 200, catalog[rawServiceName].map(withAlwaysAvailablePermission));
}

/**
 * Prepend the always-available ``any`` permission to a scope entry's
 * ``permissions`` array (deduplicating in case the catalog lists it
 * explicitly). This ensures every scope -- even one with no enumerated
 * permissions, like Linear -- surfaces at least the ``any`` option so a
 * caller can request unrestricted access under it.
 */
function withAlwaysAvailablePermission(entry) {
  const existing = Array.isArray(entry.permissions) ? entry.permissions : [];
  const withoutAny = existing.filter(
    (permission) => permission.name !== ALWAYS_AVAILABLE_PERMISSION,
  );
  return {
    ...entry,
    permissions: [
      {
        name: ALWAYS_AVAILABLE_PERMISSION,
        description: ALWAYS_AVAILABLE_PERMISSION_DESCRIPTION,
      },
      ...withoutAny,
    ],
  };
}

function handleGetSelf(response, context) {
  if (
    typeof context !== 'object' ||
    context === null ||
    typeof context.permissionsConfigPath !== 'string' ||
    context.permissionsConfigPath.length === 0
  ) {
    throw new PermissionsExtensionError(
      500,
      'Extension context did not provide permissionsConfigPath.',
    );
  }
  const file = readPermissionsFile(context.permissionsConfigPath);
  sendJson(response, 200, file);
}

function handleGetRule(response, filePath, ruleKey) {
  const file = readPermissionsFile(filePath);
  const rule = findRule(file, ruleKey);
  if (rule === null) {
    throw new RuleNotFoundError(ruleKey);
  }
  sendJson(response, 200, rule);
}

async function handlePostRule(request, response, filePath, ruleKey) {
  const permissions = await parseRulePermissionsBody(request);

  const file = existsSync(filePath) ? readPermissionsFile(filePath) : { rules: [] };
  const rules = Array.isArray(file.rules) ? [...file.rules] : [];
  const existingIndex = rules.findIndex((rule) => ruleKeyOf(rule) === ruleKey);
  const newRule = { [ruleKey]: permissions };
  if (existingIndex === -1) {
    rules.push(newRule);
  } else {
    rules[existingIndex] = newRule;
  }
  const updated = { ...file, rules };
  writePermissionsFileAtomic(filePath, updated);

  sendJson(response, existingIndex === -1 ? 201 : 200, newRule);
}

function handleDeleteRule(response, filePath, ruleKey) {
  const file = readPermissionsFile(filePath);
  const rules = Array.isArray(file.rules) ? file.rules : [];
  const remaining = rules.filter((rule) => ruleKeyOf(rule) !== ruleKey);
  if (remaining.length === rules.length) {
    throw new RuleNotFoundError(ruleKey);
  }
  const updated = { ...file, rules: remaining };
  writePermissionsFileAtomic(filePath, updated);
  response.writeHead(204);
  response.end();
}

export default async function permissionsExtension(request, response, context) {
  const route = parseRoute(request.url);
  const method = (request.method ?? 'GET').toUpperCase();
  const searchParams = parseRequestUrl(request.url).searchParams;

  try {
    if (route.kind === 'collection' && method === 'GET') {
      const filePath = resolvePathParamUnderRoot(searchParams.get('path'));
      handleGetCollection(response, filePath);
      return true;
    }
    if (route.kind === 'self' && method === 'GET') {
      handleGetSelf(response, context);
      return true;
    }
    if (route.kind === 'available-item' && method === 'GET') {
      handleGetAvailableForService(response, route.serviceNameFromPath);
      return true;
    }
    if (route.kind === 'rule') {
      const filePath = resolvePathParamUnderRoot(searchParams.get('path'));
      const ruleKey = requireQueryParam(searchParams, 'rule_key', MissingRuleKeyError);
      if (method === 'GET') {
        handleGetRule(response, filePath, ruleKey);
        return true;
      }
      if (method === 'POST') {
        await handlePostRule(request, response, filePath, ruleKey);
        return true;
      }
      if (method === 'DELETE') {
        handleDeleteRule(response, filePath, ruleKey);
        return true;
      }
    }
  } catch (error) {
    if (error instanceof PermissionsExtensionError) {
      sendError(response, error.statusCode, error.message);
      return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    sendError(response, 500, `Internal error: ${message}`);
    return true;
  }
  return false;
}
