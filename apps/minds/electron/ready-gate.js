// A one-shot readiness gate. Callers ``await waitUntilReady(timeoutMs)`` and are
// released either when ``markReady`` fires or when the bounded timeout elapses,
// so a ready-signal that never arrives degrades to proceeding rather than
// hanging forever. Extracted from main.js so the timeout/idempotency semantics
// can be unit tested (see test/unit/ready-gate.test.js).
//
// Used to gate startup workspace-restore navigation on the mngr_forward preauth
// session cookie being written: without it, a restored ``agent-<id>.localhost``
// view can reach the proxy before its bare-origin session cookie exists and get
// 302'd to the plugin's terminal-oriented "Sign in" page (a dead end for the
// app, which always pre-sets the cookie).
function createReadyGate() {
  let resolveReady;
  const ready = new Promise((resolve) => {
    resolveReady = resolve;
  });
  let isReady = false;
  return {
    // Release every current and future waiter. Idempotent: safe to call more
    // than once (the backend re-emits its ready event on restart).
    markReady() {
      isReady = true;
      resolveReady();
    },
    // Resolve as soon as ``markReady`` has fired, or after ``timeoutMs`` if it
    // has not. Never rejects.
    waitUntilReady(timeoutMs) {
      if (isReady) return Promise.resolve();
      let timer;
      const timeout = new Promise((resolve) => {
        timer = setTimeout(resolve, timeoutMs);
      });
      return Promise.race([ready, timeout]).then(() => {
        clearTimeout(timer);
      });
    },
  };
}

module.exports = { createReadyGate };
