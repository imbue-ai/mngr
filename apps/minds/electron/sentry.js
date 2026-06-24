const Sentry = require('@sentry/electron/main');
const paths = require('./paths');
const { getBuildMetadata } = require('./build-metadata');

// Error reporting for the Electron MAIN process. This mirrors the Python
// backend's Sentry setup (imbue/minds/utils/sentry/core.py) and the browser
// web-UI reporting (imbue/minds/utils/sentry/frontend.py): same
// MINDS_SENTRY_ENABLED opt-in, same environment selection, same release +
// git_sha tagging.
//
// The Electron main process reports to the SAME JavaScript Sentry projects as
// the browser web UI -- one JS project set (production / staging / dev) for all
// of minds' JavaScript. A "vanilla JS" Sentry project ingests events from both
// the @sentry/browser SDK and this @sentry/electron SDK fine, so there is no
// need for a separate Electron project. The three DSNs below are therefore the
// same values configured in imbue/minds/utils/sentry/frontend.py; the
// Python/JS language boundary forces this second copy, so the two MUST be kept
// in sync. They are PLACEHOLDERS until the real DSNs are pasted in (replacing
// the __REPLACE_ME__ markers); until then init is skipped, so a misconfigured
// DSN never points the SDK at a bogus endpoint.
const SENTRY_FRONTEND_DSN_PRODUCTION = 'https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__';
const SENTRY_FRONTEND_DSN_STAGING = 'https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__';
const SENTRY_FRONTEND_DSN_DEV = 'https://__REPLACE_ME__@o4504335315501056.ingest.us.sentry.io/__REPLACE_ME__';

const PLACEHOLDER_DSN_MARKER = '__REPLACE_ME__';
// Mirror imbue.minds.utils.sentry.core._SENTRY_ENABLED_TRUTHY_VALUES.
const SENTRY_ENABLED_TRUTHY_VALUES = ['1', 'true', 'yes'];

/**
 * Whether error reporting is opted in via MINDS_SENTRY_ENABLED (default off).
 * Mirrors imbue.minds.utils.sentry.core.is_sentry_enabled so the Electron shell
 * and the Python backend honor the same single switch.
 */
function isSentryEnabled() {
  const raw = (process.env.MINDS_SENTRY_ENABLED || '').trim().toLowerCase();
  return SENTRY_ENABLED_TRUTHY_VALUES.includes(raw);
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
 * Initialize Sentry for the Electron main process. No-op unless reporting is
 * enabled and a real (non-placeholder) DSN is configured for the environment.
 * Call this as early as possible in main.js so startup errors are captured.
 */
function initSentry() {
  if (!isSentryEnabled()) {
    console.log('[sentry] Disabled (set MINDS_SENTRY_ENABLED=1 to enable error reporting).');
    return;
  }
  const environment = resolveEnvironment();
  const dsn = dsnForEnvironment(environment);
  if (dsn.includes(PLACEHOLDER_DSN_MARKER)) {
    console.log(`[sentry] No DSN configured for environment "${environment}"; skipping init.`);
    return;
  }
  const { releaseId, gitSha } = getBuildMetadata();
  Sentry.init({
    dsn,
    environment,
    release: fixupReleaseId(releaseId),
    // Error reporting only -- no performance tracing (matches the backend).
    tracesSampleRate: 0,
    // Keep PII out of reports, matching the backend's send_default_pii=False.
    sendDefaultPii: false,
  });
  Sentry.setTag('git_sha', gitSha);
  console.log(`[sentry] Initialized (environment=${environment}, release=${fixupReleaseId(releaseId)}).`);
}

module.exports = { initSentry, isSentryEnabled, resolveEnvironment, fixupReleaseId };
