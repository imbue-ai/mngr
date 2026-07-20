// Pure predicate deciding whether a content-view ``did-fail-load`` warrants
// showing the local content-load error page. Split out of main.js (which can't
// be required outside Electron) so the filtering -- the bug-prone part -- is
// unit-testable, mirroring the split-out ``decideStartupRoute`` helper.
//
// Electron fires ``did-fail-load`` for many benign reasons besides a genuine
// "couldn't reach the workspace" failure, so we show the error page only for a
// real, top-level navigation failure. Note that HTTP errors (4xx/5xx) do NOT
// reach ``did-fail-load`` at all: the mngr_forward "Loading workspace" proxy
// answers 503 as a *successful* load, handled by ``did-navigate`` instead. So a
// ``did-fail-load`` here is always a network-layer failure (connection refused,
// DNS, TLS, a dead/stale forward port, timeout, ...).

// Chromium's ERR_ABORTED. Emitted on every superseded load -- switching
// workspaces, ``will-navigate`` preventDefault -> focusing another window, or a
// fresh ``loadURL`` replacing an in-flight one. Never a real error.
const ERR_ABORTED = -3;

function shouldShowContentLoadError({
  errorCode,
  isMainFrame,
  isLocalShellPage,
  isShuttingDown,
  isErrorState,
} = {}) {
  // A workspace embeds third-party iframes; a subframe failing must never blank
  // the whole view.
  if (!isMainFrame) return false;
  if (errorCode === ERR_ABORTED) return false;
  // The shell already owns the screen in these states, or the failure is on one
  // of our own local file:// pages (the crash page / this error page) -- showing
  // the error page then would fight the takeover or recurse into itself.
  if (isShuttingDown) return false;
  if (isErrorState) return false;
  if (isLocalShellPage) return false;
  return true;
}

module.exports = { shouldShowContentLoadError, ERR_ABORTED };
