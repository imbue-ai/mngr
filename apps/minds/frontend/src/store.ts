// The chrome-state store: module-level state + explicit notification,
// mithril-style. Components read the getters and rely on the store's own
// m.redraw() after every mutation -- m.redraw() lives HERE, never scattered
// in views. Exactly one host event subscription per document (connect()),
// shared by all mounted components.
//
// The optimistic-update semantics (mind start/stop transients, provider
// toggle pending) are ported from the Landing page's inline script; the
// accent/name cache semantics from chrome.js. Their edge-case comments moved
// here with the code.
import m from "mithril";

import type {
  ChromeBootState,
  ChromeEvent,
  ChromeProvidersPayload,
  ChromeRequestCard,
  ChromeWorkspaceEntry,
} from "./chrome_state";
import type { Host } from "./host";

// The shared backup-health cache (static/backup_health.js), loaded by the
// chrome shell. The store bridges it (rather than re-implementing its fetch
// fan-out) until the phase that deletes the file ports the fetching itself.
export interface BackupHealthBridge {
  get(agentId: string): string | null;
  onUpdate(listener: () => void): void;
}

declare global {
  interface Window {
    mindsBackupHealth?: BackupHealthBridge;
  }
}

interface AccentCacheEntry {
  accent: string | null;
  name: string | null;
}

interface StoreState {
  workspaces: ChromeWorkspaceEntry[];
  destroyingAgentIds: string[];
  destroyingStatusByAgentId: Record<string, string>;
  remoteWorkspaceStates: Record<string, string>;
  hasAccounts: boolean | null;
  restorableWorkspaceIds: string[] | null;
  accentByAgentId: Record<string, AccentCacheEntry>;
  providers: ChromeProvidersPayload | null;
  // Provider name -> the epoch ms of the user's un-acknowledged Enable/Disable
  // click. An entry marks the toggle "pending" until a providers_state whose
  // full-snapshot timestamp is at or past the click arrives (mngr observe has
  // restarted with the new setting by then).
  pendingProviderToggleClickedAtByName: Record<string, number>;
  requestsCount: number;
  requestIds: string[];
  requestCards: ChromeRequestCard[];
  isRequestsAutoOpen: boolean;
  // agent id -> non-healthy AgentHealth value; healthy agents carry no entry.
  systemInterfaceStatusByAgentId: Record<string, string>;
  discoveryHealth: string | null;
  isAuthRequired: boolean;
  // agent id -> the target liveness of an in-flight user Start/Stop. An
  // interim workspaces push (still carrying the pre-action liveness) must not
  // clobber the optimistic transient back; cleared once the authoritative
  // state reaches the target or the action fails.
  pendingMindActionTargetByAgentId: Record<string, string>;
  // agent id -> the client-only optimistic transient (STARTING / STOPPING)
  // shown while the action is in flight.
  transientLivenessByAgentId: Record<string, string>;
  // The accent-source workspace (the active scope, INCLUDING a workspace's
  // settings / sharing screens). Drives the switcher menu's current-row
  // highlight. Electron pushes it over the accent-changed IPC; browser mode's
  // chrome.js pushes its breadcrumb workspace via the MindsUI hook.
  accentScopeAgentId: string | null;
  // Whether any accent-scope push has arrived yet. The accent-to-document
  // effect no-ops until then, so a server-seeded accent (body style +
  // titlebar-surface class) survives the mount instead of being cleared by
  // the store's untouched null scope.
  isAccentScopeKnown: boolean;
  // The workspace whose CONTENT is displayed (narrow: null on its settings /
  // sharing screens). The highlight falls back to it when no scope is set.
  displayedWorkspaceAgentId: string | null;
  // Whether the displayed workspace's content is actually reachable rather
  // than the "Loading workspace" proxy loader. Electron pushes it over
  // current-workspace-changed; browser mode has no such signal (the content
  // frame is cross-origin), so it stays true there.
  isDisplayedContentReady: boolean;
  // The content view's current URL -- the input every titlebar-context
  // selector derives from. Electron pushes it over content-url-changed;
  // browser mode's chrome.js pushes it (URL poll, swaps, local-page boot).
  lastContentUrl: string | null;
}

function initialState(): StoreState {
  return {
    workspaces: [],
    destroyingAgentIds: [],
    destroyingStatusByAgentId: {},
    remoteWorkspaceStates: {},
    hasAccounts: null,
    restorableWorkspaceIds: null,
    accentByAgentId: {},
    providers: null,
    pendingProviderToggleClickedAtByName: {},
    requestsCount: 0,
    requestIds: [],
    requestCards: [],
    isRequestsAutoOpen: true,
    systemInterfaceStatusByAgentId: {},
    discoveryHealth: null,
    isAuthRequired: false,
    pendingMindActionTargetByAgentId: {},
    transientLivenessByAgentId: {},
    accentScopeAgentId: null,
    isAccentScopeKnown: false,
    displayedWorkspaceAgentId: null,
    isDisplayedContentReady: true,
    lastContentUrl: null,
  };
}

let state: StoreState = initialState();
let isConnected = false;
const listeners: Array<() => void> = [];

function notify(): void {
  listeners.forEach((listener) => listener());
  m.redraw();
}

export function subscribe(listener: () => void): () => void {
  listeners.push(listener);
  return () => {
    const idx = listeners.indexOf(listener);
    if (idx >= 0) listeners.splice(idx, 1);
  };
}

// -- Reads -----------------------------------------------------------------

export function getWorkspaces(): ChromeWorkspaceEntry[] {
  return state.workspaces;
}

export function getDestroyingAgentIds(): string[] {
  return state.destroyingAgentIds;
}

// "running" | "failed" while a destroy is in flight / failed; null otherwise.
export function getDestroyingStatus(agentId: string): string | null {
  return state.destroyingStatusByAgentId[agentId] ?? null;
}

export function getRemoteWorkspaceStates(): Record<string, string> {
  return state.remoteWorkspaceStates;
}

export function getHasAccounts(): boolean | null {
  return state.hasAccounts;
}

export function getAccentCacheEntry(agentId: string): AccentCacheEntry | null {
  return state.accentByAgentId[agentId] ?? null;
}

export function getProviders(): ChromeProvidersPayload | null {
  return state.providers;
}

export function isProviderTogglePending(name: string): boolean {
  const clickedAt = state.pendingProviderToggleClickedAtByName[name];
  if (clickedAt === undefined) return false;
  const snapshotAt = lastFullSnapshotMs();
  return snapshotAt === null || clickedAt > snapshotAt;
}

export function getRequestsCount(): number {
  return state.requestsCount;
}

export function getRequestIds(): string[] {
  return state.requestIds;
}

export function getRequestCards(): ChromeRequestCard[] {
  return state.requestCards;
}

export function isRequestsAutoOpen(): boolean {
  return state.isRequestsAutoOpen;
}

export function getSystemInterfaceStatus(agentId: string): string | null {
  return state.systemInterfaceStatusByAgentId[agentId] ?? null;
}

export function getDiscoveryHealth(): string | null {
  return state.discoveryHealth;
}

export function isAuthRequired(): boolean {
  return state.isAuthRequired;
}

// The liveness a row should render: the optimistic transient while a user
// action is in flight, else the authoritative value from the entry.
export function getEffectiveLiveness(entry: ChromeWorkspaceEntry): string | null {
  return state.transientLivenessByAgentId[entry.id] ?? entry.liveness ?? null;
}

export function getBackupWarning(agentId: string): string | null {
  return window.mindsBackupHealth !== undefined ? window.mindsBackupHealth.get(agentId) : null;
}

// The workspace the switcher menu should mark current: the accent-source
// scope when set (a workspace's settings / sharing screens still highlight
// that workspace), else the displayed workspace.
export function getActiveRowAgentId(): string | null {
  return state.accentScopeAgentId ?? state.displayedWorkspaceAgentId;
}

export function setAccentScopeAgentId(agentId: string | null): void {
  state.accentScopeAgentId = agentId;
  state.isAccentScopeKnown = true;
  applyAccentToDocument();
  notify();
}

export function getDisplayedWorkspaceAgentId(): string | null {
  return state.displayedWorkspaceAgentId;
}

export function setDisplayedWorkspaceAgentId(agentId: string | null): void {
  state.displayedWorkspaceAgentId = agentId;
  notify();
}

export function setDisplayedContentReady(isReady: boolean): void {
  state.isDisplayedContentReady = isReady;
  notify();
}

export function isDisplayedContentReady(): boolean {
  return state.isDisplayedContentReady;
}

export function getLastContentUrl(): string | null {
  return state.lastContentUrl;
}

export function setContentUrl(url: string | null): void {
  state.lastContentUrl = url;
  notify();
}

// Seed the accent/name cache from server-rendered hints (the titlebar
// skeleton's crumb, an accent-seeded page) WITHOUT clobbering fresher
// SSE-delivered data -- only gaps are filled.
export function seedAccentCacheHint(agentId: string, name: string | null, accent: string | null): void {
  const existing = state.accentByAgentId[agentId];
  state.accentByAgentId[agentId] = {
    accent: existing?.accent ?? accent,
    name: existing?.name ?? name,
  };
  notify();
}

// -- Accent-to-document effect ----------------------------------------------
//
// The titlebar background is driven by two CSS variables on the document
// root plus the ``titlebar-surface`` class on #minds-titlebar (the
// contrasting foreground is derived from --titlebar-bg in pure CSS -- see
// app.css). The accent is a pure function of the accent-scope workspace and
// the accent cache; cleared back to the neutral chrome when no scope is set.
// When the scope's accent is not cached yet (cold start, freshly-created
// workspace), the bar is left as-is; the next ``workspaces`` tick replays
// this effect with the now-populated cache.
function applyAccentToDocument(): void {
  if (!state.isAccentScopeKnown) return;
  const root = document.documentElement;
  const bar = document.getElementById("minds-titlebar");
  const agentId = state.accentScopeAgentId;
  if (agentId === null) {
    root.style.removeProperty("--workspace-accent");
    root.style.removeProperty("--titlebar-bg");
    bar?.classList.remove("titlebar-surface");
    return;
  }
  const cached = state.accentByAgentId[agentId];
  if (cached === undefined || cached.accent === null) return;
  root.style.setProperty("--workspace-accent", cached.accent);
  root.style.setProperty("--titlebar-bg", cached.accent);
  bar?.classList.add("titlebar-surface");
}

// -- Mutations -------------------------------------------------------------

function lastFullSnapshotMs(): number | null {
  const iso = state.providers?.last_full_snapshot_at ?? null;
  if (iso === null) return null;
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? null : parsed;
}

// Cache each workspace's accent + name so accent application stays a
// synchronous lookup (ported from chrome.js's rememberWorkspaceAccents).
function rememberWorkspaceAccents(workspaces: ChromeWorkspaceEntry[]): void {
  workspaces.forEach((workspace) => {
    state.accentByAgentId[workspace.id] = {
      accent: workspace.accent ?? null,
      name: workspace.name ?? null,
    };
  });
}

function applyAuthoritativeLiveness(workspaces: ChromeWorkspaceEntry[]): void {
  workspaces.forEach((workspace) => {
    const target = state.pendingMindActionTargetByAgentId[workspace.id];
    if (target === undefined) return;
    if (workspace.liveness === target) {
      // The authoritative state reached the target: the action completed, so
      // drop both the pending guard and the transient.
      delete state.pendingMindActionTargetByAgentId[workspace.id];
      delete state.transientLivenessByAgentId[workspace.id];
    }
    // An interim payload still carrying the pre-action state is ignored (the
    // transient keeps rendering) so it doesn't flicker the row back.
  });
}

export function applyChromeEvent(event: ChromeEvent): void {
  switch (event.type) {
    case "workspaces": {
      state.workspaces = event.workspaces;
      state.destroyingAgentIds = event.destroying_agent_ids;
      state.destroyingStatusByAgentId = event.destroying_status_by_agent_id;
      state.remoteWorkspaceStates = event.remote_workspace_states;
      if (event.has_accounts !== undefined) state.hasAccounts = event.has_accounts;
      if (event.restorable_workspace_ids !== undefined) {
        state.restorableWorkspaceIds = event.restorable_workspace_ids;
      }
      rememberWorkspaceAccents(event.workspaces);
      applyAuthoritativeLiveness(event.workspaces);
      // Replay the accent paint now that the cache has fresh data (cold
      // start before any tick, or a settings-page color save).
      applyAccentToDocument();
      break;
    }
    case "providers_state": {
      state.providers = event;
      // Acknowledge pending toggles the new snapshot has caught up with.
      const snapshotAt = lastFullSnapshotMs();
      if (snapshotAt !== null) {
        Object.keys(state.pendingProviderToggleClickedAtByName).forEach((name) => {
          if (state.pendingProviderToggleClickedAtByName[name] <= snapshotAt) {
            delete state.pendingProviderToggleClickedAtByName[name];
          }
        });
      }
      break;
    }
    case "requests": {
      state.requestsCount = event.count;
      state.requestIds = event.request_ids;
      state.requestCards = event.cards;
      state.isRequestsAutoOpen = event.auto_open;
      break;
    }
    case "system_interface_status": {
      if (event.status === "healthy") delete state.systemInterfaceStatusByAgentId[event.agent_id];
      else state.systemInterfaceStatusByAgentId[event.agent_id] = event.status;
      break;
    }
    case "discovery_health": {
      state.discoveryHealth = event.state;
      break;
    }
    case "auth_required": {
      state.isAuthRequired = true;
      break;
    }
    case "workspace_accent_preview": {
      // Optimistic single-workspace cache update. Update the accent WITHOUT
      // dropping the cached name: replacing the whole entry would leave it
      // name-less until the next SSE tick (a shipped-and-fixed chrome.js bug).
      const previous = state.accentByAgentId[event.agent_id];
      state.accentByAgentId[event.agent_id] = {
        accent: event.accent,
        name: previous?.name ?? null,
      };
      applyAccentToDocument();
      break;
    }
    case "open_help": {
      // A command, not state: the shell (chrome.js / Electron main) owns
      // opening the help modal. Nothing to store.
      return;
    }
  }
  notify();
}

// Seed the store from a page's boot-state island so the first mounted view
// renders complete, synchronously -- the SSE/IPC stream then takes over.
export function seed(bootState: ChromeBootState): void {
  applyChromeEvent(bootState.workspaces);
  applyChromeEvent(bootState.providers);
  applyChromeEvent(bootState.requests);
  bootState.system_interface_statuses.forEach((status) => applyChromeEvent(status));
}

// Mark a user-issued Start/Stop in flight: the row optimistically renders the
// transient (STARTING / STOPPING) and interim authoritative pushes that still
// carry the pre-action state are ignored until the target state arrives.
export function beginMindAction(agentId: string, targetLiveness: "RUNNING" | "STOPPED"): void {
  state.pendingMindActionTargetByAgentId[agentId] = targetLiveness;
  state.transientLivenessByAgentId[agentId] = targetLiveness === "RUNNING" ? "STARTING" : "STOPPING";
  notify();
}

// The action's synchronous endpoint confirmed the target state: show it
// immediately (the transient flips to the final state) while keeping the
// pending guard so an interim SSE payload still carrying the pre-action
// state cannot flicker the row back; the payload that reaches the target
// clears both (applyAuthoritativeLiveness).
export function completeMindAction(agentId: string): void {
  const target = state.pendingMindActionTargetByAgentId[agentId];
  if (target === undefined) return;
  state.transientLivenessByAgentId[agentId] = target;
  notify();
}

// The action's request failed: drop the guard + transient so the row falls
// back to the authoritative state.
export function failMindAction(agentId: string): void {
  delete state.pendingMindActionTargetByAgentId[agentId];
  delete state.transientLivenessByAgentId[agentId];
  notify();
}

// Record an un-acknowledged provider Enable/Disable click. ``clickedAtMs`` is
// the caller's Date.now() -- the store takes no time source of its own.
export function beginProviderToggle(name: string, clickedAtMs: number): void {
  state.pendingProviderToggleClickedAtByName[name] = clickedAtMs;
  notify();
}

// The toggle request failed outright: clear the pending marker so the button
// reverts (the caller surfaces the error).
export function failProviderToggle(name: string): void {
  delete state.pendingProviderToggleClickedAtByName[name];
  notify();
}

// Subscribe the store to the document's chrome event stream + the shared
// backup-health cache. Exactly once per document; later calls are no-ops.
export function connect(host: Host): void {
  if (isConnected) return;
  isConnected = true;
  host.onChromeEvent(applyChromeEvent);
  if (window.mindsBackupHealth !== undefined) {
    // Backup verdicts change row badges; the bridge's cache is the source, so
    // a bare notify is enough for views reading getBackupWarning().
    window.mindsBackupHealth.onUpdate(notify);
  }
}

export function resetStoreForTesting(): void {
  state = initialState();
  isConnected = false;
  listeners.length = 0;
}
