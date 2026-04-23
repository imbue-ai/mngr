// Size-rotating JSONL writer for the local Electron log file.
//
// Mirrors the rotation convention used by
// libs/imbue_common/imbue/imbue_common/logging.make_jsonl_file_sink so the
// on-disk layout is consistent with the Python-side events.jsonl files:
// when the active file exceeds maxSizeBytes, it is renamed to `<name>.1`
// (or the next free numeric suffix) and a fresh file is opened.
//
// The writer is intentionally minimal -- no async I/O, no buffering beyond
// Node's default stream buffering -- because the volume of records is low
// (main-process lifecycle events plus renderer console output) and we care
// about durability after a crash more than throughput.

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_MAX_BYTES = 10 * 1024 * 1024;

/**
 * @typedef {Object} LogRecord
 * @property {string} level
 * @property {string} message
 * @property {string} source
 * @property {string} [frame_url]
 * @property {string} [source_id]
 * @property {number} [line]
 * @property {string} [mind_id]
 * @property {string} [service_name]
 */

class LogWriter {
  /**
   * @param {string} filePath
   * @param {{maxSizeBytes?: number, now?: () => Date}} [options]
   */
  constructor(filePath, options = {}) {
    this._filePath = filePath;
    this._maxSizeBytes = options.maxSizeBytes ?? DEFAULT_MAX_BYTES;
    // Injectable for deterministic tests.
    this._now = options.now ?? (() => new Date());
    /** @type {number|null} */
    this._fd = null;
    this._size = 0;
  }

  /**
   * Append a record. The writer wraps it in a JSON envelope and adds a
   * timestamp and process id.
   *
   * @param {LogRecord} record
   */
  write(record) {
    const envelope = {
      timestamp: this._now().toISOString(),
      type: 'electron',
      pid: process.pid,
      ...record,
    };
    const line = JSON.stringify(envelope) + '\n';
    const lineBytes = Buffer.byteLength(line, 'utf8');
    this._rotateIfNeeded();
    const fd = this._ensureOpen();
    fs.writeSync(fd, line);
    this._size += lineBytes;
  }

  close() {
    if (this._fd !== null) {
      try {
        fs.closeSync(this._fd);
      } catch {
        // Already closed; ignore.
      }
      this._fd = null;
    }
  }

  _ensureOpen() {
    if (this._fd === null) {
      fs.mkdirSync(path.dirname(this._filePath), { recursive: true });
      this._fd = fs.openSync(this._filePath, 'a');
      try {
        this._size = fs.statSync(this._filePath).size;
      } catch {
        this._size = 0;
      }
    }
    return this._fd;
  }

  _rotateIfNeeded() {
    if (this._size < this._maxSizeBytes) return;
    if (this._fd !== null) {
      try {
        fs.closeSync(this._fd);
      } catch {
        // If close fails we still attempt the rename below; the error
        // surfaces there if the fd was genuinely unusable.
      }
      this._fd = null;
    }
    const dir = path.dirname(this._filePath);
    const base = path.basename(this._filePath);
    let index = 1;
    while (fs.existsSync(path.join(dir, `${base}.${index}`))) {
      index += 1;
    }
    fs.renameSync(this._filePath, path.join(dir, `${base}.${index}`));
    this._size = 0;
  }
}

module.exports = { LogWriter, DEFAULT_MAX_BYTES };
