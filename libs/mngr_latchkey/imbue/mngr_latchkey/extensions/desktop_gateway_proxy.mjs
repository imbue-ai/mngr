/**
 * Latchkey gateway extension: the user's desktops, and the desktop-owned
 * routes forwarded to them.
 *
 * Endpoints:
 *   GET /devices                    -> every desktop this gateway knows, connected or not
 *   ANY /permissions...             \
 *   ANY /permission-requests...      > forwarded to a desktop gateway
 *   ANY /minds-api-proxy...         /
 *
 * Loaded only by a remote host's machine; the desktop gateway serves these
 * routes itself (and ``/devices`` through ``device_list.mjs``). Every desktop
 * of the user's that is connected to the machine has announced itself in the
 * directory ``LATCHKEY_EXTENSION_DEVICES_DIR`` names: one JSON file per
 * desktop, holding the machine loopback port its reverse tunnel binds and the
 * two secrets this extension presents on the hop back to that desktop's
 * gateway (its listen password and a permissions-override JWT it minted for
 * this host). A record's age is reported, not judged: a desktop that stopped
 * announcing itself (asleep, offline, quit) is listed with when it was last
 * heard from, beside how often a connected one checks in, and is still
 * proxied to when asked for; its tunnel, which the machine's sshd retires
 * once the session stops answering probes, is what makes such a request fail
 * fast. Records are read afresh on every request, so a desktop connecting or
 * leaving takes effect without a gateway restart.
 *
 * Which desktop a forwarded request is for is the ``X-Latchkey-Device``
 * header: absent, the most recently announced desktop (which is what every
 * workspace built before the header did); one device id; ``*`` for every
 * desktop the gateway knows; or a comma-separated list of device ids, of which
 * the ones this gateway does not know are ignored. When the plural forms come
 * down to no desktop at all, the answer is 503. When they come down to a
 * single desktop, the request is forwarded to it and its response
 * relayed as is, just as a local workspace's gateway answers for the one
 * desktop it runs on. Otherwise the request goes to each desktop and the
 * answer is 200 with the responses side by side (``{"responses": [{device_id,
 * hostname, status, content_type, body}...]}``, an unreachable desktop's entry
 * carrying an ``error`` instead of a body), marked by the
 * ``X-Latchkey-Multiple-Desktops-Matched: true`` response header, which no
 * other response carries; a streaming response (``?follow=true``) is only
 * forwarded to a single desktop.
 *
 * ``POST /permission-requests`` is the one forwarded request this machine
 * takes part in, because a permission request outlives the desktops it was
 * sent to: the user answers it on whichever desktop they are at, and a
 * desktop that was offline when it was filed shows it the next time it
 * connects. So the machine assigns the request its id before forwarding, puts
 * it in the body as ``request_id`` (a body that already carries one is a 400:
 * identity is the gateway's to assign) so every desktop files the same
 * request under the same id, and keeps a copy of what the agent sent under
 * ``<LATCHKEY_DIRECTORY>/filed_permission_requests/v1/<request_id>.json`` --
 * the agent's body as sent, the desktops it was for (``"*"`` or a list of
 * device ids) and when it was filed -- for the desktops to sync against. The
 * machine validates nothing: the copy is kept when some desktop accepted the
 * request (a 2xx) or when no desktop could be reached to judge it, and dropped
 * when every desktop that answered refused it. The answer to the agent is the
 * one the header's shape gives every forwarded request, with one difference:
 * when the request reaches no desktop at all, the 503 says that the request
 * was kept for the desktops to pick up (one that reaches an unreachable
 * desktop is kept too, and answered with that desktop's 502). A ``DELETE
 * /permission-requests/<request_id>`` forwarded through here also drops the
 * machine's copy, so a request withdrawn by its agent is forgotten here too.
 * Resolving a request the user answered on a desktop is the package's
 * ``mngr-latchkey forget-request``, which sends that very DELETE to every
 * desktop through this gateway.
 *
 * Every other URL is left for the next extension.
 */

import { randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { readdir, readFile, stat } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { homedir } from 'node:os';
import { join } from 'node:path';

const DEVICES_DIR_ENV_VAR = 'LATCHKEY_EXTENSION_DEVICES_DIR';
const DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR = 'LATCHKEY_EXTENSION_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS';
const DEFAULT_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS = 30;
const DEVICE_RECORD_SUFFIX = '.json';
// Where a desktop's tunnel binds on the machine.
const LOOPBACK_HOST = '127.0.0.1';

const DEVICE_HEADER = 'X-Latchkey-Device';
const ALL_DESKTOPS = '*';
const MULTIPLE_DESKTOPS_MATCHED_HEADER = 'X-Latchkey-Multiple-Desktops-Matched';
const GATEWAY_PASSWORD_HEADER = 'X-Latchkey-Gateway-Password';
const PERMISSIONS_OVERRIDE_HEADER = 'X-Latchkey-Gateway-Permissions-Override';
const DEVICES_ROUTE = '/devices';
const PERMISSION_REQUESTS_ROUTE = '/permission-requests';
const PERMISSION_REQUEST_ITEM_PATH_PREFIX = `${PERMISSION_REQUESTS_ROUTE}/`;
const PROXY_PATH_PREFIXES = ['/permissions', PERMISSION_REQUESTS_ROUTE, '/minds-api-proxy'];

// Where this machine keeps the requests filed through it, under its latchkey
// directory. The version segment is bumped with any incompatible change to
// the record's shape, so an older record is never read as a newer one.
const FILED_REQUESTS_DIR_NAME = 'filed_permission_requests';
const FILED_REQUESTS_SCHEMA_VERSION = 'v1';
const FILED_REQUEST_FILE_SUFFIX = '.json';
// The same ids the desktops' ``permission_requests.mjs`` accepts, since the
// machine's id is what every desktop files the request under.
const VALID_REQUEST_ID_PATTERN = /^[A-Za-z0-9._-]+$/;
const REQUEST_STORED_NOTE =
  'The request was kept on this machine for the desktops to pick up when they next connect.';

const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
]);

class DesktopRoutingError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'DesktopRoutingError';
    this.statusCode = statusCode;
  }
}

class DesktopsNotConfiguredError extends DesktopRoutingError {
  constructor(detail) {
    super(503, `Desktop routing is not configured on this gateway: ${detail}.`);
    this.name = 'DesktopsNotConfiguredError';
  }
}

class NoDesktopAnnouncedError extends DesktopRoutingError {
  constructor() {
    super(503, 'No desktop has announced itself to this machine.');
    this.name = 'NoDesktopAnnouncedError';
  }
}

function requiredEnv(name) {
  const value = process.env[name];
  if (value === undefined || value.length === 0) {
    throw new DesktopsNotConfiguredError(`environment variable ${name} is not set`);
  }
  return value;
}

/** How often a connected desktop refreshes its record, reported so a caller can read a record's age. */
function announcementIntervalSeconds() {
  const raw = process.env[DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR];
  if (raw === undefined || raw.length === 0) return DEFAULT_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new DesktopsNotConfiguredError(
      `${DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR}=${raw} is not a positive number of seconds`,
    );
  }
  return seconds;
}

/**
 * Read one device record as a desktop, or return null for a file that is not
 * one (a record in a shape another version wrote, or not a record at all),
 * which is skipped rather than failing every request on the machine.
 */
async function readDeviceRecord(directory, fileName) {
  const path = join(directory, fileName);
  let content;
  let modifiedAt;
  try {
    [content, modifiedAt] = await Promise.all([readFile(path, 'utf-8'), stat(path).then((info) => info.mtime)]);
  } catch (error) {
    return null;
  }
  let record;
  try {
    record = JSON.parse(content);
  } catch (error) {
    console.error(`desktop_gateway_proxy: ignoring device record ${path}: not JSON`);
    return null;
  }
  const deviceId = fileName.slice(0, -DEVICE_RECORD_SUFFIX.length);
  if (
    typeof record !== 'object' ||
    record === null ||
    record.device_id !== deviceId ||
    typeof record.hostname !== 'string' ||
    !Number.isInteger(record.port) ||
    record.port <= 0 ||
    typeof record.gateway_password !== 'string' ||
    record.gateway_password.length === 0 ||
    typeof record.permissions_override !== 'string' ||
    record.permissions_override.length === 0
  ) {
    console.error(`desktop_gateway_proxy: ignoring device record ${path}: not the shape a desktop announces`);
    return null;
  }
  return {
    deviceId,
    hostname: record.hostname,
    lastSeenAt: modifiedAt,
    upstream: { hostname: LOOPBACK_HOST, port: record.port },
    credentials: { password: record.gateway_password, permissionsOverride: record.permissions_override },
  };
}

/** The desktops that have announced themselves to this machine, most recently seen first. */
async function readAnnouncedDesktops() {
  const directory = requiredEnv(DEVICES_DIR_ENV_VAR);
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new DesktopsNotConfiguredError(`${DEVICES_DIR_ENV_VAR}=${directory} cannot be listed: ${message}`);
  }
  const desktops = (
    await Promise.all(
      entries
        .filter((entry) => entry.isFile() && entry.name.endsWith(DEVICE_RECORD_SUFFIX))
        .map((entry) => readDeviceRecord(directory, entry.name)),
    )
  ).filter((desktop) => desktop !== null);
  desktops.sort((first, second) => {
    const byRecency = second.lastSeenAt.getTime() - first.lastSeenAt.getTime();
    return byRecency !== 0 ? byRecency : first.deviceId.localeCompare(second.deviceId);
  });
  return desktops;
}

function isProxyRoute(pathOnly) {
  return PROXY_PATH_PREFIXES.some((prefix) => pathOnly === prefix || pathOnly.startsWith(`${prefix}/`));
}

function resolveLatchkeyDirectory() {
  const directoryOverride = process.env.LATCHKEY_DIRECTORY;
  if (directoryOverride !== undefined && directoryOverride.length > 0) return directoryOverride;
  return join(homedir(), '.latchkey');
}

function filedRequestsDirectory() {
  return join(resolveLatchkeyDirectory(), FILED_REQUESTS_DIR_NAME, FILED_REQUESTS_SCHEMA_VERSION);
}

function filedRequestPath(requestId) {
  return join(filedRequestsDirectory(), `${requestId}${FILED_REQUEST_FILE_SUFFIX}`);
}

function generateRequestId() {
  return randomUUID().replace(/-/g, '');
}

/** Keep the machine's copy of a filed request: a complete file, so a reader never sees a half-written one. */
function writeFiledRequest(record) {
  const directory = filedRequestsDirectory();
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const destination = filedRequestPath(record.request_id);
  const temporary = join(directory, `.tmp.${record.request_id}`);
  writeFileSync(temporary, `${JSON.stringify(record)}\n`, { encoding: 'utf-8', mode: 0o600 });
  renameSync(temporary, destination);
}

/** Drop the machine's copy of a filed request, if it has one; an id that names no file is nothing to drop. */
function removeFiledRequest(requestId) {
  if (!VALID_REQUEST_ID_PATTERN.test(requestId)) return;
  const path = filedRequestPath(requestId);
  if (!existsSync(path)) return;
  try {
    unlinkSync(path);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`desktop_gateway_proxy: could not drop the filed request ${path}: ${message}`);
  }
}

/**
 * What the X-Latchkey-Device header asks for: ``{kind: 'default'}``, ``{kind:
 * 'all'}``, ``{kind: 'one', deviceId}`` or ``{kind: 'many', deviceIds}``.
 */
function parseDeviceHeader(request) {
  const raw = request.headers[DEVICE_HEADER.toLowerCase()];
  const value = (Array.isArray(raw) ? raw.join(',') : raw ?? '').trim();
  if (value.length === 0) return { kind: 'default' };
  if (value === ALL_DESKTOPS) return { kind: 'all' };
  if (!value.includes(',')) return { kind: 'one', deviceId: value };
  const deviceIds = [...new Set(value.split(',').map((part) => part.trim()).filter((part) => part.length > 0))];
  if (deviceIds.length === 0 || deviceIds.includes(ALL_DESKTOPS)) {
    throw new DesktopRoutingError(
      400,
      `The ${DEVICE_HEADER} header must name one desktop, a comma-separated list of desktops, or '*' for all of them.`,
    );
  }
  return { kind: 'many', deviceIds };
}

/** The desktop a device id names; one this gateway has never heard from is a 503, like no desktop at all. */
function findDesktop(desktops, deviceId) {
  const desktop = desktops.find((candidate) => candidate.deviceId === deviceId);
  if (desktop === undefined) {
    throw new DesktopRoutingError(503, `Desktop ${deviceId} is not known to this gateway.`);
  }
  return desktop;
}

/**
 * Copy the inbound headers for the hop to one desktop: minus the hop-by-hop
 * ones, the host, the header that chose the desktop, and the caller's
 * credentials, which the desktop's own replace: the caller's password
 * authenticates it to *this* gateway and is a different secret from that
 * desktop gateway's, and its permissions override would let it pick the
 * policy the desktop evaluates it against.
 */
function buildUpstreamHeaders(request, desktop) {
  const headers = {};
  const rawHeaders = request.rawHeaders ?? [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    const lowerName = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(lowerName) ||
      lowerName === 'host' ||
      lowerName === DEVICE_HEADER.toLowerCase() ||
      lowerName === GATEWAY_PASSWORD_HEADER.toLowerCase() ||
      lowerName === PERMISSIONS_OVERRIDE_HEADER.toLowerCase()
    )
      continue;
    const existing = headers[name];
    if (existing === undefined) {
      headers[name] = value;
    } else if (Array.isArray(existing)) {
      existing.push(value);
    } else {
      headers[name] = [existing, value];
    }
  }
  headers.host = `${desktop.upstream.hostname}:${desktop.upstream.port}`;
  headers[GATEWAY_PASSWORD_HEADER] = desktop.credentials.password;
  headers[PERMISSIONS_OVERRIDE_HEADER] = desktop.credentials.permissionsOverride;
  return headers;
}

function upstreamRequestOptions(request, desktop) {
  return {
    hostname: desktop.upstream.hostname,
    port: desktop.upstream.port,
    method: (request.method ?? 'GET').toUpperCase(),
    path: request.url ?? '/',
    headers: buildUpstreamHeaders(request, desktop),
  };
}

function relayResponseHead(upstreamResponse, response) {
  const filtered = [];
  const rawHeaders = upstreamResponse.rawHeaders ?? [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    if (HOP_BY_HOP_HEADERS.has(name.toLowerCase())) continue;
    filtered.push(name, value);
  }
  response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.statusMessage, filtered);
}

function sendJson(response, statusCode, payload, extraHeaders = {}) {
  if (response.headersSent) {
    response.end();
    return;
  }
  const body = `${JSON.stringify(payload)}\n`;
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body, 'utf-8'),
    ...extraHeaders,
  });
  response.end(body);
}

function sendError(response, statusCode, message) {
  sendJson(response, statusCode, { error: message });
}

/** Forward the request to one desktop, streaming both ways (so ``?follow=true`` keeps following). */
function proxyRequest(request, response, desktop) {
  return new Promise((resolve) => {
    const upstreamRequest = httpRequest(upstreamRequestOptions(request, desktop));

    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };

    upstreamRequest.on('error', (error) => {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Desktop ${desktop.deviceId} (${desktop.hostname}) is unreachable: ${message}`);
      settle();
    });

    upstreamRequest.on('response', (upstreamResponse) => {
      relayResponseHead(upstreamResponse, response);
      upstreamResponse.on('error', () => {
        if (!response.writableEnded) response.end();
        settle();
      });
      upstreamResponse.pipe(response);
      upstreamResponse.on('end', settle);
    });

    request.on('close', () => {
      if (!request.complete && !upstreamRequest.destroyed) upstreamRequest.destroy();
    });
    request.on('error', () => {
      if (!upstreamRequest.destroyed) upstreamRequest.destroy();
    });
    request.pipe(upstreamRequest);
  });
}

function readWholeBody(stream) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    stream.on('data', (chunk) => chunks.push(chunk));
    stream.on('end', () => resolve(Buffer.concat(chunks)));
    stream.on('error', reject);
  });
}

/**
 * Send the (buffered) request to one desktop and collect its whole response:
 * ``{desktop, status, contentType, body}`` for one that answered, or
 * ``{desktop, status: 502, failure}`` for one that could not be reached or
 * read. ``body`` replaces the request's own, so its length is sent afresh.
 */
function collectFromDesktop(request, body, desktop) {
  return new Promise((resolve) => {
    const options = upstreamRequestOptions(request, desktop);
    options.headers['content-length'] = String(body.length);
    const upstreamRequest = httpRequest(options);
    upstreamRequest.on('error', (error) => {
      const message = error instanceof Error ? error.message : String(error);
      resolve({ desktop, status: 502, failure: `is unreachable: ${message}` });
    });
    upstreamRequest.on('response', (upstreamResponse) => {
      readWholeBody(upstreamResponse).then(
        (upstreamBody) =>
          resolve({
            desktop,
            status: upstreamResponse.statusCode ?? 502,
            contentType: upstreamResponse.headers['content-type'] ?? null,
            body: upstreamBody.toString('utf-8'),
          }),
        (error) => {
          const message = error instanceof Error ? error.message : String(error);
          resolve({ desktop, status: 502, failure: `answered, but the response could not be read: ${message}` });
        },
      );
    });
    upstreamRequest.end(body);
  });
}

/** One desktop's collected response as an entry of an aggregated answer. */
function toResponseEntry(collected) {
  const entry = { device_id: collected.desktop.deviceId, hostname: collected.desktop.hostname, status: collected.status };
  if (collected.failure !== undefined) {
    return { ...entry, error: `Desktop ${collected.failure}` };
  }
  return { ...entry, content_type: collected.contentType, body: collected.body };
}

/** One desktop's collected response relayed as the whole answer, as if the request had gone there alone. */
function relayCollected(response, collected) {
  if (collected.failure !== undefined) {
    sendError(response, 502, `Desktop ${collected.desktop.deviceId} (${collected.desktop.hostname}) ${collected.failure}`);
    return;
  }
  const headers = collected.contentType === null ? {} : { 'Content-Type': collected.contentType };
  response.writeHead(collected.status, headers);
  response.end(collected.body);
}

/**
 * Forward the request to several desktops at once and answer with every
 * response side by side. The body is read whole first, since it is sent
 * once per desktop; a desktop that cannot be reached is one entry among the
 * others rather than a failure of the whole.
 */
async function broadcastRequest(request, response, desktops) {
  const body = await readWholeBody(request);
  const collected = await Promise.all(desktops.map((desktop) => collectFromDesktop(request, body, desktop)));
  sendJson(response, 200, { responses: collected.map(toResponseEntry) }, { [MULTIPLE_DESKTOPS_MATCHED_HEADER]: 'true' });
}

/**
 * The desktops a plural header names: every known one for ``*``, or the known
 * ones among those listed, in the order listed.
 */
function selectDesktops(desktops, selection) {
  if (selection.kind === 'all') return desktops;
  return selection.deviceIds
    .map((deviceId) => desktops.find((candidate) => candidate.deviceId === deviceId))
    .filter((desktop) => desktop !== undefined);
}

function listDesktops(response, desktops) {
  sendJson(response, 200, {
    devices: desktops.map((desktop) => ({
      device_id: desktop.deviceId,
      hostname: desktop.hostname,
      last_seen_at: desktop.lastSeenAt.toISOString(),
    })),
    announcement_interval_seconds: announcementIntervalSeconds(),
  });
}

/** Which desktops a filed request is recorded as being for: ``"*"`` or the device ids, known or not. */
function filedRequestDevices(selection, targets) {
  if (selection.kind === 'all') return ALL_DESKTOPS;
  if (selection.kind === 'one') return [selection.deviceId];
  if (selection.kind === 'many') return selection.deviceIds;
  return targets.length === 0 ? ALL_DESKTOPS : [targets[0].deviceId];
}

/**
 * File an agent's permission request: give it its id, forward it to the
 * desktops it is for and keep this machine's copy (see the header comment).
 * A body that is not a JSON object is forwarded as it is and never kept: the
 * desktops refuse it, and there is no shape to put an id into.
 */
async function fileRequest(request, response) {
  const rawBody = await readWholeBody(request);
  let parsed;
  try {
    parsed = JSON.parse(rawBody.toString('utf-8'));
  } catch (error) {
    parsed = undefined;
  }
  const isBodyAnObject = typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed);
  if (isBodyAnObject && parsed.request_id !== undefined) {
    throw new DesktopRoutingError(400, "A permission request's request_id is assigned by the gateway; the body must not carry one.");
  }
  const requestId = isBodyAnObject ? generateRequestId() : null;
  const body = isBodyAnObject ? Buffer.from(JSON.stringify({ ...parsed, request_id: requestId }), 'utf-8') : rawBody;
  const selection = parseDeviceHeader(request);
  const keep = (targets) =>
    writeFiledRequest({
      request_id: requestId,
      devices: filedRequestDevices(selection, targets),
      created_at: new Date().toISOString(),
      body: parsed,
    });

  // The copy is kept before the agent hears back, so an answer never precedes
  // the record a desktop would sync against.
  const keepIfTaken = (collected) => {
    const answered = collected.filter((entry) => entry.failure === undefined);
    const isAccepted = answered.some((entry) => entry.status >= 200 && entry.status < 300);
    if (requestId !== null && (isAccepted || answered.length === 0)) {
      keep(collected.map((entry) => entry.desktop));
    }
  };
  try {
    await routeToDesktops(request, response, { body, onCollected: keepIfTaken });
  } catch (error) {
    // No desktop was there to judge the request: it is kept for the ones the
    // header names, and the agent is told so along with why it went nowhere.
    const isNoDesktop =
      error instanceof DesktopRoutingError && error.statusCode === 503 && !(error instanceof DesktopsNotConfiguredError);
    if (!isNoDesktop || requestId === null) throw error;
    keep([]);
    throw new DesktopRoutingError(503, `${error.message} ${REQUEST_STORED_NOTE}`);
  }
}

/**
 * The desktops the header sends a request to: the most recently announced one
 * by default, the one named, every known one for ``*``, or the known ones among
 * those listed. A selection that comes down to no desktop is a 503.
 */
function selectTargets(selection, desktops) {
  if (selection.kind === 'default') {
    if (desktops.length === 0) throw new NoDesktopAnnouncedError();
    return [desktops[0]];
  }
  if (selection.kind === 'one') return [findDesktop(desktops, selection.deviceId)];
  const targets = selectDesktops(desktops, selection);
  if (targets.length === 0) {
    if (selection.kind === 'all') throw new NoDesktopAnnouncedError();
    throw new DesktopRoutingError(
      503,
      `None of the desktops ${DEVICE_HEADER} names (${selection.deviceIds.join(', ')}) is known to this gateway.`,
    );
  }
  return targets;
}

/**
 * Forward the request to the desktops its header names and answer the caller
 * with the one desktop's response, or with every response side by side. With
 * ``buffered``, its ``body`` is sent in place of the request's own, and what
 * each desktop answered is handed to its ``onCollected`` before the caller is
 * answered; without it the request streams through.
 */
async function routeToDesktops(request, response, buffered = null) {
  const targets = selectTargets(parseDeviceHeader(request), await readAnnouncedDesktops());
  if (buffered === null) {
    if (targets.length === 1) {
      await proxyRequest(request, response, targets[0]);
    } else {
      await broadcastRequest(request, response, targets);
    }
    return;
  }
  const collected = await Promise.all(targets.map((desktop) => collectFromDesktop(request, buffered.body, desktop)));
  buffered.onCollected(collected);
  if (targets.length === 1) {
    relayCollected(response, collected[0]);
  } else {
    sendJson(response, 200, { responses: collected.map(toResponseEntry) }, { [MULTIPLE_DESKTOPS_MATCHED_HEADER]: 'true' });
  }
}

export default async function desktopGatewayProxyExtension(request, response) {
  const pathOnly = new URL(request.url ?? '', 'http://placeholder.invalid').pathname;
  const isDevicesRoute = pathOnly === DEVICES_ROUTE;
  if (!isDevicesRoute && !isProxyRoute(pathOnly)) return false;
  const method = (request.method ?? 'GET').toUpperCase();

  try {
    if (isDevicesRoute) {
      if (method !== 'GET') {
        throw new DesktopRoutingError(405, `${DEVICES_ROUTE} only answers GET.`);
      }
      listDesktops(response, await readAnnouncedDesktops());
    } else if (pathOnly === PERMISSION_REQUESTS_ROUTE && method === 'POST') {
      await fileRequest(request, response);
    } else {
      if (method === 'DELETE' && pathOnly.startsWith(PERMISSION_REQUEST_ITEM_PATH_PREFIX)) {
        removeFiledRequest(pathOnly.slice(PERMISSION_REQUEST_ITEM_PATH_PREFIX.length));
      }
      await routeToDesktops(request, response);
    }
  } catch (error) {
    if (error instanceof DesktopRoutingError) {
      sendError(response, error.statusCode, error.message);
    } else if (!response.headersSent) {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Desktop routing failure: ${message}`);
    } else if (!response.writableEnded) {
      response.end();
    }
  }
  return true;
}
