// Rolling capture of the RENDERER console, for bug reports.
//
// logger.js tees the main process's own console into electron.log, but nothing
// on disk carried what the *pages* printed -- the minds SPA, and the workspace
// iframe and nested service frames that share its webContents. So a UI problem
// arrived in a report with no front-end console behind it. This module records
// every console message from every frame of every window into a log file under
// the log dir.
//
// It writes through the shared rotating stream (log-rotation.js) with that
// module's defaults, which is exactly what logger.js gives electron.log and what
// the Python backend sink uses: 10MB per file, 10 gzipped rotations. Every log
// in this folder is therefore bounded by one rule instead of this one carrying a
// cap of its own. Appending also costs only the bytes each message adds, where
// the buffer-and-rewrite scheme this replaced rewrote the whole file on every
// flush -- up to megabytes per second while a renderer was chatty.
//
// Neither this file's name nor the `.<timestamp>.gz` its rotations take matches
// any LogAttachmentGroup glob in utils/sentry/core.py (which sweeps this same
// folder for *.jsonl, minds.log, electron.log and their rotations). That is
// deliberate: this is app-lifetime history, and only the per-report copy staged
// from it is uploaded, so the console rides along only when the user asked for
// logs. The file is never deleted -- console history survives every report.
const path = require('path');
const { createRotatingLogStream } = require('./log-rotation');
const { formatTimestampedLine } = require('./log-timestamp');

// Matches no attachment-group glob, and is not the staged `bug-report-*-console.log`
// name that a report attaches.
const CONSOLE_TAIL_FILENAME = 'console-tail.log';

let stream = null;

/**
 * Report a capture failure without going through console.*.
 *
 * logger.js wraps console.log/warn/error to tee into electron.log, so reporting
 * a log-stream problem through console.* would feed one log stream's failure
 * into another. log-rotation.js avoids the same recursion the same way.
 */
function warnToStderr(message) {
  try {
    process.stderr.write(`[console-capture] ${message}\n`);
  } catch {
    // Nothing more we can do.
  }
}

/**
 * Point the capture at ``logDir``, opening the rotating stream.
 *
 * Idempotent: a second call is a no-op. The directory is a parameter (rather
 * than read from paths.js) so this module stays require-able outside Electron.
 * Opening in append mode is what continues the file across a restart, so no
 * seeding from disk is needed.
 *
 * ``bounds`` overrides the rotation size/count and exists so a test can prove
 * rotation without writing the 10MB the real bound needs. Production passes
 * nothing, which is the point: the caps then come from log-rotation.js's own
 * defaults, the same ones electron.log gets.
 */
function initConsoleCapture(logDir, bounds) {
  if (stream) return;
  const filePath = path.join(logDir, CONSOLE_TAIL_FILENAME);
  try {
    stream = createRotatingLogStream({ filePath, ...(bounds || {}) });
  } catch (err) {
    warnToStderr(`could not open ${filePath}: ${err && err.message}`);
  }
}

/**
 * Format one console message as a single timestamped record.
 *
 * Shape: ``<iso> [console:<LEVEL>] <message> (<source>:<line>)``, matching the
 * stamp electron.log carries so the two can be interleaved. Embedded newlines
 * are escaped so one stack trace stays one record, which keeps a line-oriented
 * reader (the bug report's excerpt) counting messages rather than fragments.
 */
function formatConsoleLine(details, now) {
  const level = String(details.level || 'info').toUpperCase();
  const source = details.sourceId ? `${details.sourceId}:${details.lineNumber}` : 'unknown';
  const message = String(details.message).replace(/\r?\n/g, '\\n');
  return formatTimestampedLine(`[console:${level}] ${message} (${source})`, now);
}

/**
 * Record one ``console-message`` event from a window's webContents.
 *
 * ``details`` is Electron's event object (message, level, sourceId,
 * lineNumber, frame); anything that does not carry a string message is
 * ignored rather than written as garbage.
 */
function recordConsoleMessage(details) {
  if (!stream || !details || typeof details.message !== 'string') return;
  stream.write(formatConsoleLine(details));
}

/**
 * End the stream, flushing everything buffered. Returns a promise.
 *
 * Capture is process-lifetime, so this exists for orderly shutdown and to give
 * a test a deterministic point at which the file is complete. Writes after it
 * are dropped rather than reopening the stream.
 */
function closeConsoleCapture() {
  if (!stream) return Promise.resolve();
  const ending = stream.end();
  stream = null;
  return ending;
}

module.exports = {
  initConsoleCapture,
  recordConsoleMessage,
  closeConsoleCapture,
  formatConsoleLine,
  CONSOLE_TAIL_FILENAME,
};
