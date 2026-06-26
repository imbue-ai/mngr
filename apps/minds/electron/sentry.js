const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
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
 * Parse the per-machine user config (`<dataDir>/config.toml`), or return `{}` when
 * the file is absent or unreadable.
 *
 * The config is written by the backend's MindsConfig when the user answers the
 * consent screen or toggles account settings; the Electron shell reads it
 * directly (no IPC) so the error-reporting settings stay in sync. Read live on
 * each call so toggling a setting takes effect without an app restart. Returning
 * `{}` on any failure means every setting falls back to its conservative default.
 */
function readUserConfig() {
  try {
    const configPath = path.join(paths.getDataDir(), 'config.toml');
    return parseToml(fs.readFileSync(configPath, 'utf8'));
  } catch {
    return {};
  }
}

/**
 * Whether the user has enabled automatic error reporting (default off).
 *
 * The browser web UI and this Electron main process both report automatic
 * errors only, so both honor the same per-machine `report_unexpected_errors`
 * user setting that gates the Python backend's automatic sends. Defaults to
 * false when the key is absent, so we never report without a confirmed opt-in.
 */
function isErrorReportingEnabled() {
  return readUserConfig().report_unexpected_errors === true;
}

/**
 * Whether the user has opted to include recent logs in error reports (default off).
 *
 * Mirrors the Python backend's `include_error_logs` setting (read live there by
 * imbue/minds/utils/sentry/core.py via `is_log_inclusion_enabled`), stored in the
 * same `<dataDir>/config.toml`. Gates the log attachments on the one-shot
 * backend-down manual report (see `captureManualReport`). Defaults to false when
 * the key is absent, so logs are never attached without a confirmed opt-in.
 */
function isLogInclusionEnabled() {
  return readUserConfig().include_error_logs === true;
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
    // Drop the native-crash (Crashpad) integration when the dev launcher asks
    // us to (it sets MINDS_DISABLE_CRASHPAD; see the `start` script in
    // package.json). Crashpad's `crashReporter.start()` spawns a
    // `chrome_crashpad_handler` that inherits the Electron process's stderr and
    // outlives the app on quit. The dev launcher pipes that stderr through
    // `concurrently` to prefix `[electron]` output, and `concurrently` waits for
    // EOF on the stream before its kill-others fires; the orphaned handler holds
    // the write end open, so EOF never comes and the launcher hangs after every
    // quit. Keying off the launcher's flag (not packaged-ness) keeps native-crash
    // minidump reporting on for every other run -- packaged builds and a plain
    // `electron .` alike.
    integrations: (defaults) =>
      process.env.MINDS_DISABLE_CRASHPAD === '1'
        ? defaults.filter((integration) => integration.name !== 'SentryMinidump')
        : defaults,
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

// -- Backend-down report enrichment (logs + host basics) --
//
// The full-app error takeover fires captureManualReport when the Python backend
// is down, so its rich /help collector (report_collector.py) is unreachable. We
// still attach what the main process can gather on its own: recent log files --
// including the backend's own minds-events.jsonl, which is still on disk even
// though that process died -- and cheap host/system basics. Live backend state
// (accounts, workspaces, discovery) is gone with the backend and is simply
// absent from a down-backend report.
//
// Unlike the backend (which uploads logs to S3 and references URLs in the event,
// to keep its high-volume automatic stream off Sentry's attachment quota and out
// of the ~1MB event limit), this rare one-shot path attaches gzipped logs
// directly via the SDK's native attachment channel -- no S3 client/credentials
// to duplicate here.

// Per-file cap on how much of each log we attach. We tail the most recent bytes
// (a crash surfaces at the end of the log; the head is rarely worth the size).
// gzip typically shrinks logs ~10x, so two ~5 MiB tails land far under Sentry's
// 40 MB compressed-envelope limit. Staying under that limit is load-bearing: an
// oversized envelope is dropped WHOLE (HTTP 413), which would lose the user's
// report along with the logs.
const MAX_LOG_TAIL_BYTES = 5 * 1024 * 1024;
// Defensive ceiling on total compressed attachment bytes, well below the 40 MB
// envelope limit, so we never risk a 413 even if gzip underperforms on logs that
// are already compact.
const MAX_TOTAL_COMPRESSED_ATTACHMENT_BYTES = 15 * 1024 * 1024;

/**
 * Read at most the last ``maxBytes`` of a file. Logs are tailed (not read whole)
 * because a crash surfaces at the end and the head is rarely worth the size.
 */
function readFileTail(filePath, maxBytes) {
  const { size } = fs.statSync(filePath);
  if (size <= maxBytes) return fs.readFileSync(filePath);
  const fd = fs.openSync(filePath, 'r');
  try {
    const buffer = Buffer.alloc(maxBytes);
    fs.readSync(fd, buffer, 0, maxBytes, size - maxBytes);
    return buffer;
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * The newest (by mtime) regular file in ``dir`` whose name satisfies
 * ``predicate``, or null when the directory is unreadable or has no match.
 */
function newestFileMatching(dir, predicate) {
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return null;
  }
  let newest = null;
  for (const name of names) {
    if (!predicate(name)) continue;
    const filePath = path.join(dir, name);
    try {
      const stat = fs.statSync(filePath);
      if (!stat.isFile()) continue;
      if (!newest || stat.mtimeMs > newest.mtimeMs) newest = { filePath, name, mtimeMs: stat.mtimeMs };
    } catch {
      // Raced/removed/permission-denied -- skip this candidate.
    }
  }
  return newest;
}

/**
 * Gzip the tails of the most relevant log files into Sentry attachments: the
 * live Python backend log (``*.jsonl``, written by the now-dead backend but
 * still on disk) and the Electron main-process log (``*.log``). Returns [] when
 * the logs directory is unreadable. Best-effort per file -- a file that can't be
 * read (or that would breach the compressed-attachment budget) is skipped rather
 * than aborting the whole report.
 */
function collectLogAttachments() {
  const logDir = paths.getLogDir();
  // The live backend log is exactly ``*.jsonl``; rotated logs are ``*.jsonl.<ts>``.
  // Matching the bare suffix keeps us on the live file rather than a rotated one.
  const candidates = [
    newestFileMatching(logDir, (name) => name.endsWith('.jsonl')),
    newestFileMatching(logDir, (name) => name.endsWith('.log')),
  ];
  const attachments = [];
  let totalCompressedBytes = 0;
  for (const candidate of candidates) {
    if (!candidate) continue;
    let compressed;
    try {
      compressed = zlib.gzipSync(readFileTail(candidate.filePath, MAX_LOG_TAIL_BYTES));
    } catch (err) {
      console.warn(`[report-error] could not read log ${candidate.name}: ${err && err.message}`);
      continue;
    }
    if (totalCompressedBytes + compressed.length > MAX_TOTAL_COMPRESSED_ATTACHMENT_BYTES) {
      console.warn(`[report-error] skipping log ${candidate.name}: compressed-attachment budget exceeded`);
      continue;
    }
    totalCompressedBytes += compressed.length;
    attachments.push({ filename: `${candidate.name}.gz`, data: compressed, contentType: 'application/gzip' });
  }
  return attachments;
}

/**
 * Cheap host/system facts the main process can gather without the backend,
 * mirroring the backend collector's "basics" + resource snapshot so a
 * backend-down report still carries version, platform, and load context.
 */
function collectSystemBasics() {
  const { releaseId, gitSha } = getBuildMetadata();
  const [load1m, load5m, load15m] = os.loadavg();
  const basics = {
    minds_release_id: releaseId,
    minds_git_sha: gitSha,
    platform: `${process.platform} ${os.release()} (${os.arch()})`,
    node_version: process.versions.node,
    cpu_count: os.cpus().length,
    load_average: { '1m': load1m, '5m': load5m, '15m': load15m },
    memory: { total_bytes: os.totalmem(), free_bytes: os.freemem() },
  };
  try {
    const stat = fs.statfsSync(paths.getDataDir());
    basics.disk = { total_bytes: stat.blocks * stat.bsize, free_bytes: stat.bavail * stat.bsize };
  } catch (err) {
    console.warn(`[report-error] could not stat data dir for disk usage: ${err && err.message}`);
  }
  return basics;
}

/**
 * Capture a user-initiated bug report from the Electron main process and return its event id.
 *
 * Used by the full-app error takeover (shell.html): when the Python backend has crashed its normal
 * /help report flow is unreachable, but this main-process Sentry is always initialized, so the user
 * can still file a report of the on-screen error. Alongside the message/details we attach host/system
 * basics, and -- when the user has opted into log inclusion -- recent log files (gzipped, tailed), so
 * the report is useful even though the backend's richer in-process state is gone with it.
 * Collection is best-effort: a failure to gather logs or basics never blocks the report itself.
 *
 * Log attachment honors the same opt-in as the backend and the /help flow: logs are attached when the
 * persistent ``include_error_logs`` setting is on, OR when the per-report ``includeLogs`` (the
 * takeover's "Include recent logs" checkbox, shown only when that setting is off) is set. Host basics
 * carry no log/file contents and are always included.
 *
 * The event is tagged ``manually_submitted`` so the ``beforeSend`` gate always lets it through, even
 * when automatic reporting is off (an explicit user action). Note this reports to the JavaScript
 * Sentry project, not the Python backend's project.
 *
 * Returns the Sentry event id (a 32-char hex string the user can quote), or null if Sentry dropped it.
 */
function captureManualReport({ message, details, includeLogs = false }) {
  let basics = null;
  try {
    basics = collectSystemBasics();
  } catch (err) {
    console.warn(`[report-error] could not collect system basics: ${err && err.message}`);
  }
  let attachments = [];
  if (isLogInclusionEnabled() || includeLogs) {
    try {
      attachments = collectLogAttachments();
    } catch (err) {
      console.warn(`[report-error] could not collect log attachments: ${err && err.message}`);
    }
  }
  // captureEvent's second arg is the event hint; attachments ride the envelope as
  // separate items (not in the event body). Omit the hint entirely when there are
  // none so we don't hand the SDK an empty attachments array.
  const hint = attachments.length ? { attachments } : undefined;
  return (
    Sentry.captureEvent(
      {
        message: message || 'Minds app error (manual report)',
        level: 'error',
        tags: { manually_submitted: 'true' },
        extra: { details: details || '', basics },
      },
      hint
    ) || null
  );
}

module.exports = {
  initSentry,
  isErrorReportingEnabled,
  isLogInclusionEnabled,
  resolveEnvironment,
  fixupReleaseId,
  captureManualReport,
};
