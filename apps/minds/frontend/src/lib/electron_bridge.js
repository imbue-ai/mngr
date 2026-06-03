// Thin wrapper around the Electron preload bridges. The preload script
// exposes ``window.minds`` (chrome / workspace IPC) and may expose
// ``window.electron`` (focus/navigation hooks); in browsers and under
// jsdom both bridges are absent so the Solid components stay testable.
//
// The migration plan keeps the Electron preload contract unchanged for
// now; later phases may renegotiate it once the chrome+sidebar bundles
// land.

function safeBridge() {
  if (typeof window === 'undefined') return null;
  return window.electron || null;
}

function safeMinds() {
  if (typeof window === 'undefined') return null;
  return window.minds || null;
}

export function isElectron() {
  return safeMinds() !== null || safeBridge() !== null;
}

export function navigateContent(url) {
  const minds = safeMinds();
  if (minds && typeof minds.navigateContent === 'function') {
    minds.navigateContent(url);
    return true;
  }
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

// -- window.minds IPC helpers used by the chrome and sidebar bundles ----

export function contentGoBack() {
  const minds = safeMinds();
  if (minds && typeof minds.contentGoBack === 'function') {
    minds.contentGoBack();
    return true;
  }
  return false;
}

export function contentGoForward() {
  const minds = safeMinds();
  if (minds && typeof minds.contentGoForward === 'function') {
    minds.contentGoForward();
    return true;
  }
  return false;
}

export function toggleSidebar() {
  const minds = safeMinds();
  if (minds && typeof minds.toggleSidebar === 'function') {
    minds.toggleSidebar();
    return true;
  }
  return false;
}

export function toggleRequestsPanel() {
  const minds = safeMinds();
  if (minds && typeof minds.toggleRequestsPanel === 'function') {
    minds.toggleRequestsPanel();
    return true;
  }
  return false;
}

export function minimizeWindow() {
  const minds = safeMinds();
  if (minds && typeof minds.minimize === 'function') {
    minds.minimize();
    return true;
  }
  return false;
}

export function maximizeWindow() {
  const minds = safeMinds();
  if (minds && typeof minds.maximize === 'function') {
    minds.maximize();
    return true;
  }
  return false;
}

export function closeWindow() {
  const minds = safeMinds();
  if (minds && typeof minds.close === 'function') {
    minds.close();
    return true;
  }
  return false;
}

export function openWorkspaceInNewWindow(agentId) {
  const minds = safeMinds();
  if (minds && typeof minds.openWorkspaceInNewWindow === 'function') {
    minds.openWorkspaceInNewWindow(agentId);
    return true;
  }
  return false;
}

export function showWorkspaceContextMenu(agentId, x, y) {
  const minds = safeMinds();
  if (minds && typeof minds.showWorkspaceContextMenu === 'function') {
    minds.showWorkspaceContextMenu(agentId, x, y);
    return true;
  }
  return false;
}

export function closeModal() {
  const minds = safeMinds();
  if (minds && typeof minds.closeModal === 'function') {
    minds.closeModal();
    return true;
  }
  return false;
}

// Subscription helpers. Each registers ``handler`` with the matching
// window.minds IPC channel (if present) and returns a no-op when not in
// Electron. They never throw -- if the channel is missing the call is
// a quiet no-op so components can register unconditionally.

export function onWindowTitleChange(handler) {
  const minds = safeMinds();
  if (minds && typeof minds.onWindowTitleChange === 'function') {
    minds.onWindowTitleChange(handler);
    return true;
  }
  return false;
}

export function onContentTitleChange(handler) {
  const minds = safeMinds();
  if (minds && typeof minds.onContentTitleChange === 'function') {
    minds.onContentTitleChange(handler);
    return true;
  }
  return false;
}

export function onContentURLChange(handler) {
  const minds = safeMinds();
  if (minds && typeof minds.onContentURLChange === 'function') {
    minds.onContentURLChange(handler);
    return true;
  }
  return false;
}

export function onCurrentWorkspaceChanged(handler) {
  const minds = safeMinds();
  if (minds && typeof minds.onCurrentWorkspaceChanged === 'function') {
    minds.onCurrentWorkspaceChanged(handler);
    return true;
  }
  return false;
}

export function onChromeEvent(handler) {
  const minds = safeMinds();
  if (minds && typeof minds.onChromeEvent === 'function') {
    minds.onChromeEvent(handler);
    return true;
  }
  return false;
}
