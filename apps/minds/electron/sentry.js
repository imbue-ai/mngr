const fs = require('fs');
const path = require('path');
const Sentry = require('@sentry/electron/main');
const { parse: parseToml } = require('smol-toml');
const paths = require('./paths');
const { getBuildMetadata } = require('./build-metadata');

// Error reporting for the Electron MAIN process. This mirrors the Python
// backend's Sentry setup (imbue/minds/utils/sentry/core.py) and the browser
// web-UI reporting (imbue/minds/utils/sentry/frontend.py): gated by the same
// per-machine `report_unexpected_errors` user setting, same environment
// selection, same release + git_sha tagging.
//
// The Electron main process reports to the SAME JavaScript Sentry projects as
// the browser web UI -- one JS project set (production / staging / dev) for all
// of minds' JavaScript. A "vanilla JS" Sentry project ingests events from both
// the @sentry/browser SDK and this @sentry/electron SDK fine, so there is no
// need for a separate Electron project. The three DSNs below are therefore the
// same values configured in imbue/minds/utils/sentry/frontend.py; the
// Python/JS language boundary forces this second copy, so the two MUST be kept
// in sync.
const SENTRY_FRONTEND_DSN_PRODUCTION = 'https://70356438f3a945b8e58cb0a6f8773d0a@o4504335315501056.ingest.us.sentry.io/4511620037804032';
const SENTRY_FRONTEND_DSN_STAGING = 'https://b8ce0a0ea4d38de2bda94e5ff6168572@o4504335315501056.ingest.us.sentry.io/4511620045144064';
const SENTRY_FRONTEND_DSN_DEV = 'https://ddc0f18beba95166b72eacd9d4b48bf0@o4504335315501056.ingest.us.sentry.io/4511620043243520';

/**
 * Whether the user has enabled automatic error reporting (default off).
 *
 * The browser web UI and this Electron main process both report automatic
 * errors only, so both honor the same per-machine `report_unexpected_errors`
 * user setting that gates the Python backend's automatic sends. The setting
 * lives in `<dataDir>/config.toml` (written by the backend's MindsConfig when
 * the user answers the consent screen or toggles account settings); the Electron
 * shell reads it directly so it stays in sync without any IPC. Read live on
 * every event so toggling the setting takes effect without an app restart.
 * Defaults to false when the file or key is absent or unreadable, so we never
 * report without a confirmed opt-in.
 */
function isErrorReportingEnabled() {
  try {
    const configPath = path.join(paths.getDataDir(), 'config.toml');
    const parsed = parseToml(fs.readFileSync(configPath, 'utf8'));
    return parsed.report_unexpected_errors === true;
  } catch {
    return false;
  }
}

/**
 * Select the Sentry environment from the resolved minds root name, mirroring
 * imbue.minds.utils.sentry.core.resolve_sentry_environment: only the exact
 * production / staging roots get their own target; everything else (dev-*,
 * ci-*, or no activated env -> the 'minds' default) maps to development.
 */
function resolveEnvironment() {
  const rootName = paths.getMindsRootName();
  if (rootName === 'minds') {
    return 'production';
  }
  if (rootName === 'minds-staging') {
    return 'staging';
  }
  return 'development';
}

function dsnForEnvironment(environment) {
  switch (environment) {
    case 'production':
      return SENTRY_FRONTEND_DSN_PRODUCTION;
    case 'staging':
      return SENTRY_FRONTEND_DSN_STAGING;
    default:
      return SENTRY_FRONTEND_DSN_DEV;
  }
}

/**
 * Normalize a release-candidate version into the semver form Sentry expects.
 * Mirrors imbue.minds.utils.sentry.core.fixup_release_id so the Electron shell,
 * the Python backend, and the web UI all report under the exact same release.
 */
function fixupReleaseId(releaseId) {
  return releaseId.replace(/(\d+\.\d+\.\d+)rc(\d+)/, '$1-rc.$2');
}

/**
 * Initialize Sentry for the Electron main process. The SDK is always
 * initialized so it is ready to capture startup errors, but a `beforeSend` hook
 * reads `report_unexpected_errors` live on every event and drops the event while
 * reporting is disabled -- mirroring the backend's automatic-reporting gate.
 * Reading the setting per event means toggling it (via the consent screen or
 * account settings) takes effect without restarting the app. Call this as early
 * as possible in main.js so startup errors are captured once reporting is on.
 */
function initSentry() {
  const environment = resolveEnvironment();
  const dsn = dsnForEnvironment(environment);
  const { releaseId, gitSha } = getBuildMetadata();
  Sentry.init({
    dsn,
    environment,
    release: fixupReleaseId(releaseId),
    // Error reporting only -- no performance tracing (matches the backend).
    tracesSampleRate: 0,
    // Keep PII out of reports, matching the backend's send_default_pii=False.
    sendDefaultPii: false,
    // Gate automatic sends on the user's live setting: drop every event while
    // reporting is disabled (matches the backend's _AutomaticReportingGate).
    // A manually-submitted bug report is an explicit user action, so it always
    // sends regardless of the setting -- mirroring the Python backend's
    // MANUALLY_SUBMITTED_TAG bypass in imbue/minds/utils/sentry/core.py.
    beforeSend: (event) => {
      if (event.tags && event.tags.manually_submitted === 'true') {
        return event;
      }
      return isErrorReportingEnabled() ? event : null;
    },
  });
  Sentry.setTag('git_sha', gitSha);
  console.log(
    `[sentry] Initialized (environment=${environment}, release=${fixupReleaseId(releaseId)}); ` +
      'automatic reporting gated live by the report_unexpected_errors user setting.'
  );
}

/**
 * Capture a user-initiated bug report from the Electron main process and return its event id.
 *
 * Used by the full-app error takeover (shell.html): when the Python backend has crashed its normal
 * /help report flow is unreachable, but this main-process Sentry is always initialized, so the user
 * can still file a one-shot report of the on-screen error. The event is tagged ``manually_submitted``
 * so the ``beforeSend`` gate always lets it through, even when automatic reporting is off (an explicit
 * user action). Note this reports to the JavaScript Sentry project, not the Python backend's project.
 *
 * Returns the Sentry event id (a 32-char hex string the user can quote), or null if Sentry dropped it.
 */
function captureManualReport({ message, details }) {
  return (
    Sentry.captureEvent({
      message: message || 'Minds app error (manual report)',
      level: 'error',
      tags: { manually_submitted: 'true' },
      extra: { details: details || '' },
    }) || null
  );
}

module.exports = { initSentry, isErrorReportingEnabled, resolveEnvironment, fixupReleaseId, captureManualReport };
