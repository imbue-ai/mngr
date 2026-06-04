// Per-workspace color helper. Replaces the legacy workspace_accent.js
// hash-derived OKLCH client-side. The server is the source of truth now:
//
//   GET  /api/workspace-color/<agent_id>  -> { color, resolved_hex, theme }
//   POST /api/workspace-color/<agent_id>  body { color: <preset-or-literal> }
//
// Exposed surface:
//   window.mindsWorkspaceColor.get(agentId, callback)
//     -> fetch + cache; calls callback({ color, resolved_hex, theme })
//   window.mindsWorkspaceColor.apply(agentId, newColor)
//     -> POST then live-apply to <html>; returns a Promise
//   window.mindsWorkspaceColor.applyToHtml(htmlEl, resolvedHex, theme)
//     -> low-level: set data-theme + --workspace-bg on the given element
//
// The .apply() helper is shared between the titlebar quick-flip flyout
// and the WorkspaceSettings color picker -- both flip the chrome live
// (no page reload). A 150ms transition on --workspace-bg lives in
// tokens.css; this helper just sets the property and lets CSS animate.
(function () {
  var cache = {};

  function applyToHtml(htmlEl, resolvedHex, theme) {
    if (!htmlEl) return;
    htmlEl.setAttribute('data-theme', theme);
    htmlEl.style.setProperty('--workspace-bg', resolvedHex);
  }

  function get(agentId, callback) {
    if (cache[agentId] !== undefined) { callback(cache[agentId]); return; }
    fetch('/api/workspace-color/' + encodeURIComponent(agentId), {
      credentials: 'same-origin',
    }).then(function (resp) {
      if (!resp.ok) throw new Error('workspace-color GET failed: ' + resp.status);
      return resp.json();
    }).then(function (data) {
      cache[agentId] = data;
      callback(data);
    }).catch(function (err) {
      // On network errors the picker falls back gracefully -- callers
      // typically ignore the failure and keep the existing color.
      console.error('mindsWorkspaceColor.get failed:', err);
      callback(null);
    });
  }

  function apply(agentId, newColor) {
    return fetch('/api/workspace-color/' + encodeURIComponent(agentId), {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ color: newColor }),
    }).then(function (resp) {
      if (!resp.ok) throw new Error('workspace-color POST failed: ' + resp.status);
      // The POST returns the resolved hex + theme so we don't have to
      // recompute luminance client-side.
      return resp.json();
    }).then(function (data) {
      cache[agentId] = data;
      applyToHtml(document.documentElement, data.resolved_hex, data.theme);
      return data;
    });
  }

  // Mark agentId as the most-recently-visited workspace (persists in
  // MindsConfig) and apply its color to the chrome's <html> in place.
  // Called from chrome.js whenever the iframe navigates into a workspace
  // URL, so the titlebar + sidebar + any pre-workspace pages (Landing,
  // Welcome) flip to the workspace's color and stay there until a
  // different workspace is opened. Idempotent if agentId is already
  // active: the POST just confirms the existing state.
  function activate(agentId) {
    if (!agentId) return Promise.resolve(null);
    return fetch('/api/active-workspace/' + encodeURIComponent(agentId), {
      method: 'POST',
      credentials: 'same-origin',
    }).then(function (resp) {
      if (!resp.ok) throw new Error('active-workspace POST failed: ' + resp.status);
      return resp.json();
    }).then(function (data) {
      cache[agentId] = data;
      applyToHtml(document.documentElement, data.resolved_hex, data.theme);
      return data;
    });
  }

  window.mindsWorkspaceColor = {
    get: get,
    apply: apply,
    activate: activate,
    applyToHtml: applyToHtml,
  };
})();
