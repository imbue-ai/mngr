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
  readFileSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, resolve, relative } from 'node:path';

const COLLECTION_PATH = '/permissions';
const SELF_PATH = '/permissions/self';
const RULES_COLLECTION_PATH = '/permissions/rules';
const PERMISSIONS_ROOT_ENV_VAR = 'LATCHKEY_EXTENSION_PERMISSIONS_ROOT';

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
    writeFileSync(tempPath, serialized, 'utf-8');
    renameSync(tempPath, filePath);
  } catch (error) {
    try {
      unlinkSync(tempPath);
    } catch {
      // best-effort cleanup
    }
    void directory;
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

function parseRoute(requestUrl) {
  const pathOnly = parseRequestUrl(requestUrl).pathname;
  if (pathOnly === COLLECTION_PATH || pathOnly === `${COLLECTION_PATH}/`) {
    return { kind: 'collection' };
  }
  if (pathOnly === SELF_PATH || pathOnly === `${SELF_PATH}/`) {
    return { kind: 'self' };
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
