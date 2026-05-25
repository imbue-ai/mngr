/**
 * Latchkey gateway extension: transparent HTTP reverse proxy from the
 * gateway to the minds desktop client's bare-origin "Minds API" server.
 *
 * Endpoints:
 *   ANY /minds-api-proxy            -> <minds-api>/
 *   ANY /minds-api-proxy/<rest>...  -> <minds-api>/<rest>...
 *
 * Every other URL is left for the next extension (this handler returns
 * ``false`` without touching the response).
 *
 * The upstream base URL is read from the
 * ``LATCHKEY_EXTENSION_MINDS_API_URL`` environment variable on every
 * request, so the minds desktop client can rebind its server to a new
 * port and simply restart the ``mngr latchkey forward`` supervisor with
 * the fresh value: the next request through the proxy picks up the new
 * URL without any in-process cache to invalidate. When the env var is
 * unset / empty / unparseable, the proxy responds 503 so callers see a
 * deterministic "not configured" failure rather than a hung connection.
 *
 * The proxy authenticates *to* the Minds API using the Authorization Bearer
 * header populated from the ``LATCHKEY_EXTENSION_MINDS_API_KEY`` environment
 * variable. When the env var is not set, the inbound ``Authorization`` header
 * is passed through unchanged (the Minds API will then 401 the request); this
 * keeps the proxy useful for tests that don't bother stubbing the key.
 *
 * The proxy is intentionally transparent in every other respect: it
 * forwards the request method, the path-and-query suffix, the inbound
 * body, and the request headers (minus hop-by-hop headers, the
 * gateway-internal password / permissions-override headers, and the
 * ``Authorization`` header which is always replaced when the key env
 * var is set), and streams the upstream response status, headers, and
 * body straight back. Restricting which paths an agent may reach is
 * the job of the agent's ``latchkey_permissions.json``.
 *
 * NOTE: extension requests still go through the gateway's permission
 * check, so callers must have a rule that allows them to talk to
 * ``latchkey-self.invalid`` on the relevant method/path.
 */

import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';

const PROXY_PATH_PREFIX = '/minds-api-proxy';
const MINDS_API_URL_ENV_VAR = 'LATCHKEY_EXTENSION_MINDS_API_URL';
const MINDS_API_KEY_ENV_VAR = 'LATCHKEY_EXTENSION_MINDS_API_KEY';

// Hop-by-hop headers per RFC 7230 section 6.1. Stripped from both the
// inbound request before we forward upstream and the upstream response
// before we relay it back, because they describe the lifetime of a
// single TCP hop and must not leak across one.
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

// Gateway-internal headers that the upstream Minds API has no business
// seeing -- they identify the caller to the gateway, not to the proxied
// service. Mirrors the gateway's own ``GATEWAY_INTERNAL_HEADERS``
// constant (we don't import it because extensions only see Node's raw
// HTTP API, not the gateway internals).
const GATEWAY_INTERNAL_HEADERS = new Set([
  'x-latchkey-gateway-password',
  'x-latchkey-gateway-permissions-override',
]);

class MindsApiProxyError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'MindsApiProxyError';
    this.statusCode = statusCode;
  }
}

class MindsApiNotConfiguredError extends MindsApiProxyError {
  constructor(detail) {
    super(503, `Minds API proxy is not configured: ${detail}.`);
    this.name = 'MindsApiNotConfiguredError';
  }
}

/**
 * Parse the upstream base URL out of the environment. Returns a
 * ``URL`` object representing the base (path is preserved as a prefix
 * to apply when rewriting). Throws ``MindsApiNotConfiguredError`` when
 * the env var is missing/empty/unparseable or uses an unsupported
 * scheme.
 */
function resolveUpstreamBase() {
  const raw = process.env[MINDS_API_URL_ENV_VAR];
  if (raw === undefined || raw.length === 0) {
    throw new MindsApiNotConfiguredError(`environment variable ${MINDS_API_URL_ENV_VAR} is not set`);
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new MindsApiNotConfiguredError(`${MINDS_API_URL_ENV_VAR}=${raw} is not a valid URL: ${message}`);
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new MindsApiNotConfiguredError(
      `${MINDS_API_URL_ENV_VAR}=${raw} uses unsupported scheme '${parsed.protocol}' (expected http:// or https://)`,
    );
  }
  return parsed;
}

function isProxyRoute(pathOnly) {
  if (pathOnly === PROXY_PATH_PREFIX) return true;
  return pathOnly.startsWith(`${PROXY_PATH_PREFIX}/`);
}

/**
 * Compute the path-and-query string to send upstream. The request URL
 * is path-and-query only (no scheme/host) on Node's incoming request,
 * so we strip the ``/minds-api-proxy`` prefix and stitch the
 * remainder onto the upstream base's own path.
 *
 * Examples (with base ``http://127.0.0.1:8420``):
 *   /minds-api-proxy           -> /
 *   /minds-api-proxy/          -> /
 *   /minds-api-proxy/foo?x=1   -> /foo?x=1
 *
 * Examples (with base ``http://127.0.0.1:8420/api``):
 *   /minds-api-proxy           -> /api
 *   /minds-api-proxy/foo?x=1   -> /api/foo?x=1
 */
function buildUpstreamPath(requestUrl, upstreamBase) {
  // ``requestUrl`` already starts with ``/``; use a placeholder origin
  // so WHATWG URL can split path and query without us re-implementing
  // it.
  const parsed = new URL(requestUrl ?? '', 'http://placeholder.invalid');
  let suffix = parsed.pathname.slice(PROXY_PATH_PREFIX.length);
  if (suffix.length === 0) suffix = '/';
  // Trim a single trailing slash off the base path so concatenation
  // does not produce ``//foo``. An empty / ``/`` base path means
  // "upstream root", and ``suffix`` already starts with ``/``.
  const basePath = upstreamBase.pathname === '/' ? '' : upstreamBase.pathname.replace(/\/$/, '');
  return `${basePath}${suffix}${parsed.search}`;
}

/**
 * Build the headers object to forward upstream. Iterates over
 * ``request.rawHeaders`` (the ``[name, value, name, value, ...]`` shape
 * that preserves original case and multi-value entries verbatim) rather
 * than ``request.headers`` so we can drop dropped-name entries cleanly
 * even when they appear multiple times.
 *
 * The ``Host`` header is overwritten with the upstream's authority so
 * upstreams that vhost on it (or just log it) see the right value.
 */
function buildUpstreamHeaders(request, upstreamBase) {
  // When the proxy is configured with an API key we drop any inbound
  // ``Authorization`` header outright so agents cannot spoof one; the
  // replacement value is appended at the end of this function. When
  // the env var is not set we pass any inbound value through verbatim
  // (the Minds API will then 401 -- this is fine for tests).
  const apiKey = process.env[MINDS_API_KEY_ENV_VAR];
  const overwriteAuthorization = typeof apiKey === 'string' && apiKey.length > 0;

  const headers = {};
  const rawHeaders = request.rawHeaders ?? [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    const lowerName = name.toLowerCase();
    if (HOP_BY_HOP_HEADERS.has(lowerName)) continue;
    if (GATEWAY_INTERNAL_HEADERS.has(lowerName)) continue;
    if (lowerName === 'host') continue;
    if (overwriteAuthorization && lowerName === 'authorization') continue;
    const existing = headers[name];
    if (existing === undefined) {
      headers[name] = value;
    } else if (Array.isArray(existing)) {
      existing.push(value);
    } else {
      headers[name] = [existing, value];
    }
  }
  headers['host'] = upstreamBase.host;
  if (overwriteAuthorization) {
    headers['authorization'] = `Bearer ${apiKey}`;
  }
  return headers;
}

/**
 * Write response headers minus hop-by-hop entries. ``rawHeaders`` is
 * the same flat array shape as on the request side; we filter inline
 * and preserve duplicates verbatim by passing the original array
 * (minus dropped entries) straight to ``writeHead``.
 */
function relayResponseHead(upstreamResponse, response) {
  const filtered = [];
  const rawHeaders = upstreamResponse.rawHeaders ?? [];
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index];
    const value = rawHeaders[index + 1];
    if (HOP_BY_HOP_HEADERS.has(name.toLowerCase())) continue;
    filtered.push(name, value);
  }
  // ``writeHead(statusCode, statusMessage, headers)`` accepts a flat
  // ``[name, value, ...]`` array directly, preserving duplicate header
  // names (which ``Object.fromEntries`` would collapse).
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

/**
 * Open the upstream request, pipe the inbound body into it, and pipe
 * the upstream response back. Returns a promise that settles once both
 * directions have completed (or one of them errored).
 */
function proxyRequest(request, response, upstreamBase) {
  return new Promise((resolve) => {
    const upstreamRequest = pickRequestImpl(upstreamBase)({
      protocol: upstreamBase.protocol,
      hostname: upstreamBase.hostname,
      port: upstreamBase.port.length > 0 ? upstreamBase.port : undefined,
      method: (request.method ?? 'GET').toUpperCase(),
      path: buildUpstreamPath(request.url, upstreamBase),
      headers: buildUpstreamHeaders(request, upstreamBase),
    });

    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };

    upstreamRequest.on('error', (error) => {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Minds API proxy upstream error: ${message}`);
      settle();
    });

    upstreamRequest.on('response', (upstreamResponse) => {
      relayResponseHead(upstreamResponse, response);
      upstreamResponse.on('error', () => {
        // If the upstream connection drops mid-stream, just close the
        // downstream socket. Headers are already sent so we cannot
        // surface an error body.
        if (!response.writableEnded) response.end();
        settle();
      });
      upstreamResponse.pipe(response);
      upstreamResponse.on('end', settle);
    });

    // If the downstream client disconnects before sending the full
    // request body, abort the upstream request so we don't leak a
    // socket. ``request.complete`` is true iff the inbound HTTP
    // message has been fully parsed; ``close`` is otherwise also
    // emitted on normal end-of-stream (including empty-body GETs once
    // they have been piped), and aborting then would tear down the
    // upstream socket before it had a chance to respond.
    request.on('close', () => {
      if (!request.complete && !upstreamRequest.destroyed) {
        upstreamRequest.destroy();
      }
    });
    request.on('error', () => {
      if (!upstreamRequest.destroyed) upstreamRequest.destroy();
    });

    // Pipe the inbound request body straight through. For methods that
    // carry no body (GET/HEAD) the source stream simply ends without
    // emitting any data.
    request.pipe(upstreamRequest);
  });
}

export default async function mindsApiProxyExtension(request, response) {
  const pathOnly = new URL(request.url ?? '', 'http://placeholder.invalid').pathname;
  if (!isProxyRoute(pathOnly)) {
    return false;
  }

  let upstreamBase;
  try {
    upstreamBase = resolveUpstreamBase();
  } catch (error) {
    if (error instanceof MindsApiProxyError) {
      sendError(response, error.statusCode, error.message);
      return true;
    }
    const message = error instanceof Error ? error.message : String(error);
    sendError(response, 500, `Internal error: ${message}`);
    return true;
  }

  try {
    await proxyRequest(request, response, upstreamBase);
  } catch (error) {
    if (!response.headersSent) {
      const message = error instanceof Error ? error.message : String(error);
      sendError(response, 502, `Minds API proxy failure: ${message}`);
    } else if (!response.writableEnded) {
      response.end();
    }
  }
  return true;
}
