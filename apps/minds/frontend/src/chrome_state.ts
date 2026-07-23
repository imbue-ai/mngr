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
  // Friendly compute-provider label (e.g. "AWS") for the landing row chip.
  provider?: string;
  // "true" for workspaces known only from synced records (another device).
  is_remote?: string;
  location?: string;
  // The synced record's host id (the remove-record handle); remote rows only.
  host_id?: string;
  // Detail for a remote tile's "error" state (the tooltip text).
  state_detail?: string;
  account?: string;
}

export interface ChromeWorkspacesPayload {
  type: "workspaces";
  workspaces: ChromeWorkspaceEntry[];
  destroying_agent_ids: string[];
  destroying_status_by_agent_id: Record<string, string>;
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

// One pending-request card in the inbox's left list.
export interface ChromeRequestCard {
  id: string;
  // Handler-provided request kind (e.g. "permission request").
  kind_label: string;
  // The requesting workspace's display name.
  ws_name: string;
  // Handler-provided one-line summary of the request.
  display_name: string;
  // The workspace accent hex (mirrors the homepage tile's color).
  accent: string;
}

export interface ChromeRequestsPayload {
  type: "requests";
  count: number;
  request_ids: string[];
  // Card summaries, most-recent-first (same order as request_ids).
  cards: ChromeRequestCard[];
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

// Landing-page-specific boot island data (the ``landing`` sibling of the
// chrome snapshot; mirror of LandingBootExtras in chrome_state.py).
export interface LandingBootExtras {
  mngr_forward_origin: string;
  account_email: string;
  extra_account_count: number;
  locked_account_emails: string[];
  is_discovering: boolean;
}

export interface LandingBootIsland {
  chrome: ChromeBootState;
  landing: LandingBootExtras;
}

// Inbox-page-specific boot island data (the ``inbox`` sibling of the chrome
// snapshot; mirror of InboxBootExtras in chrome_state.py).
export interface InboxBootExtras {
  // The initially-selected request id (empty for none).
  selected_id: string;
  // True only for an intentional whole-inbox open; false dismisses on
  // resolution instead of advancing to the next pending request.
  keep_open: boolean;
}

export interface InboxBootIsland {
  chrome: ChromeBootState;
  inbox: InboxBootExtras;
}

// Sharing-editor boot island data (mirror of SharingBootExtras in
// chrome_state.py). No ``chrome`` sibling: the editor's state comes from its
// own JSON status endpoint, not the chrome SSE stream.
export interface SharingBootExtras {
  agent_id: string;
  service_name: string;
  // Workspace display name for the heading (falls back to the agent id).
  ws_name: string;
  // The bound account's email for the heading; empty hides it.
  account_email: string;
  // URL-proposed draft emails folded into the first load only.
  initial_emails: string[];
  // True in the Electron overlay modal: plain-links heading + dismissing
  // Cancel (nothing may navigate the overlay iframe).
  is_modal: boolean;
  // Bare origin of the mngr forward plugin for the page heading's workspace
  // link; empty in the modal.
  mngr_forward_origin: string;
}

export interface SharingBootIsland {
  sharing: SharingBootExtras;
}

// Creating-page boot island data (mirror of CreatingBootExtras in
// chrome_state.py). No ``chrome`` sibling: the page's live state comes from
// the create-operation status endpoint and its SSE log stream.
export interface CreatingBootExtras {
  // The creation id (minds-internal in-flight handle) the status poll and
  // log stream are keyed by.
  agent_id: string;
  // Server-resolved caption for the current creation status (first paint).
  status_text: string;
  // Expected wall-clock creation duration; drives the progress bar easing.
  expected_duration_seconds: number;
}

export interface CreatingBootIsland {
  creating: CreatingBootExtras;
}

// Destroying-page boot island data (mirror of DestroyingBootExtras in
// chrome_state.py). No ``chrome`` sibling: the page's live state comes from
// the destroy-operation status endpoint and its SSE log stream.
export interface DestroyingBootExtras {
  // The workspace agent id being destroyed (keys the operation endpoints).
  agent_id: string;
  // Workspace display name for the page heading.
  agent_name: string;
  // The destroy worker's pid, shown in the heading's helper line.
  pid: number;
  // Initial server-computed operation status: running / failed / done.
  status: string;
}

export interface DestroyingBootIsland {
  destroying: DestroyingBootExtras;
}

// Error-reporting consent page boot island data (mirror of
// ConsentBootExtras in chrome_state.py).
export interface ConsentBootExtras {
  report_unexpected_errors: boolean;
  include_logs: boolean;
}

export interface ConsentBootIsland {
  consent: ConsentBootExtras;
}

// Authentication-failure page boot island data (mirror of
// AuthErrorBootExtras in chrome_state.py).
export interface AuthErrorBootExtras {
  message: string;
}

export interface AuthErrorBootIsland {
  auth_error: AuthErrorBootExtras;
}

// -- App-level settings island ---------------------------------------------
// Mirrors of the permission-overview models in
// latchkey/permission_overview.py (the Python source of these shapes) plus
// SettingsBootExtras in chrome_state.py.

export interface GrantedPermissionPayload {
  label: string;
  // Plain-English tooltip; empty when the catalog has none.
  description: string;
}

export interface WorkspaceServiceGrantPayload {
  workspace_agent_id: string;
  workspace_name: string;
  host_id: string;
  // Workspace accent color hex (#rrggbb) for the card header dot.
  color: string;
  permissions: GrantedPermissionPayload[];
}

export interface ServicePermissionOverviewPayload {
  // Raw service name (e.g. "slack"); the revoke action key.
  service_name: string;
  display_name: string;
  workspace_grants: WorkspaceServiceGrantPayload[];
}

export interface SharedPathPayload {
  path: string;
  // "read" or "read and write".
  access_label: string;
}

export interface WorkspaceFileSharingGrantPayload {
  workspace_agent_id: string;
  workspace_name: string;
  host_id: string;
  color: string;
  paths: SharedPathPayload[];
}

export interface WorkspaceDelegationVerbPayload {
  // Detent verb schema name (e.g. "minds-workspaces-destroy"); revoke key.
  verb_permission: string;
  label: string;
  description: string;
  is_all_workspaces: boolean;
  target_names: string[];
}

export interface WorkspaceDelegationGrantPayload {
  workspace_agent_id: string;
  workspace_name: string;
  host_id: string;
  color: string;
  verbs: WorkspaceDelegationVerbPayload[];
}

// Mirror of SettingsBootExtras in chrome_state.py.
export interface SettingsBootExtras {
  report_unexpected_errors: boolean;
  include_error_logs: boolean;
  services_overview: ServicePermissionOverviewPayload[];
  file_sharing_grants: WorkspaceFileSharingGrantPayload[];
  workspace_delegation_grants: WorkspaceDelegationGrantPayload[];
  permissions_unavailable: boolean;
  is_master_password_set: boolean;
  is_modal: boolean;
}

export interface SettingsBootIsland {
  settings: SettingsBootExtras;
}

// Mirror of AccountEntryPayload / AccountsBootExtras in chrome_state.py.
export interface AccountEntryPayload {
  user_id: string;
  email: string;
  workspace_count: number;
  is_default: boolean;
  // False renders the "Signed out" indicator (provider block disabled).
  is_enabled: boolean;
}

export interface AccountsBootExtras {
  accounts: AccountEntryPayload[];
  is_modal: boolean;
}

export interface AccountsBootIsland {
  accounts: AccountsBootExtras;
}
