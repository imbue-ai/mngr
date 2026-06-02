// Thin wrapper around the Electron preload bridge. The preload script
// exposes ``window.electron`` only inside the Electron app; in browsers
// and under jsdom the bridge no-ops so Solid components stay testable.
//
// The migration plan keeps the Electron preload contract unchanged for
// now; later phases may renegotiate it once the chrome+sidebar bundles
// land.

function safeBridge() {
  if (typeof window === 'undefined') return null;
  return window.electron || null;
}

export function isElectron() {
  return safeBridge() !== null;
}

export function navigateContent(url) {
  const bridge = safeBridge();
  if (bridge && typeof bridge.navigateContent === 'function') {
    bridge.navigateContent(url);
    return true;
  }
  return false;
}

export function focusWorkspace(agentId) {
  const bridge = safeBridge();
  if (bridge && typeof bridge.focusWorkspace === 'function') {
    bridge.focusWorkspace(agentId);
    return true;
  }
  return false;
}
