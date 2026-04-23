// Pure routing logic for Electron console capture.
//
// Decides, per `console-message` event, whether the record belongs on the
// local-only writer (`~/.minds/logs/electron.jsonl`) or should be forwarded
// to a mind's workspace server via the iframe-log buffer, and holds the
// pre-port pending queue that protects the privacy boundary while the
// desktop-client backend port is still being discovered.
//
// This module deliberately has no Electron imports so it can be exercised
// directly under `node --test`. The Electron-aware singleton in
// `logger.js` injects the writer/buffer/fetchFn and owns the process
// lifecycle (timers, will-quit, etc.).

'use strict';

const { classifyFrame } = require('./classify-frame');

const DEFAULT_MAX_PENDING_RECORDS = 1000;

/**
 * @typedef {Object} RouterWriter
 * @property {(record: object) => void} write
 */

/**
 * @typedef {Object} RouterBuffer
 * @property {(mindId: string, port: number, record: object) => Promise<void>|null} enqueue
 * @property {() => void} close
 * @property {() => Promise<void>} flushAll
 */

/**
 * @typedef {Object} RouterOptions
 * @property {RouterWriter} writer
 * @property {RouterBuffer} buffer
 * @property {number} [maxPendingRecords]
 * @property {() => Date} [now]
 */

class LogRouter {
  /**
   * @param {RouterOptions} options
   */
  constructor(options) {
    if (!options || !options.writer || !options.buffer) {
      throw new Error('LogRouter requires { writer, buffer }');
    }
    this._writer = options.writer;
    this._buffer = options.buffer;
    this._maxPendingRecords = options.maxPendingRecords ?? DEFAULT_MAX_PENDING_RECORDS;
    this._now = options.now ?? (() => new Date());
    /** @type {number|null} */
    this._backendPort = null;
    /** @type {Array<{mindId: string, record: object}>} */
    this._pendingMindRecords = [];
    this._closed = false;
  }

  setBackendPort(port) {
    this._backendPort = port;
    if (this._closed || this._pendingMindRecords.length === 0) return;
    // Drain pending records through the buffer so back-pressure and
    // flushing behave identically to the steady-state path. Each enqueue
    // is guarded for the same reason as in handleConsoleMessage: a
    // synchronous throw from the buffer must not propagate out of the
    // logger (this would surface as an unhandled rejection during Electron
    // startup, where setBackendPort is invoked).
    const pending = this._pendingMindRecords;
    this._pendingMindRecords = [];
    for (const entry of pending) {
      try {
        this._buffer.enqueue(entry.mindId, port, entry.record);
      } catch {
        // Intentionally empty: logging must never surface errors to callers.
      }
    }
  }

  /**
   * Record a main-process event (replaces ad-hoc `console.log` calls).
   *
   * @param {string} level
   * @param {string} message
   */
  logMain(level, message) {
    if (this._closed) return;
    // Swallow writer failures: this method is invoked from arbitrary
    // main-process code paths and a thrown fs error would crash Electron.
    // Console capture is diagnostic, never load-bearing.
    try {
      this._writer.write({ level, source: 'electron/main', message });
    } catch {
      // Intentionally empty: logging must never surface errors to callers.
    }
  }

  /**
   * Route a `console-message` payload through the classifier.
   *
   * @param {object} details - the raw Electron event payload
   * @param {string} viewName - 'chrome' | 'sidebar' | 'requests-panel' | 'content'
   */
  handleConsoleMessage(details, viewName) {
    if (this._closed) return;
    const frameUrl =
      details && details.frame && typeof details.frame.url === 'string' ? details.frame.url : '';
    const classification = classifyFrame(frameUrl, viewName);
    const level = typeof details.level === 'string' ? details.level : 'info';
    const message = typeof details.message === 'string' ? details.message : '';
    const sourceId = typeof details.sourceId === 'string' ? details.sourceId : '';
    const line = typeof details.lineNumber === 'number' ? details.lineNumber : 0;

    if (classification.destination === 'mind') {
      const mindRecord = {
        level,
        message,
        frame_url: frameUrl,
        source_id: sourceId,
        line,
        service_name: classification.serviceName,
        mind_id: classification.mindId,
        client_timestamp: this._now().toISOString(),
      };
      if (this._backendPort === null) {
        // Port not yet known: queue the record rather than writing it to
        // the local file, because routing a mind-owned service log to the
        // user's laptop would cross the privacy boundary the classifier
        // just established.
        this._pendingMindRecords.push({ mindId: classification.mindId, record: mindRecord });
        while (this._pendingMindRecords.length > this._maxPendingRecords) {
          this._pendingMindRecords.shift();
        }
        return;
      }
      // `enqueue` is a sync state mutation that may trigger an async flush;
      // wrap defensively so a synchronous throw from the buffer (e.g. bad
      // internal state) cannot propagate out of the Electron event handler.
      try {
        this._buffer.enqueue(classification.mindId, this._backendPort, mindRecord);
      } catch {
        // Intentionally empty: logging must never surface errors to callers.
      }
      return;
    }

    // Swallow writer failures here for the same reason as `logMain`: this is
    // called from the `console-message` event listener and a thrown fs
    // error would take down the Electron main process.
    try {
      this._writer.write({
        level,
        source: classification.source,
        message,
        frame_url: frameUrl,
        source_id: sourceId,
        line,
        mind_id: classification.mindId,
        service_name: classification.serviceName,
      });
    } catch {
      // Intentionally empty: logging must never surface errors to callers.
    }
  }

  /**
   * Stop routing new records. The caller is responsible for closing the
   * underlying writer/buffer; this just flips the closed flag and drops
   * any still-pending mind records (they are intentionally not routed to
   * the writer, to preserve the privacy boundary).
   */
  close() {
    this._closed = true;
    this._pendingMindRecords = [];
  }
}

module.exports = { LogRouter, DEFAULT_MAX_PENDING_RECORDS };
