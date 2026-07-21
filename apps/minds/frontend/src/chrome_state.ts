// TypeScript mirror of the chrome data contract in
// imbue/minds/desktop_client/chrome_state.py -- the payloads pushed over the
// /_chrome/events SSE stream (and, in Electron, relayed over the
// onChromeEvent IPC) plus the boot-state island shape. Keep the two files in
// sync when the contract changes.
//
// Optional properties mirror the Python "absent when unset" semantics
// (`exclude_none`); properties typed `| null` are always present on the wire.

export interface ChromeWorkspaceEntry {
  id: string;
  name: string;
  accent: string;
  // "true" when the workspace's provider had a discovery error.
  is_stale?: string;
  // "true" when the mind's host can stop/start (docker/lima today).
  supports_shutdown?: string;
  // RUNNING / STOPPED / UNKNOWN, present only on shutdown-capable minds.
  liveness?: string;
  // "true" for workspaces known only from synced records (another device).
  is_remote?: string;
  location?: string;
  account?: string;
}

export interface ChromeWorkspacesPayload {
  type: "workspaces";
  workspaces: ChromeWorkspaceEntry[];
  destroying_agent_ids: string[];
  // Connect-time snapshot only; absent on diff-driven updates.
  has_accounts?: boolean;
  restorable_workspace_ids?: string[];
  remote_workspace_states: Record<string, string>;
}

export interface ChromeProviderEntry {
  name: string;
  backend: string | null;
  status: "ok" | "error" | "disabled";
  is_enabled: boolean;
  error_type?: string;
  error_message?: string;
}

export interface ChromeProvidersPayload {
  type: "providers_state";
  providers: ChromeProviderEntry[];
  last_event_at: string | null;
  last_full_snapshot_at: string | null;
}

export interface ChromeRequestsPayload {
  type: "requests";
  count: number;
  request_ids: string[];
  auto_open: boolean;
}

export interface ChromeSystemInterfaceStatusPayload {
  type: "system_interface_status";
  agent_id: string;
  // AgentHealth value: healthy / stuck / restarting / restart_failed.
  status: string;
  error?: string;
}

export interface ChromeDiscoveryHealthPayload {
  type: "discovery_health";
  state: string;
}

export interface ChromeOpenHelpPayload {
  type: "open_help";
  description: string;
  workspace_agent_id: string | null;
}

export interface ChromeAuthRequiredPayload {
  type: "auth_required";
}

// Electron-only: emitted by main.js over the onChromeEvent IPC when the
// settings page in this window's bundle previews a freshly-picked accent
// (an optimistic local shortcut; the authoritative value arrives later via
// the normal workspaces payload). Never sent over the SSE stream.
export interface ChromeWorkspaceAccentPreviewEvent {
  type: "workspace_accent_preview";
  agent_id: string;
  accent: string;
}

export type ChromeEvent =
  | ChromeWorkspacesPayload
  | ChromeProvidersPayload
  | ChromeRequestsPayload
  | ChromeSystemInterfaceStatusPayload
  | ChromeDiscoveryHealthPayload
  | ChromeOpenHelpPayload
  | ChromeAuthRequiredPayload
  | ChromeWorkspaceAccentPreviewEvent;

// The connect-time snapshot ChromeShell renders into the #minds-boot-state
// island (ChromeBootState.to_payload_dict() on the Python side). Pages may
// bundle page-specific extras as sibling keys in their own island slice.
export interface ChromeBootState {
  workspaces: ChromeWorkspacesPayload;
  providers: ChromeProvidersPayload;
  requests: ChromeRequestsPayload;
  system_interface_statuses: ChromeSystemInterfaceStatusPayload[];
}
