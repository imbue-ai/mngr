'use strict';

// Formatting for the main process's GPU-process-death diagnostics. Kept free of
// any `electron` imports so it can be unit-tested under plain node.
//
// Chromium aborts the whole browser process (SIGTRAP, "GPU process isn't
// usable. Goodbye.") once the GPU process has died and every
// fallback mode is used up. The minidump says THAT it happened, not why the
// GPU process died or whether it died before or after the app was told to
// quit. Each death is therefore logged with its reason and exit code, where it
// falls relative to a termination signal and the shutdown, and how many GPU
// deaths this run has seen -- enough to tell a GPU process crash-looping on its
// own from one killed by a process-group signal during teardown. The free system
// memory and the app's total working set go on the same line, to tell a GPU
// process that died under memory pressure from one that died with plenty free.
// The free figure is `os.freemem()`, the same one Sentry's device context
// reports as free memory. On macOS it counts only free pages, not inactive or
// purgeable ones the OS can reclaim, so it reads low on a healthy Mac: compare
// it across deaths and against the app's own figure, not as an absolute.

const BYTES_PER_MB = 1024 * 1024;

/**
 * Render a byte count as whole megabytes (`1234MB`), or `unavailable` when it
 * could not be read.
 *
 * @param {number | null | undefined} bytes
 * @returns {string}
 */
function formatMegabytes(bytes) {
  return Number.isFinite(bytes) ? `${Math.round(bytes / BYTES_PER_MB)}MB` : 'unavailable';
}

/**
 * Render `app.getGPUFeatureStatus()` as compact `feature=status` pairs.
 *
 * The keys and values are Chromium's fixed feature names and status strings
 * (e.g. `gpu_compositing=disabled_software`), so nothing user-specific can
 * reach the log. Returns `unavailable` when no status object was supplied.
 *
 * @param {Record<string, string> | null | undefined} featureStatus
 * @returns {string}
 */
function formatGpuFeatureStatus(featureStatus) {
  if (!featureStatus || typeof featureStatus !== 'object') return 'unavailable';
  const pairs = Object.keys(featureStatus)
    .sort()
    .map((name) => `${name}=${featureStatus[name]}`);
  return pairs.length > 0 ? pairs.join(',') : 'unavailable';
}

/**
 * Build the log line for one GPU-process death.
 *
 * @param {object} state
 * @param {string} state.reason                 `details.reason` from `child-process-gone`.
 * @param {number} state.exitCode               `details.exitCode` from `child-process-gone`.
 * @param {number} state.gpuDeathCount          GPU deaths seen this run, including this one.
 * @param {boolean} state.isShuttingDown        A shutdown had already committed.
 * @param {string | null} state.terminationSignal The first SIGTERM/SIGINT received, or null.
 * @param {number} state.uptimeSeconds          Seconds since the main process started.
 * @param {Record<string, string> | null} state.featureStatus `app.getGPUFeatureStatus()`.
 * @param {number | null} state.freeMemoryBytes `os.freemem()`.
 * @param {number | null} state.appMemoryBytes  Summed working set of every app process, or null.
 * @returns {string}
 */
function formatGpuProcessGone({
  reason,
  exitCode,
  gpuDeathCount,
  isShuttingDown,
  terminationSignal,
  uptimeSeconds,
  featureStatus,
  freeMemoryBytes,
  appMemoryBytes,
}) {
  return (
    `[gpu] GPU process gone (reason=${reason}, exitCode=${exitCode}, ` +
    `gpuDeathCount=${gpuDeathCount}, isShuttingDown=${isShuttingDown}, ` +
    `terminationSignal=${terminationSignal || 'none'}, uptime=${uptimeSeconds.toFixed(1)}s, ` +
    `freeMemory=${formatMegabytes(freeMemoryBytes)}, appMemory=${formatMegabytes(appMemoryBytes)}, ` +
    `features=${formatGpuFeatureStatus(featureStatus)})`
  );
}

module.exports = { formatGpuFeatureStatus, formatGpuProcessGone, formatMegabytes };
