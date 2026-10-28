'use strict';

// Crashpad annotations for the Electron MAIN (browser) process. Kept free of
// any `electron` imports so it can be unit-tested under plain node; main.js
// hands in Electron's `crashReporter`.
//
// A native crash of the browser process reaches Sentry as a minidump whose
// Electron Framework frames are usually unsymbolicated, so the stack cannot say
// which component trapped. Breadcrumbs ride along too, but the SDK persists the
// scope to disk asynchronously, so the last second before a crash can be
// missing from them. Crashpad annotations (`crashReporter.addExtraParameter`)
// live in process memory and are copied into the dump at the moment of the
// crash, so they are exact. They surface on the Sentry event under the
// `Electron Framework` / `electron` contexts as `crashpad.<key>`.
//
// Three annotations, each one compact `key=value` string, overwritten in place:
//
//   minds_last_nav    the last navigation milestone of any window/frame
//                     (start / redirect / commit / fail), with the CLASS of
//                     the target origin -- never the URL.
//   minds_memory      a periodic snapshot of the app's process working sets
//                     and the system's free memory.
//   minds_last_event  the name of the last main-process event handled (window
//                     focus/blur/show, notification show/click, updater
//                     status type, sleep/wake/screen lock, quit) -- never its
//                     payload.
//
// Every value carries `uptime_ms`, the main process's uptime when it was
// written. The event's crash time minus the `app` context's `app_start_time`
// gives the uptime at the crash, so the two together say how stale each
// annotation was when the process died.

const { parseWorkspaceId } = require('./surface-routing');

const NAV_KEY = 'minds_last_nav';
const MEMORY_KEY = 'minds_memory';
const LAST_EVENT_KEY = 'minds_last_event';

const NAV_PHASES = new Set(['start', 'redirect', 'commit', 'fail']);

// The workspace iframe's redirect chain passes through these backend paths
// before it lands on the workspace origin (see wireBundleNavigationEvents).
const BRIDGE_PATH_PATTERN = /^\/(?:forward-bridge|_bridge|goto)(?:\/|$)/;
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

/**
 * Classify a navigation target without recording any part of it.
 *
 * - `workspace`: a workspace origin served by the forward proxy.
 * - `bridge`: a hop of the workspace iframe's redirect chain on the backend.
 * - `backend`: any other loopback http(s) page (the SPA and its routes).
 * - `internal`: a non-http(s) document (shell.html, about:blank, data:, ...).
 * - `external`: any other http(s) origin.
 * - `invalid`: not a parseable URL.
 *
 * @param {string | null | undefined} url
 * @returns {'workspace' | 'bridge' | 'backend' | 'internal' | 'external' | 'invalid'}
 */
function classifyNavigationTarget(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return 'invalid';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return 'internal';
  // Checked before parseWorkspaceId, which also accepts the backend's
  // /goto/<id> path -- a bridge hop here, not the workspace origin itself.
  if (LOOPBACK_HOSTS.has(parsed.hostname)) {
    return BRIDGE_PATH_PATTERN.test(parsed.pathname) ? 'bridge' : 'backend';
  }
  return parseWorkspaceId(url) ? 'workspace' : 'external';
}

/**
 * Summarize `app.getAppMetrics()` and `process.getSystemMemoryInfo()` (both in
 * KB) as whole MB. Chromium's browser-process type is `Browser`.
 *
 * @param {{type: string, memory: {workingSetSize: number}}[]} appMetrics
 * @param {{free: number} | null} systemMemory
 * @returns {{browserWorkingSetMb: number | null, totalWorkingSetMb: number, processCount: number, systemFreeMb: number | null}}
 */
function summarizeMemory(appMetrics, systemMemory) {
  const kbToMb = (kb) => Math.round(kb / 1024);
  let browserKb = null;
  let totalKb = 0;
  for (const metric of appMetrics) {
    const kb = (metric.memory && metric.memory.workingSetSize) || 0;
    totalKb += kb;
    if (metric.type === 'Browser') browserKb = kb;
  }
  return {
    browserWorkingSetMb: browserKb === null ? null : kbToMb(browserKb),
    totalWorkingSetMb: kbToMb(totalKb),
    processCount: appMetrics.length,
    systemFreeMb: systemMemory && typeof systemMemory.free === 'number' ? kbToMb(systemMemory.free) : null,
  };
}

/**
 * Build the annotator. With a null `reporter` (Crashpad not started, e.g. the
 * dev launcher's MINDS_DISABLE_CRASHPAD) every method is a no-op: Electron
 * requires `crashReporter.start()` before any other crashReporter call.
 *
 * Never throws: a failing write is logged once, then dropped silently.
 *
 * @param {{addExtraParameter: (key: string, value: string) => void} | null} reporter
 * @param {{uptimeMs?: () => number, warn?: (message: string) => void}} [options]
 */
function createCrashAnnotator(reporter, options = {}) {
  const uptimeMs = options.uptimeMs || (() => Math.round(process.uptime() * 1000));
  const warn = options.warn || ((message) => console.warn(message));
  let hasWarned = false;

  const write = (key, fields) => {
    if (reporter === null) return;
    const value = Object.entries({ ...fields, uptime_ms: uptimeMs() })
      .map(([name, field]) => `${name}=${field}`)
      .join(' ');
    try {
      reporter.addExtraParameter(key, value);
    } catch (err) {
      if (hasWarned) return;
      hasWarned = true;
      warn(`[crash-annotations] could not set a crashpad annotation (further failures are silent): ${err.message || err}`);
    }
  };

  return {
    /**
     * @param {{windowId: number, isMainFrame: boolean, phase: string, url: string, isSameDocument: boolean}} nav
     */
    recordNavigation({ windowId, isMainFrame, phase, url, isSameDocument }) {
      write(NAV_KEY, {
        window: windowId,
        frame: isMainFrame ? 'main' : 'sub',
        phase: NAV_PHASES.has(phase) ? phase : 'unknown',
        target: classifyNavigationTarget(url),
        same_doc: Boolean(isSameDocument),
      });
    },

    /**
     * @param {{type: string, memory: {workingSetSize: number}}[]} appMetrics
     * @param {{free: number} | null} systemMemory
     */
    recordMemory(appMetrics, systemMemory) {
      const summary = summarizeMemory(appMetrics, systemMemory);
      write(MEMORY_KEY, {
        browser_ws_mb: summary.browserWorkingSetMb === null ? 'unknown' : summary.browserWorkingSetMb,
        total_ws_mb: summary.totalWorkingSetMb,
        processes: summary.processCount,
        system_free_mb: summary.systemFreeMb === null ? 'unknown' : summary.systemFreeMb,
      });
    },

    /** @param {string} eventName A fixed event name; never a payload. */
    recordMainEvent(eventName) {
      write(LAST_EVENT_KEY, { event: eventName });
    },
  };
}

module.exports = {
  LAST_EVENT_KEY,
  MEMORY_KEY,
  NAV_KEY,
  classifyNavigationTarget,
  createCrashAnnotator,
  summarizeMemory,
};
