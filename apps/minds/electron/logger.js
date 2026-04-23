// Single-instance logger for the Electron main process.
//
// Owns:
//   - a `LogWriter` writing to ~/.minds/logs/electron.jsonl (local-only records)
//   - an `IframeLogBuffer` that POSTs MIND-destined records back to each
//     mind's workspace server via the desktop-client subdomain proxy
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
const { classifyFrame } = require('./classify-frame');

const FLUSH_INTERVAL_MS = 1000;

const MAX_PENDING_RECORDS = 1000;

let _writer = null;
let _buffer = null;
/** @type {NodeJS.Timeout|null} */
let _flushTimer = null;
/** @type {number|null} */
let _backendPort = null;
/**
 * Records classified as MIND-destined but received before the desktop-client
 * backend port is known. These cannot be POSTed yet, and dropping them into
 * the local file would cross the local/mind privacy boundary -- so we queue
 * them here and drain into `_buffer` when `setBackendPort` fires.
 *
 * @type {Array<{mindId: string, record: object}>}
 */
let _pendingMindRecords = [];

function init() {
  if (_writer !== null) return;
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
      _writer.write({
        level: 'warning',
        source: 'electron/main',
        message: `iframe-logs POST failed for mind ${mindId}: ${err.message}`,
      });
    },
  });
  _flushTimer = setInterval(() => {
    if (_buffer !== null) _buffer.flushAll().catch(() => {});
  }, FLUSH_INTERVAL_MS);
  app.on('will-quit', handleWillQuit);
}

function setBackendPort(port) {
  _backendPort = port;
  // Drain any records queued before the port was known. We enqueue through
  // the normal buffer so back-pressure and flushing behave identically to
  // the steady-state path.
  if (_buffer === null || _pendingMindRecords.length === 0) return;
  const pending = _pendingMindRecords;
  _pendingMindRecords = [];
  for (const entry of pending) {
    _buffer.enqueue(entry.mindId, port, entry.record);
  }
}

async function close() {
  if (_flushTimer !== null) {
    clearInterval(_flushTimer);
    _flushTimer = null;
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
  _pendingMindRecords = [];
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
  if (_writer === null) return;
  _writer.write({ level, source: 'electron/main', message });
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
    handleConsoleMessage(details, viewName);
  });
}

function handleConsoleMessage(details, viewName) {
  const frameUrl = details && details.frame && typeof details.frame.url === 'string' ? details.frame.url : '';
  const classification = classifyFrame(frameUrl, viewName);
  const level = typeof details.level === 'string' ? details.level : 'info';
  const message = typeof details.message === 'string' ? details.message : '';
  const sourceId = typeof details.sourceId === 'string' ? details.sourceId : '';
  const line = typeof details.lineNumber === 'number' ? details.lineNumber : 0;

  if (classification.destination === 'mind' && _buffer !== null) {
    const mindRecord = {
      level,
      message,
      frame_url: frameUrl,
      source_id: sourceId,
      line,
      service_name: classification.serviceName,
      mind_id: classification.mindId,
      client_timestamp: new Date().toISOString(),
    };
    if (_backendPort === null) {
      // Port not yet known. Queue the record rather than writing it to the
      // local file; routing a mind-owned service log to the user's laptop
      // would cross the privacy boundary the classifier just established.
      _pendingMindRecords.push({ mindId: classification.mindId, record: mindRecord });
      while (_pendingMindRecords.length > MAX_PENDING_RECORDS) {
        _pendingMindRecords.shift();
      }
      return;
    }
    _buffer.enqueue(classification.mindId, _backendPort, mindRecord);
    return;
  }

  fallbackLocal(level, message, frameUrl, sourceId, line, classification);
}

function fallbackLocal(level, message, frameUrl, sourceId, line, classification) {
  if (_writer === null) return;
  _writer.write({
    level,
    source: classification.source,
    message,
    frame_url: frameUrl,
    source_id: sourceId,
    line,
    mind_id: classification.mindId,
    service_name: classification.serviceName,
  });
}

module.exports = {
  init,
  close,
  setBackendPort,
  logMain,
  attachConsoleListener,
};
