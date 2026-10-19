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
// own from one killed by a process-group signal during teardown.

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
}) {
  return (
    `[gpu] GPU process gone (reason=${reason}, exitCode=${exitCode}, ` +
    `gpuDeathCount=${gpuDeathCount}, isShuttingDown=${isShuttingDown}, ` +
    `terminationSignal=${terminationSignal || 'none'}, uptime=${uptimeSeconds.toFixed(1)}s, ` +
    `features=${formatGpuFeatureStatus(featureStatus)})`
  );
}

module.exports = { formatGpuFeatureStatus, formatGpuProcessGone };
