// Durable logging for the Electron MAIN process.
//
// The main process previously logged only to stdout/stderr via bare console.*
// calls, which vanish in packaged builds -- so a main-process problem (e.g. a
// content-view renderer dying over sleep) left nothing on disk to diagnose.
// This module tees every console.log/warn/error into ~/.minds/logs/electron.log
// (rotated + gzipped like the other logs) and records uncaught exceptions /
// unhandled rejections, so those failures are durably diagnosable and uploadable
// with bug reports. Initialize this as the very first thing in main.js (before
// initSentry) so startup output is captured.
//
// Why not the `electron-log` library: it is the usual pick for durable Electron
// logging, but it does not gzip its rotations and names them differently, and we
// specifically want on-disk gzipped rotations under fixed, exact names
// (`electron.log` / `electron.log.<ts>.gz`, mirroring the backend's `minds.log`
// scheme) so the Sentry `LogAttachmentGroup` globs in utils/sentry/core.py can
// upload the current file plus the newest rotation with no extra transform. The
// shared electron/log-rotation.js gives us exactly that and mirrors the Python
// jsonl sink's 100MB/keep-10 behavior, so a small, purpose-fit helper is
// preferable to adapting a heavier general-purpose dependency here.
const path = require('path');
const util = require('util');
const paths = require('./paths');
const { createRotatingLogStream } = require('./log-rotation');

let logStream = null;
// Reentrancy guard against a log write synchronously re-entering writeLine.
// console.* is wrapped below to call back into writeLine, so if logStream.write
// ever emitted a console.* call on the same tick, writeLine -> logStream.write ->
// console.* -> writeLine would recurse until the stack overflows and the main
// process crashes. The guard makes the nested call a no-op; the wrapped console.*
// still reaches stdout/stderr via the wrapper's original(...), so nothing is lost.
// (log-rotation.js defers its own rename-failure console.warn into an async stream
// callback, where it is buffered rather than recursed, so this guard is defensive.)
let isWritingLine = false;

/** ``LEVEL`` + ISO-8601 UTC timestamp prefix, matching console formatting semantics via util.format. */
function formatLine(level, args) {
  return `${new Date().toISOString()} [${level}] ${util.format(...args)}\n`;
}

function writeLine(level, args) {
  if (!logStream || isWritingLine) return;
  isWritingLine = true;
  try {
    logStream.write(formatLine(level, args));
  } catch {
    // A broken log sink must never disturb the caller.
  } finally {
    isWritingLine = false;
  }
}

/**
 * Tee console output into electron.log and record fatal main-process failures.
 *
 * Idempotent: a second call is a no-op. Wrapping console preserves the original
 * stdout/stderr behavior (dev terminals are unchanged) and adds the file tee.
 * Uncaught exceptions are observed via ``uncaughtExceptionMonitor`` -- a Node
 * primitive that logs WITHOUT counting as a handler, so Sentry's capture and the
 * process's exit behavior are left exactly as they were. Unhandled rejections
 * are logged alongside Sentry's own handling (Sentry already registers a
 * listener, so adding a logging one changes neither the exit decision nor the
 * capture).
 */
function initElectronLogging() {
  if (logStream) return;
  logStream = createRotatingLogStream({ filePath: path.join(paths.getLogDir(), 'electron.log') });

  for (const level of ['log', 'warn', 'error']) {
    const original = console[level].bind(console);
    console[level] = (...args) => {
      writeLine(level.toUpperCase(), args);
      original(...args);
    };
  }

  process.on('uncaughtExceptionMonitor', (err, origin) => {
    writeLine('UNCAUGHT', [`(${origin})`, err && err.stack ? err.stack : err]);
  });
  process.on('unhandledRejection', (reason) => {
    writeLine('UNHANDLED_REJECTION', [reason && reason.stack ? reason.stack : reason]);
  });
}

module.exports = { initElectronLogging };
