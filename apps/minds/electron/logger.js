// Single-instance logger for the Electron main process.
//
// Owns:
//   - a `LogWriter` writing to ~/.minds/logs/electron.jsonl (local-only records)
//   - an `IframeLogBuffer` that POSTs MIND-destined records back to each
//     mind's workspace server via the desktop-client subdomain proxy
//   - a `LogRouter` holding the pure routing logic between the two, so
//     the routing decisions can be exercised without Electron.
//
// Consumers in main.js / backend.js call `logMain()` for their own
// `console.log`-replacement messages and `attachConsoleListener()` once per
// WebContentsView to route renderer messages through the classifier.

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { app, net } = require('electron');

const paths = require('./paths');
const { LogWriter } = require('./log-writer');
const { IframeLogBuffer } = require('./iframe-log-buffer');
const { LogRouter } = require('./log-router');

const FLUSH_INTERVAL_MS = 1000;

let _writer = null;
let _buffer = null;
let _router = null;
/** @type {NodeJS.Timeout|null} */
let _flushTimer = null;

function init() {
  if (_router !== null) return;
  const logDir = paths.getLogDir();
  fs.mkdirSync(logDir, { recursive: true });
  _writer = new LogWriter(path.join(logDir, 'electron.jsonl'));
  _buffer = new IframeLogBuffer({
    fetchFn: async (url, fetchInit) => {
      const response = await net.fetch(url, fetchInit);
      return { ok: response.ok, status: response.status };
    },
    onError: (err, mindId) => {
      // Degrade gracefully: a failed POST is recorded locally so the user can
      // see mind-side logging outages, but we don't retry the batch.
      if (_writer === null) return;
      // Must not throw: IframeLogBuffer.flush catches errors from this
      // callback and re-invokes onError with the new error, so a writer
      // fault here would produce unbounded recursion between flush's catch
      // and this callback. Swallow writer failures silently; if the local
      // writer itself is broken we have nowhere useful to report it.
      try {
        _writer.write({
          level: 'warning',
          source: 'electron/main',
          message: `iframe-logs POST failed for mind ${mindId}: ${err.message}`,
        });
      } catch {
        // Intentionally empty: see comment above.
      }
    },
  });
  _router = new LogRouter({ writer: _writer, buffer: _buffer });
  _flushTimer = setInterval(() => {
    if (_buffer !== null) _buffer.flushAll().catch(() => {});
  }, FLUSH_INTERVAL_MS);
  app.on('will-quit', handleWillQuit);
}

function setBackendPort(port) {
  if (_router === null) return;
  _router.setBackendPort(port);
}

async function close() {
  if (_flushTimer !== null) {
    clearInterval(_flushTimer);
    _flushTimer = null;
  }
  if (_router !== null) {
    _router.close();
    _router = null;
  }
  if (_buffer !== null) {
    _buffer.close();
    await _buffer.flushAll();
    _buffer = null;
  }
  if (_writer !== null) {
    _writer.close();
    _writer = null;
  }
}

function handleWillQuit() {
  // Best-effort synchronous close: fire flushAll without awaiting because
  // Electron's will-quit does not wait for async handlers.
  close().catch(() => {});
}

/**
 * Record a main-process event (replaces ad-hoc `console.log` calls).
 * @param {string} level
 * @param {string} message
 */
function logMain(level, message) {
  if (_router === null) return;
  _router.logMain(level, message);
}

/**
 * Attach a `console-message` listener to a WebContentsView's webContents,
 * routing each message through the classifier.
 *
 * @param {Electron.WebContents} webContents
 * @param {string} viewName - 'chrome' | 'sidebar' | 'requests-panel' | 'content'
 */
function attachConsoleListener(webContents, viewName) {
  webContents.on('console-message', (details) => {
    if (_router === null) return;
    _router.handleConsoleMessage(details, viewName);
  });
}

module.exports = {
  init,
  close,
  setBackendPort,
  logMain,
  attachConsoleListener,
};
