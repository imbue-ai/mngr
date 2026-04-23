// Per-agent accent color helper. Mirrors workspace_accent() in
// templates.py: SHA-256 over the agent id, first four bytes mod 360 picks
// the OKLCH hue, fixed L/C match server. Loaded by both chrome.html and
// sidebar.html (and any other page that needs a client-side accent
// fallback) so the logic lives in one place rather than being copy-pasted
// into every per-page script.
//
// Usage:
//   window.mindsAccent.get(agentId, function (color) { ... });
//
// In the common case the server attaches `accent` to each workspace dict
// over SSE and this helper is only used when that field is missing.
(function () {
  var cache = {};

  async function compute(agentId) {
    var enc = new TextEncoder().encode(agentId);
    var digest = await crypto.subtle.digest('SHA-256', enc);
    var view = new DataView(digest);
    var hue = view.getUint32(0, false) % 360;
    return 'oklch(65% 0.15 ' + hue + ')';
  }

  function get(agentId, cb) {
    if (cache[agentId] !== undefined) { cb(cache[agentId]); return; }
    compute(agentId).then(function (c) { cache[agentId] = c; cb(c); });
  }

  window.mindsAccent = { get: get };
})();
