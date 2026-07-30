// Line timestamping for the logs the Electron main process writes: the captured
// backend log (minds.log) and, via logger.js, electron.log -- so both carry the
// same stamp shape and a reader can interleave them.
//
// The Python backend's console format carries no time of its own -- it is tuned
// for an interactive terminal -- so its captured stdout/stderr lands in
// minds.log undated. Every other log a bug report uploads (minds-events.jsonl,
// electron.log, the latchkey/discovery event streams) is timestamped, so an
// undated minds.log cannot be lined up against them when reconstructing an
// incident. These helpers stamp each captured line as it is written.
//
// Stamping happens at capture time (within milliseconds of the backend emitting
// the line) rather than at the source, so it covers everything the child writes
// -- loguru console output, bare prints, and tracebacks alike -- without
// changing the format the backend prints to an interactive console.

/**
 * Format one log line: ISO-8601 UTC stamp + payload.
 *
 * Appends exactly one newline so the file stays one record per line. The
 * payload is passed through byte-for-byte (ANSI colour codes and non-ASCII
 * included) so the capture still matches what the backend emitted. ``now`` is
 * injectable for tests.
 */
function formatTimestampedLine(line, now) {
  const at = now || new Date();
  return `${at.toISOString()} ${line}\n`;
}

/**
 * Accumulate output chunks and hand back whole lines.
 *
 * A child's stdout/stderr arrives in arbitrary chunks that routinely split a
 * line in half (and a multi-line traceback across several reads), so a chunk
 * cannot be stamped as-is: doing so would date a fragment and corrupt the
 * one-record-per-line shape. ``push`` returns only the lines completed by this
 * chunk, holding any trailing partial line back until its newline arrives;
 * ``flush`` surfaces that remainder at exit so a backend dying mid-line still
 * gets its last output -- the output most worth having -- into the log.
 */
function createLineSplitter() {
  let buffer = '';
  return {
    push(text) {
      buffer += text;
      const lines = buffer.split('\n');
      // The final element is whatever followed the last newline: either an
      // incomplete line or '' when the chunk ended cleanly. Either way it is
      // not yet a complete line, so it stays buffered.
      buffer = lines.pop() || '';
      return lines;
    },
    flush() {
      if (!buffer) return null;
      const remainder = buffer;
      buffer = '';
      return remainder;
    },
  };
}

module.exports = {
  formatTimestampedLine,
  createLineSplitter,
};
