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
 * Every other URL is left for the next extension.
 */

import { readdir, readFile, stat } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
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
const PROXY_PATH_PREFIXES = ['/permissions', '/permission-requests', '/minds-api-proxy'];

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

/** Send the (buffered) request to one desktop and collect its whole response as one entry. */
function collectFromDesktop(request, body, desktop) {
  const entry = { device_id: desktop.deviceId, hostname: desktop.hostname };
  return new Promise((resolve) => {
    const upstreamRequest = httpRequest(upstreamRequestOptions(request, desktop));
    upstreamRequest.on('error', (error) => {
      const message = error instanceof Error ? error.message : String(error);
      resolve({ ...entry, status: 502, error: `Desktop is unreachable: ${message}` });
    });
    upstreamRequest.on('response', (upstreamResponse) => {
      readWholeBody(upstreamResponse).then(
        (upstreamBody) =>
          resolve({
            ...entry,
            status: upstreamResponse.statusCode ?? 502,
            content_type: upstreamResponse.headers['content-type'] ?? null,
            body: upstreamBody.toString('utf-8'),
          }),
        (error) => {
          const message = error instanceof Error ? error.message : String(error);
          resolve({ ...entry, status: 502, error: `Desktop's response could not be read: ${message}` });
        },
      );
    });
    upstreamRequest.end(body);
  });
}

/**
 * Forward the request to several desktops at once and answer with every
 * response side by side. The body is read whole first, since it is sent
 * once per desktop; a desktop that cannot be reached is one entry among the
 * others rather than a failure of the whole.
 */
async function broadcastRequest(request, response, desktops) {
  const body = await readWholeBody(request);
  const responses = await Promise.all(desktops.map((desktop) => collectFromDesktop(request, body, desktop)));
  sendJson(response, 200, { responses }, { [MULTIPLE_DESKTOPS_MATCHED_HEADER]: 'true' });
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

async function routeToDesktops(request, response) {
  const selection = parseDeviceHeader(request);
  const desktops = await readAnnouncedDesktops();

  if (selection.kind === 'default') {
    if (desktops.length === 0) throw new NoDesktopAnnouncedError();
    await proxyRequest(request, response, desktops[0]);
    return;
  }
  if (selection.kind === 'one') {
    await proxyRequest(request, response, findDesktop(desktops, selection.deviceId));
    return;
  }
  const targets = selectDesktops(desktops, selection);
  if (targets.length === 0) {
    if (selection.kind === 'all') throw new NoDesktopAnnouncedError();
    throw new DesktopRoutingError(
      503,
      `None of the desktops ${DEVICE_HEADER} names (${selection.deviceIds.join(', ')}) is known to this gateway.`,
    );
  }
  if (targets.length === 1) {
    await proxyRequest(request, response, targets[0]);
    return;
  }
  await broadcastRequest(request, response, targets);
}

export default async function desktopGatewayProxyExtension(request, response) {
  const pathOnly = new URL(request.url ?? '', 'http://placeholder.invalid').pathname;
  const isDevicesRoute = pathOnly === DEVICES_ROUTE;
  if (!isDevicesRoute && !isProxyRoute(pathOnly)) return false;

  try {
    if (isDevicesRoute) {
      if ((request.method ?? 'GET').toUpperCase() !== 'GET') {
        throw new DesktopRoutingError(405, `${DEVICES_ROUTE} only answers GET.`);
      }
      listDesktops(response, await readAnnouncedDesktops());
    } else {
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
