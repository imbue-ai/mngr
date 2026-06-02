// Per-agent accent color. Mirrors workspace_accent() in Python's
// imbue.minds.desktop_client.templates and the legacy
// static/workspace_accent.js helper:
//
//   SHA-256(agent_id) -> first 4 bytes (big-endian) -> hue = uint32 % 360
//   color = oklch(65% 0.15 <hue>)
//
// Fixed lightness/chroma so the only axis of variation is hue. Any change
// to the inputs (algorithm, L, C, hue mod) must be made in lockstep with
// the Python side or per-workspace stripes will drift between SSR HTML
// and client rehydration.

const LIGHTNESS_PERCENT = 65;
const CHROMA = 0.15;
const HUE_MOD = 360;

const cache = new Map();

async function sha256Bytes(text) {
  // crypto.subtle is available in both browsers and in Node >= 19. We use
  // the same algorithm in the SSR sidecar (Node) and in the client.
  const enc = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest('SHA-256', enc);
  return new DataView(digest);
}

export async function workspaceAccent(agentId) {
  if (cache.has(agentId)) return cache.get(agentId);
  const view = await sha256Bytes(agentId);
  const hue = view.getUint32(0, false) % HUE_MOD;
  const color = `oklch(${LIGHTNESS_PERCENT}% ${CHROMA} ${hue})`;
  cache.set(agentId, color);
  return color;
}

// Synchronous variant for hot loops -- callers must seed the cache via
// `workspaceAccent(agentId)` first, otherwise this falls back to the
// neutral default (matches the CSS fallback in globals.css).
export function workspaceAccentCached(agentId) {
  if (cache.has(agentId)) return cache.get(agentId);
  return `oklch(${LIGHTNESS_PERCENT}% ${CHROMA} 230)`;
}

// Test-only escape hatch so unit tests can clear cross-test state without
// reaching into the module's internals.
export function _clearAccentCacheForTests() {
  cache.clear();
}
