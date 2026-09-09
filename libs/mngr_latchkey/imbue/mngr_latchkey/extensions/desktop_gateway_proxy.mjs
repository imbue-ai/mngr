import { readFile } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';

const DESKTOP_GATEWAY_URL_ENV_VAR = 'LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL';
// The desktop's two secrets are named by *file path* rather than carried by
// value, and are read again for every proxied request. Both belong to whichever
// of the user's computers is currently connected -- the password is that
// gateway's own listen password, and the override JWT is signed by that
// computer's encryption key and names a path on its disk -- so both change when
// the user moves to another computer, while this gateway (and the workspace it
// serves) keeps running. Provisioning from the new computer rewrites the files;
// reading them per request is what makes the new values take effect without a
// gateway restart, and what keeps a proxied request from ever presenting the
// secrets of a computer that is no longer on the other end of the tunnel.
const DESKTOP_PASSWORD_FILE_ENV_VAR = 'LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE';
const DESKTOP_PERMISSIONS_OVERRIDE_FILE_ENV_VAR =
  'LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE_FILE';
const GATEWAY_PASSWORD_HEADER = 'X-Latchkey-Gateway-Password';
const PERMISSIONS_OVERRIDE_HEADER = 'X-Latchkey-Gateway-Permissions-Override';
// Prefix that asks for a third-party request to leave from the user's own
// machine rather than from this VPS (e.g. because the destination blocks
// datacenter IPs). The rest of the path is the absolute target URL, exactly
// as the gateway's own ``/gateway/<url>`` endpoint spells it, so unwrapping
// is a prefix swap: ``/via-desktop/https://a.com/x`` is forwarded to the
// desktop gateway as ``/gateway/https://a.com/x`` and handled there by its
// native outbound proxy. Credentials are injected, and the permission check
// runs, on the desktop -- against the same host permissions file this proxy
// already targets -- so routing this way grants nothing extra.
//
// Local workspaces never send this prefix: their gateway already runs on the
// user's machine, so minds leaves their prefix env var empty and they call
// ``/gateway/<url>`` directly.
const VIA_DESKTOP_PATH_PREFIX = '/via-desktop';
const GATEWAY_PATH_PREFIX = '/gateway/';

const PROXY_PATH_PREFIXES = [
  '/permissions',
  '/permission-requests',
  '/minds-api-proxy',
  VIA_DESKTOP_PATH_PREFIX,
];

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

class DesktopGatewayProxyError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'DesktopGatewayProxyError';
    this.statusCode = statusCode;
  }
}

class DesktopGatewayNotConfiguredError extends DesktopGatewayProxyError {
  constructor(detail) {
    super(503, `Desktop latchkey gateway proxy is not configured: ${detail}.`);
    this.name = 'DesktopGatewayNotConfiguredError';
  }
}

function resolveDesktopGatewayBase() {
  const raw = process.env[DESKTOP_GATEWAY_URL_ENV_VAR];
  if (raw === undefined || raw.length === 0) {
    throw new DesktopGatewayNotConfiguredError(
      `environment variable ${DESKTOP_GATEWAY_URL_ENV_VAR} is not set`,
    );
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new DesktopGatewayNotConfiguredError(
      `${DESKTOP_GATEWAY_URL_ENV_VAR}=${raw} is not a valid URL: ${message}`,
    );
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new DesktopGatewayNotConfiguredError(
      `${DESKTOP_GATEWAY_URL_ENV_VAR}=${raw} uses unsupported scheme '${parsed.protocol}' (expected http:// or https://)`,
    );
  }
  return parsed;
}

async function readSecretFile(envVarName) {
  const path = process.env[envVarName];
  if (path === undefined || path.length === 0) {
    throw new DesktopGatewayNotConfiguredError(`environment variable ${envVarName} is not set`);
  }
  let content;
  try {
    content = await readFile(path, 'utf-8');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new DesktopGatewayNotConfiguredError(`${envVarName}=${path} cannot be read: ${message}`);
  }
  const value = content.trim();
  if (value.length === 0) {
    throw new DesktopGatewayNotConfiguredError(`${envVarName}=${path} is empty`);
  }
  return value;
}

/**
 * Read the secrets that authenticate this hop to the desktop gateway.
 *
 * Read together (and afresh) so a request never mixes one computer's password
 * with another's override JWT: provisioning writes both, so a half-updated pair
 * is only ever momentary.
 */
async function resolveDesktopCredentials() {
  const [password, permissionsOverride] = await Promise.all([
    readSecretFile(DESKTOP_PASSWORD_FILE_ENV_VAR),
    readSecretFile(DESKTOP_PERMISSIONS_OVERRIDE_FILE_ENV_VAR),
  ]);
  return { password, permissionsOverride };
}

function isProxyRoute(pathOnly) {
  return PROXY_PATH_PREFIXES.some(
    (prefix) => pathOnly === prefix || pathOnly.startsWith(`${prefix}/`),
  );
}

/**
 * Translate an inbound request URL into the path to request upstream.
 *
 * Every proxied family but ``/via-desktop`` is forwarded verbatim, because the
 * desktop serves it from an extension mounted at the same path. ``/via-desktop``
 * is unwrapped onto the desktop gateway's native ``/gateway/<url>`` endpoint,
 * so the desktop needs no extension of its own.
 *
 * The remainder is required to be an absolute http(s) URL -- the same rule
 * ``extractTargetUrl`` enforces on the native route -- so a malformed target
 * fails here, on the machine the caller can see, rather than as an opaque 400
 * from a gateway two hops away. Operating on the raw request URL (never a
 * parsed one) keeps the target byte-identical to what the caller sent, matching
 * how the native endpoint slices its own prefix.
 */
function buildUpstreamPath(rawUrl, pathOnly) {
  const isViaDesktop =
    pathOnly === VIA_DESKTOP_PATH_PREFIX || pathOnly.startsWith(`${VIA_DESKTOP_PATH_PREFIX}/`);
  if (!isViaDesktop) return rawUrl;
  const target = rawUrl.startsWith(`${VIA_DESKTOP_PATH_PREFIX}/`)
    ? rawUrl.slice(VIA_DESKTOP_PATH_PREFIX.length + 1)
    : '';
  if (!target.startsWith('http://') && !target.startsWith('https://')) {
    throw new DesktopGatewayProxyError(
      400,
      `${VIA_DESKTOP_PATH_PREFIX} must be followed by an absolute http:// or https:// URL.`,
    );
  }
  return `${GATEWAY_PATH_PREFIX}${target}`;
}

/**
 * Copy the inbound headers, minus the two the desktop hop authenticates itself with.
 *
 * The caller's password authenticates it to *this* gateway and is a different
 * secret from the desktop gateway's own listen password, so it is dropped here
 * rather than forwarded; likewise the caller's permissions override, which
 * would otherwise let it pick the policy the desktop evaluates it against.
 */
function buildUpstreamHeaders(request, upstreamBase, desktopCredentials) {
  const headers = {};
  const rawHeaders = request.rawHeaders ?? [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    const lowerName = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(lowerName) ||
      lowerName === 'host' ||
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
  headers.host = upstreamBase.host;
  headers[GATEWAY_PASSWORD_HEADER] = desktopCredentials.password;
  headers[PERMISSIONS_OVERRIDE_HEADER] = desktopCredentials.permissionsOverride;
  return headers;
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

function sendError(response, statusCode, message) {
  if (response.headersSent) {
    response.end();
    return;
  }
  const body = `${JSON.stringify({ error: message })}\n`;
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body, 'utf-8'),
  });
  response.end(body);
}

function pickRequestImpl(upstreamBase) {
  return upstreamBase.protocol === 'https:' ? httpsRequest : httpRequest;
}

function proxyRequest(request, response, upstreamBase, desktopCredentials, upstreamPath) {
  return new Promise((resolve) => {
    const upstreamRequest = pickRequestImpl(upstreamBase)({
      protocol: upstreamBase.protocol,
      hostname: upstreamBase.hostname,
      port: upstreamBase.port.length > 0 ? upstreamBase.port : undefined,
      method: (request.method ?? 'GET').toUpperCase(),
      path: upstreamPath,
      headers: buildUpstreamHeaders(request, upstreamBase, desktopCredentials),
    });

    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };

    upstreamRequest.on('error', (error) => {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Desktop latchkey gateway is unreachable: ${message}`);
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

export default async function desktopGatewayProxyExtension(request, response) {
  const pathOnly = new URL(request.url ?? '', 'http://placeholder.invalid').pathname;
  if (!isProxyRoute(pathOnly)) return false;

  let upstreamBase;
  let desktopCredentials;
  let upstreamPath;
  try {
    upstreamBase = resolveDesktopGatewayBase();
    desktopCredentials = await resolveDesktopCredentials();
    upstreamPath = buildUpstreamPath(request.url ?? '/', pathOnly);
  } catch (error) {
    if (error instanceof DesktopGatewayProxyError) {
      sendError(response, error.statusCode, error.message);
      return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    sendError(response, 500, `Internal error: ${message}`);
    return true;
  }

  try {
    await proxyRequest(request, response, upstreamBase, desktopCredentials, upstreamPath);
  } catch (error) {
    if (!response.headersSent) {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Desktop latchkey gateway proxy failure: ${message}`);
    } else if (!response.writableEnded) {
      response.end();
    }
  }
  return true;
}
