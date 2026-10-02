/**
 * Latchkey gateway extension for the desktop gateway: the one desktop it runs on.
 *
 * Endpoints:
 *   GET /devices -> this desktop, as the one device a local workspace can address
 *
 * Answers in the shape a remote host's machine answers it in
 * (``desktop_gateway_proxy.mjs``), so a workspace lists the user's desktops
 * the same way whether its gateway is local or remote. The desktop is named by
 * ``LATCHKEY_EXTENSION_LOCAL_DEVICE_ID`` and ``..._HOSTNAME``; it is always
 * connected to its own gateway, so it is reported as seen just now.
 *
 * Every other URL is left for the next extension.
 */

const LOCAL_DEVICE_ID_ENV_VAR = 'LATCHKEY_EXTENSION_LOCAL_DEVICE_ID';
const LOCAL_DEVICE_HOSTNAME_ENV_VAR = 'LATCHKEY_EXTENSION_LOCAL_DEVICE_HOSTNAME';
const DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR = 'LATCHKEY_EXTENSION_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS';
const DEFAULT_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS = 30;
const DEVICES_ROUTE = '/devices';

class DeviceListError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.name = 'DeviceListError';
    this.statusCode = statusCode;
  }
}

function requiredEnv(name) {
  const value = process.env[name];
  if (value === undefined || value.length === 0) {
    throw new DeviceListError(503, `The local device is not configured on this gateway: ${name} is not set.`);
  }
  return value;
}

function announcementIntervalSeconds() {
  const raw = process.env[DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR];
  if (raw === undefined || raw.length === 0) return DEFAULT_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    throw new DeviceListError(
      503,
      `The local device is not configured on this gateway: ${DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR}=${raw} is not a positive number of seconds.`,
    );
  }
  return seconds;
}

function sendJson(response, statusCode, payload) {
  const body = `${JSON.stringify(payload)}\n`;
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body, 'utf-8'),
  });
  response.end(body);
}

export default async function deviceListExtension(request, response) {
  const pathOnly = new URL(request.url ?? '', 'http://placeholder.invalid').pathname;
  if (pathOnly !== DEVICES_ROUTE) return false;

  try {
    if ((request.method ?? 'GET').toUpperCase() !== 'GET') {
      throw new DeviceListError(405, `${DEVICES_ROUTE} only answers GET.`);
    }
    sendJson(response, 200, {
      devices: [
        {
          device_id: requiredEnv(LOCAL_DEVICE_ID_ENV_VAR),
          hostname: requiredEnv(LOCAL_DEVICE_HOSTNAME_ENV_VAR),
          last_seen_at: new Date().toISOString(),
        },
      ],
      announcement_interval_seconds: announcementIntervalSeconds(),
    });
  } catch (error) {
    if (!(error instanceof DeviceListError)) throw error;
    sendJson(response, error.statusCode, { error: error.message });
  }
  return true;
}
