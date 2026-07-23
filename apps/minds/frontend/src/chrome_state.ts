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

// -- Inbox request-detail payloads (mirrors of the InboxDetail* models in
// chrome_state.py): the typed replacement for the old server-rendered inbox
// right-pane HTML fragments, served as JSON by GET /inbox/detail/<id> and
// seeded in the inbox island's ``inbox_detail`` key.

export interface InboxDetailUnavailable {
  kind: "unavailable";
  // Optional supporting sentence; empty shows the heading alone.
  message: string;
}

export interface PredefinedPermissionDetail {
  kind: "predefined";
  agent_id: string;
  request_id: string;
  ws_name: string;
  rationale: string;
  display_name: string;
  permission_schemas: string[];
  description_by_permission_name: Record<string, string>;
  checked_permissions: string[];
  // The catch-all permission's stored name (Detent's wildcard) and the
  // clearer user-facing label shown in its place.
  wildcard_permission: string;
  wildcard_label: string;
  // Whether Approve will run a browser sign-in (progress notice copy).
  will_open_browser: boolean;
  // Non-empty selects the deny-only unknown-scope variant.
  unknown_scope: string;
}

export interface FileSharingPermissionDetail {
  kind: "file_sharing";
  agent_id: string;
  request_id: string;
  ws_name: string;
  rationale: string;
  file_path: string;
  // READ or WRITE.
  access: string;
  access_human_label: string;
  // Absolute WebDAV mount roots; Approve is blocked for paths outside them.
  allowed_roots: string[];
  // Absolute home directory for client-side ~ expansion.
  home_dir: string;
}

export interface AccountsPermissionDetail {
  kind: "accounts";
  agent_id: string;
  request_id: string;
  ws_name: string;
  rationale: string;
}

export interface WorkspaceVerbOption {
  permission: string;
  display_name: string;
  description: string;
  is_targeted: boolean;
  is_checked: boolean;
}

export interface WorkspacePermissionDetail {
  kind: "workspace";
  agent_id: string;
  request_id: string;
  ws_name: string;
  rationale: string;
  display_name: string;
  verbs: WorkspaceVerbOption[];
  target_workspace_id: string;
  target_workspace_name: string;
  // Whether the all-vs-selected target radio is offered.
  show_target_choice: boolean;
}

export type InboxDetailPayload =
  | InboxDetailUnavailable
  | PredefinedPermissionDetail
  | FileSharingPermissionDetail
  | AccountsPermissionDetail
  | WorkspacePermissionDetail;

export interface InboxBootIsland {
  chrome: ChromeBootState;
  inbox: InboxBootExtras;
  // The initially-selected request's detail payload; absent when the inbox
  // is empty or nothing could be resolved.
  inbox_detail?: InboxDetailPayload;
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
  // Whether the workspace has an associated account; false renders the
  // associate prompt instead of the editor.
  has_account: boolean;
  // Signed-in accounts offered by the associate prompt (when unassociated).
  associate_accounts: AssociateAccountPayload[];
  // Where a successful association returns to; empty reloads in place.
  redirect_url: string;
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

// Mirror of AssociateAccountPayload / WorkspaceSettingsBootExtras in
// chrome_state.py.
export interface AssociateAccountPayload {
  user_id: string;
  email: string;
}

export interface WorkspaceSettingsBootExtras {
  agent_id: string;
  ws_name: string;
  // Stored workspace color hex (#rrggbb); pre-selects the picker.
  current_color: string;
  // The pickable palette swatches as an ordered name -> hex map.
  palette: Record<string, string>;
  // Provider-health flag; true disables rename/color controls.
  is_stale: boolean;
  // True for hosts leased from Imbue Cloud: account association is fixed.
  is_leased_imbue_cloud: boolean;
  has_account: boolean;
  // The associated account's email; empty when unassociated.
  current_account_email: string;
  associate_accounts: AssociateAccountPayload[];
  servers: string[];
}

export interface WorkspaceSettingsBootIsland {
  workspace_settings: WorkspaceSettingsBootExtras;
}

// Mirror of CreateFormBootExtras in chrome_state.py. Carries the effective
// (preset-resolved) selections plus the option lists; the advanced selects
// are the source of truth on submit.
export interface CreateFormBootExtras {
  git_url: string;
  branch: string;
  host_name: string;
  color: string;
  launch_modes: string[];
  selected_launch_mode: string;
  ai_providers: string[];
  selected_ai_provider: string;
  docker_runtimes: string[];
  selected_docker_runtime: string;
  backup_providers: string[];
  selected_backup_provider: string;
  backup_api_key_env: string;
  accounts: AssociateAccountPayload[];
  default_account_id: string;
  anthropic_api_key: string;
  error_message: string;
  region_options_by_launch_mode: Record<string, string[]>;
  region_selected_by_launch_mode: Record<string, string>;
  selected_preset: string;
  start_advanced: boolean;
}

export interface CreateFormBootIsland {
  create: CreateFormBootExtras;
}

// SuperTokens auth surfaces (mirrors of the *BootExtras in chrome_state.py).
export interface AuthFormBootExtras {
  default_to_signup: boolean;
  // Modal-only copy above the tabs; empty on the standalone page.
  intro: string;
  // Standalone-page info banner; empty otherwise.
  message: string;
  // Safe local path a successful sign-in / the back link returns to.
  return_to: string;
  // True for the overlay sign-in modal (backdrop + card + host dismissal).
  is_modal: boolean;
}

export interface AuthFormBootIsland {
  auth: AuthFormBootExtras;
}

export interface CheckEmailBootExtras {
  email: string;
}

export interface CheckEmailBootIsland {
  check_email: CheckEmailBootExtras;
}

export interface OauthCloseBootExtras {
  email: string;
  display_name: string;
}

export interface OauthCloseBootIsland {
  oauth_close: OauthCloseBootExtras;
}

export interface AccountSettingsBootExtras {
  email: string;
  display_name: string;
  provider: string;
  user_id_prefix: string;
}

export interface AccountSettingsBootIsland {
  account_settings: AccountSettingsBootExtras;
}

// Get-help modal boot island data (mirror of HelpBootExtras in
// chrome_state.py).
export interface HelpBootExtras {
  // Persistent include-logs preference; true always attaches logs and hides
  // the one-off checkbox.
  include_logs_setting: boolean;
  // The workspace the help flow was opened from; empty on a general screen.
  workspace_agent_id: string;
  // Whether the have-an-agent-help option is offered.
  assist_available: boolean;
  // Pre-filled report text (an in-workspace /assist agent's diagnosis).
  description: string;
  // True for the agent-escalation flow: no mode choice, agent framing.
  is_agent_report: boolean;
  workspace_name: string;
}

export interface HelpBootIsland {
  help: HelpBootExtras;
}
