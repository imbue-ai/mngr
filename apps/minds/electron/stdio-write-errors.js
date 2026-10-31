// Keep a failed write to the Electron main process's own stdout/stderr from
// crashing it.
//
// Writes to these streams -- console.* and backend.js's dev-mode forward of the
// backend's stderr -- report failure asynchronously, as an 'error' event on the
// stream. An error thrown from that event is an uncaught exception, which takes
// down the main process. Two cases are known:
//
// - EPIPE. When a dev launch goes through a pipe (e.g.
//   `just minds-start | head -30`), the reader can exit while the backend is
//   still alive. This is expected, and it is dropped silently because the
//   downstream consumer is gone.
// - ENOSPC. When stderr is redirected to a file on a full disk, Node writes the
//   stream synchronously but still reports the failure here, not to the code
//   that called write(). Errors like this one are dropped too, since nothing
//   downstream can act on them.
//
// The log files are the durable record either way. The first failure other than
// EPIPE on each stream is noted once through `warn`, which defaults to
// console.warn so it reaches electron.log. Once a write has failed, Node leaves
// the stream errored, so whatever is written to it afterwards is lost for the
// rest of the run even if the disk frees up. Pure (no Electron API) so it is
// unit-testable under node.

/**
 * Attach the 'error' listener to each named stream, e.g.
 * ``{ stdout: process.stdout, stderr: process.stderr }``.
 */
function dropStdioWriteErrors(streamsByName, warn = (message) => console.warn(message)) {
  for (const [name, stream] of Object.entries(streamsByName)) {
    let isWriteFailureNoted = false;
    stream.on('error', (err) => {
      if ((err && err.code === 'EPIPE') || isWriteFailureNoted) return;
      isWriteFailureNoted = true;
      warn(`[stdio] write to the main process's ${name} failed; dropping further write errors on it: ${(err && err.message) || err}`);
    });
  }
}

module.exports = { dropStdioWriteErrors };
