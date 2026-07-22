// Size-based log rotation for the Electron-written log files (electron.log and
// minds.log). Mirrors the Python jsonl sink's scheme (imbue_common/logging.py):
// 10MB threshold, keep the 10 newest rotated files, timestamp-suffixed names.
// Unlike the Python sink, rotated files are gzipped after rotating (so error
// reports can upload the current file plus the most recent rotation gzip), and
// no cross-process lock is needed -- only the Electron main process writes these
// files.
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { pipeline } = require('stream');

// 10MB, matching the Python backend jsonl sink so all of minds' logs behave
// consistently.
const DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024;
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
 * main process never blocks compressing a large file. Best-effort: a failure
 * leaves the raw rotation on disk (still uploadable) rather than crashing.
 */
function compressRotatedFile(rawPath, dir, baseName, maxRotatedCount) {
  const gzPath = rawPath + ROTATED_SUFFIX;
  // pipeline (unlike a bare .pipe() chain) destroys every stream -- freeing the
  // read + write file descriptors -- on both success and error, so a gzip failure
  // can't leak fds over the long-lived main process's many rotations.
  pipeline(fs.createReadStream(rawPath), zlib.createGzip(), fs.createWriteStream(gzPath), (err) => {
    if (err) {
      console.warn(`[log-rotation] failed to gzip ${rawPath}: ${err && err.message}`);
      return;
    }
    fs.unlink(rawPath, () => {});
    pruneRotated(dir, baseName, maxRotatedCount);
  });
}

/**
 * Open an append-mode, size-rotating log sink at ``filePath``.
 *
 * Returns ``{ write(chunk), end() }``. Backed by an async ``fs.WriteStream``
 * (matching the non-blocking log stream this replaced -- writes never block the
 * Electron main thread). When the tracked size reaches ``maxSizeBytes`` the
 * stream is ended (which flushes all buffered writes and closes the fd) and only
 * THEN is the file renamed to ``<name>.<timestamp>`` -- so the background gzip
 * can never read a partially-flushed file. A fresh stream is opened, the rotated
 * file is gzipped and pruned to ``maxRotatedCount`` newest, and any lines that
 * arrived mid-rotation are replayed. ``end()`` returns a promise that resolves
 * once the current stream has flushed.
 */
function createRotatingLogStream(options) {
  const filePath = options.filePath;
  const maxSizeBytes = options.maxSizeBytes || DEFAULT_MAX_SIZE_BYTES;
  const maxRotatedCount = options.maxRotatedCount || DEFAULT_MAX_ROTATED_COUNT;
  const dir = path.dirname(filePath);
  const baseName = path.basename(filePath);

  fs.mkdirSync(dir, { recursive: true });
  pruneRotated(dir, baseName, maxRotatedCount);

  // Never throw from an async stream 'error' (that would crash the main process),
  // and never route it through console.* (which is teed back into a log stream) --
  // write it straight to stderr.
  const attachErrorHandler = (writeStream) => {
    writeStream.on('error', (err) => {
      try {
        process.stderr.write(`[log-rotation] write stream error for ${filePath}: ${err && err.message}\n`);
      } catch {
        // Nothing more we can do.
      }
    });
  };

  const openStream = () => {
    const writeStream = fs.createWriteStream(filePath, { flags: 'a' });
    attachErrorHandler(writeStream);
    return writeStream;
  };

  let stream = openStream();
  let size = 0;
  try {
    size = fs.statSync(filePath).size;
  } catch {
    size = 0;
  }
  let isRotating = false;
  // Lines that arrive after the old stream starts closing but before the fresh
  // one opens are buffered here and replayed once rotation completes.
  const pendingDuringRotation = [];

  const rotate = () => {
    isRotating = true;
    const rotating = stream;
    // end()'s callback fires after all buffered writes have flushed and the fd is
    // closed; renaming only then guarantees the gzip reads a complete file.
    rotating.end(() => {
      try {
        const rawPath = filePath + '.' + rotationTimestamp();
        fs.renameSync(filePath, rawPath);
        compressRotatedFile(rawPath, dir, baseName, maxRotatedCount);
      } catch (err) {
        console.warn(`[log-rotation] rotation failed for ${filePath}: ${err && err.message}`);
      }
      stream = openStream();
      size = 0;
      isRotating = false;
      const buffered = pendingDuringRotation.splice(0);
      for (const chunk of buffered) writeChunk(chunk);
    });
  };

  function writeChunk(text) {
    if (isRotating) {
      pendingDuringRotation.push(text);
      return;
    }
    if (size >= maxSizeBytes) {
      pendingDuringRotation.push(text);
      rotate();
      return;
    }
    stream.write(text);
    size += Buffer.byteLength(text);
  }

  return {
    write(chunk) {
      writeChunk(typeof chunk === 'string' ? chunk : String(chunk));
    },
    end() {
      return new Promise((resolve) => {
        try {
          stream.end(resolve);
        } catch {
          resolve();
        }
      });
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
