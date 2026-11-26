// Polls `read` until `isDone` accepts what it returned, for the Electron
// harness specs, whose state lives in a separate process.

async function waitFor(read, isDone, what, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await read();
    if (isDone(last)) return last;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${what} not seen within ${timeoutMs}ms; last: ${JSON.stringify(last)}`);
}

module.exports = { waitFor };
