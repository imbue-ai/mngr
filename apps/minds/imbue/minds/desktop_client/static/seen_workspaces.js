// Client-side "seen workspaces" tracker, shared by chrome.js (browser-mode
// inline sidebar) and sidebar.js (the Electron modal sidebar) so the
// blinking-new-tab affordance behaves identically in both. Loaded before
// each of them (see Chrome.jinja / Sidebar.jinja).
//
// The server marks a workspace ``is_new`` when the agent carries the
// Caretaker scheduler's ``auto_created`` / ``caretaker`` label (see
// _build_workspace_list in app.py). A row only actually blinks while it is
// ``is_new`` AND not yet in this client's seen set -- so a tab pulses until
// the user opens it once, then stops forever (per client/device).
//
// Seeding rule: on a brand-new client (no stored set) we seed the set with
// every workspace id currently known, so pre-existing tabs -- including the
// day-1 chat tab that predates this feature -- never blink. Only genuinely
// new workspaces that arrive *after* seeding can pulse.
//
// Persistence: a versioned localStorage key. localStorage can be unavailable
// (private mode, a sandboxed context); we degrade to an in-memory set so the
// feature still works for the session, just without cross-launch memory.
//
// Usage:
//   window.mindsSeenWorkspaces.seedIfFresh(['agent-..', ..]);  // once, idempotent
//   window.mindsSeenWorkspaces.has('agent-..');                // bool
//   window.mindsSeenWorkspaces.markSeen('agent-..');           // bool: newly added?
(function () {
  var STORAGE_KEY = 'minds.seenWorkspaceIds.v1';
  // Sentinel value distinguishing "seeded with an empty set" from "never
  // seeded": once seeding has happened the key is present even if no ids
  // were known, so a later launch with brand-new tabs blinks correctly.
  var SEEDED_KEY = 'minds.seenWorkspaceIds.seeded.v1';

  // In-memory mirror; the source of truth when localStorage is usable, and
  // the only store when it is not.
  var memoryIds = null;
  var memorySeeded = false;

  function readStore() {
    if (memoryIds !== null) return memoryIds;
    var ids = {};
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          parsed.forEach(function (id) { if (typeof id === 'string') ids[id] = true; });
        }
      }
      memorySeeded = window.localStorage.getItem(SEEDED_KEY) === '1';
    } catch (e) {
      // localStorage unavailable or corrupt; fall back to memory-only.
    }
    memoryIds = ids;
    return memoryIds;
  }

  function persist() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.keys(memoryIds)));
      window.localStorage.setItem(SEEDED_KEY, '1');
    } catch (e) {
      // Memory-only mode; nothing to persist.
    }
  }

  function isSeeded() {
    readStore();
    return memorySeeded;
  }

  // Seed the seen set with all currently-known ids, but only the first time
  // ever for this client. After seeding, only ids the user explicitly opens
  // are added. Idempotent: a second call (or a later launch) is a no-op.
  function seedIfFresh(workspaceIds) {
    var ids = readStore();
    if (memorySeeded) return;
    (workspaceIds || []).forEach(function (id) { if (typeof id === 'string') ids[id] = true; });
    memorySeeded = true;
    persist();
  }

  function has(id) {
    return !!readStore()[id];
  }

  // Mark an id seen. Returns true if it was newly added (so the caller can
  // re-render to drop the pulse), false if it was already present.
  function markSeen(id) {
    if (!id) return false;
    var ids = readStore();
    if (ids[id]) return false;
    ids[id] = true;
    persist();
    return true;
  }

  window.mindsSeenWorkspaces = {
    seedIfFresh: seedIfFresh,
    isSeeded: isSeeded,
    has: has,
    markSeen: markSeen,
  };
})();
