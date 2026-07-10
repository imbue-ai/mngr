// Size-based log rotation for the Electron-written log files (electron.log and
// minds.log). Mirrors the Python jsonl sink's scheme (imbue_common/logging.py):
// 100MB threshold, keep the 10 newest rotated files, timestamp-suffixed names.
// Unlike the Python sink, rotated files are gzipped after rotating (so error
// reports can upload the current file plus the most recent rotation gzip), and
// no cross-process lock is needed -- only the Electron main process writes these
// files.
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

// 100MB, matching the jsonl sink so all of minds' logs behave consistently.
const DEFAULT_MAX_SIZE_BYTES = 100 * 1024 * 1024;
const DEFAULT_MAX_ROTATED_COUNT = 10;

// The compressed suffix appended to a rotated file once gzipped.
const ROTATED_SUFFIX = '.gz';

/**
 * Timestamp suffix for a rotated file: ``YYYYMMDDHHMMSSmmm`` in UTC.
 *
 * Fixed-width so a lexicographic sort of file names is chronological. Mirrors
 * the Python sink's ``<YYYYMMDDHHMMSSffffff>`` shape; JS ``Date`` only resolves
 * to milliseconds, so the sub-second field is 3 digits rather than 6 -- still
 * fixed-width, so sorting is unaffected. ``now`` is injectable for tests.
 */
function rotationTimestamp(now) {
  const d = now || new Date();
  const pad = (value, width) => String(value).padStart(width, '0');
  return (
    pad(d.getUTCFullYear(), 4) +
    pad(d.getUTCMonth() + 1, 2) +
    pad(d.getUTCDate(), 2) +
    pad(d.getUTCHours(), 2) +
    pad(d.getUTCMinutes(), 2) +
    pad(d.getUTCSeconds(), 2) +
    pad(d.getUTCMilliseconds(), 3)
  );
}

/**
 * Remove the oldest gzipped rotations of ``baseName`` in ``dir``, keeping at
 * most ``maxRotatedCount``. Only ``<baseName>.<ts>.gz`` files are considered;
 * a transient un-gzipped rotation (between the rename and the gzip completing)
 * is left alone. Fixed-width timestamps make a name sort chronological.
 */
function pruneRotated(dir, baseName, maxRotatedCount) {
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return;
  }
  const prefix = baseName + '.';
  const rotated = names
    .filter((name) => name.startsWith(prefix) && name.endsWith(ROTATED_SUFFIX))
    .sort();
  const excess = rotated.length - maxRotatedCount;
  for (let i = 0; i < excess; i++) {
    try {
      fs.unlinkSync(path.join(dir, rotated[i]));
    } catch {
      // Raced/removed/permission-denied -- skip.
    }
  }
}

/**
 * Gzip a just-rotated raw file in the background, then remove the raw file and
 * prune old rotations. Done off the main path (streamed, not gzipSync) so the
 * main process never blocks compressing a ~100MB file. Best-effort: a failure
 * leaves the raw rotation on disk (still uploadable) rather than crashing.
 */
function compressRotatedFile(rawPath, dir, baseName, maxRotatedCount) {
  const gzPath = rawPath + ROTATED_SUFFIX;
  const source = fs.createReadStream(rawPath);
  const gzip = zlib.createGzip();
  const dest = fs.createWriteStream(gzPath);
  const onError = (err) => {
    console.warn(`[log-rotation] failed to gzip ${rawPath}: ${err && err.message}`);
  };
  source.on('error', onError);
  gzip.on('error', onError);
  dest.on('error', onError);
  dest.on('finish', () => {
    fs.unlink(rawPath, () => {});
    pruneRotated(dir, baseName, maxRotatedCount);
  });
  source.pipe(gzip).pipe(dest);
}

/**
 * Open an append-mode, size-rotating log sink at ``filePath``.
 *
 * Returns ``{ write(chunk), end() }``. Writes are synchronous (fd-based) so the
 * file is fully flushed before a rotation renames it -- the gzip of the rotated
 * file then never races a partial flush. When the tracked size reaches
 * ``maxSizeBytes`` the current file is renamed to ``<name>.<timestamp>``, a
 * fresh file is opened immediately (logging continues), and the rotated file is
 * gzipped in the background and pruned to ``maxRotatedCount`` newest.
 */
function createRotatingLogStream(options) {
  const filePath = options.filePath;
  const maxSizeBytes = options.maxSizeBytes || DEFAULT_MAX_SIZE_BYTES;
  const maxRotatedCount = options.maxRotatedCount || DEFAULT_MAX_ROTATED_COUNT;
  const dir = path.dirname(filePath);
  const baseName = path.basename(filePath);

  fs.mkdirSync(dir, { recursive: true });
  pruneRotated(dir, baseName, maxRotatedCount);

  let fd = fs.openSync(filePath, 'a');
  let size = 0;
  try {
    size = fs.statSync(filePath).size;
  } catch {
    size = 0;
  }

  function rotateIfNeeded() {
    if (size < maxSizeBytes) return;
    try {
      fs.closeSync(fd);
      const rawPath = filePath + '.' + rotationTimestamp();
      fs.renameSync(filePath, rawPath);
      fd = fs.openSync(filePath, 'a');
      size = 0;
      compressRotatedFile(rawPath, dir, baseName, maxRotatedCount);
    } catch (err) {
      console.warn(`[log-rotation] rotation failed for ${filePath}: ${err && err.message}`);
      // Ensure we still hold a usable fd so logging keeps working.
      try {
        fd = fs.openSync(filePath, 'a');
      } catch {
        // Nothing more we can do; subsequent writes will no-op on the closed fd.
      }
    }
  }

  return {
    write(chunk) {
      const text = typeof chunk === 'string' ? chunk : String(chunk);
      rotateIfNeeded();
      try {
        fs.writeSync(fd, text);
      } catch {
        // A broken log fd must never bring down the app.
      }
      size += Buffer.byteLength(text);
    },
    end() {
      try {
        fs.closeSync(fd);
      } catch {
        // Already closed -- fine.
      }
    },
  };
}

module.exports = {
  createRotatingLogStream,
  rotationTimestamp,
  pruneRotated,
  DEFAULT_MAX_SIZE_BYTES,
  DEFAULT_MAX_ROTATED_COUNT,
};
