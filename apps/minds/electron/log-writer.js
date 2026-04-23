// Size-rotating JSONL writer for the local Electron log file.
//
// Mirrors the rotation convention used by
// libs/imbue_common/imbue/imbue_common/logging.make_jsonl_file_sink so the
// on-disk layout is visually consistent with the Python-side events.jsonl
// files: when the active file exceeds maxSizeBytes it is renamed to
// `<name>.<YYYYMMDDHHMMSSffffff>` (UTC, zero-padded), matching the suffix
// shape produced by `generate_rotation_timestamp`. Because the Python-side
// `ROTATED_JSONL_PATTERN` (`^events\.jsonl\.\d+$`) is anchored to the
// literal basename, `cleanup_old_rotated_files` cannot be pointed at this
// writer's directory to prune its siblings -- pruning is done locally by
// `_pruneOldRotations` against the Electron basename instead. After each
// rotation the oldest siblings beyond `maxRotatedCount` are deleted, so the
// retention behaviour matches the Python sink even though the identifying
// regex is not shared.
//
// JS has only millisecond precision for `Date.now()`, so the microsecond
// portion of the suffix is `ms * 1000`. That keeps the suffix 20 digits long
// and lexicographically sortable in chronological order, same as the Python
// side. If two rotations happen to land on the same millisecond, a short
// numeric tiebreaker is appended (`.<timestamp>.<N>`) so the rename always
// resolves to a fresh path.
//
// The writer is intentionally minimal -- no async I/O, no buffering beyond
// Node's default stream buffering -- because the volume of records is low
// (main-process lifecycle events plus renderer console output) and we care
// about durability after a crash more than throughput.

'use strict';

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_MAX_BYTES = 10 * 1024 * 1024;
const DEFAULT_MAX_ROTATED_COUNT = 10;

// Matches rotated siblings created by this writer OR by the Python sink; both
// produce `<name>.<digits>` suffixes.
const ROTATED_SUFFIX_PATTERN = /^\.(\d+)(?:\.(\d+))?$/;

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

/**
 * Build a rotation timestamp suffix matching
 * imbue_common.logging.generate_rotation_timestamp: 14-digit UTC calendar
 * component followed by 6 digits of sub-second precision (microseconds in the
 * Python producer, milliseconds*1000 here because JS lacks finer resolution).
 *
 * @param {Date} date
 * @returns {string}
 */
function buildRotationTimestamp(date) {
  const pad = (value, width) => String(value).padStart(width, '0');
  const year = pad(date.getUTCFullYear(), 4);
  const month = pad(date.getUTCMonth() + 1, 2);
  const day = pad(date.getUTCDate(), 2);
  const hour = pad(date.getUTCHours(), 2);
  const minute = pad(date.getUTCMinutes(), 2);
  const second = pad(date.getUTCSeconds(), 2);
  const subSecond = pad(date.getUTCMilliseconds() * 1000, 6);
  return `${year}${month}${day}${hour}${minute}${second}${subSecond}`;
}

class LogWriter {
  /**
   * @param {string} filePath
   * @param {{maxSizeBytes?: number, maxRotatedCount?: number, now?: () => Date}} [options]
   */
  constructor(filePath, options = {}) {
    this._filePath = filePath;
    this._maxSizeBytes = options.maxSizeBytes ?? DEFAULT_MAX_BYTES;
    this._maxRotatedCount = options.maxRotatedCount ?? DEFAULT_MAX_ROTATED_COUNT;
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
    // Open first so `_size` reflects the current on-disk size before the
    // rotation check. Otherwise the first write after startup would observe
    // the constructor's `_size = 0` and skip rotation even when the existing
    // file is already over the cap.
    this._ensureOpen();
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
    const timestamp = buildRotationTimestamp(this._now());
    // Base target name: `<base>.<timestamp>`. If that exists (two rotations
    // land in the same JS millisecond), fall back to a numeric tiebreaker.
    let target = path.join(dir, `${base}.${timestamp}`);
    if (fs.existsSync(target)) {
      let tie = 1;
      while (fs.existsSync(path.join(dir, `${base}.${timestamp}.${tie}`))) {
        tie += 1;
      }
      target = path.join(dir, `${base}.${timestamp}.${tie}`);
    }
    fs.renameSync(this._filePath, target);
    this._size = 0;
    this._pruneOldRotations(dir, base);
  }

  _pruneOldRotations(dir, base) {
    if (this._maxRotatedCount <= 0) return;
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch {
      return;
    }
    const rotated = [];
    for (const name of entries) {
      if (!name.startsWith(`${base}.`)) continue;
      const suffix = name.slice(base.length);
      if (ROTATED_SUFFIX_PATTERN.test(suffix)) {
        rotated.push(name);
      }
    }
    if (rotated.length <= this._maxRotatedCount) return;
    rotated.sort();
    const excess = rotated.length - this._maxRotatedCount;
    for (let index = 0; index < excess; index += 1) {
      try {
        fs.unlinkSync(path.join(dir, rotated[index]));
      } catch {
        // Best-effort cleanup: another process may have pruned the same
        // file concurrently, or the file may have been removed manually.
      }
    }
  }
}

module.exports = { LogWriter, DEFAULT_MAX_BYTES, DEFAULT_MAX_ROTATED_COUNT };
