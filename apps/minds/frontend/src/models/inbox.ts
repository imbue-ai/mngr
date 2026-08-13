// Inbox model: typed card list + per-kind detail payloads + grant/deny flow.
//
// Ports the legacy Inbox.jinja shell semantics onto the /ui/api inbox routes:
// master/detail selection, Approve busy states (progress -> granted/denied /
// needs-manual-credentials / failed), fire-and-forget Deny with "(denying...)"
// cards, advance-after-resolution vs dismiss (keep-open semantics), the
// file-sharing path validation (home expansion + within-roots), and the
// predefined dialog's wildcard-permission exclusivity. Grants/denies submit
// the SAME form fields to the legacy /requests/<id>/grant|deny routes.

import type { RequestsStore } from "./requests";

export interface InboxCard {
  id: string;
  kind_label: string;
  ws_name: string;
  display_name: string;
  accent: string;
}

export interface PermissionAccountChoice {
  value: string;
  label: string;
  hint: string;
  /** Picking this account has to establish credentials before the grant applies. */
  is_credential_setup_needed: boolean;
  /** Picking this account also requires the user to name it. */
  is_account_name_needed: boolean;
}

export interface WorkspaceVerbChoice {
  permission: string;
  display_name: string;
  description: string;
  is_targeted: boolean;
}

/** One value of a service's credential command that the user must fill in. */
export interface ManualCredentialParameter {
  name: string;
  label: string;
}

/** The credential form shown while an account that needs credentials is selected.
 * An empty `parameters` means Minds cannot work out what to ask for: the dialog
 * shows `message` as an error and offers no Approve. */
export interface ManualCredentialsPrompt {
  parameters: ManualCredentialParameter[];
  message: string;
}

export interface PredefinedPermissionDetail {
  kind: "predefined";
  request_id: string;
  agent_id: string;
  ws_name: string;
  rationale: string;
  scope: string;
  display_name: string;
  permission_schemas: string[];
  description_by_permission_name: Record<string, string>;
  checked_permissions: string[];
  account_choices: PermissionAccountChoice[];
  selected_account_value: string;
  new_account_value: string;
  wildcard_permission: string;
  wildcard_label: string;
  will_open_browser: boolean;
  manual_credentials: ManualCredentialsPrompt | null;
}

export interface FileSharingPermissionDetail {
  kind: "file_sharing";
  request_id: string;
  agent_id: string;
  ws_name: string;
  rationale: string;
  file_path: string;
  access: string;
  access_human_label: string;
  allowed_roots: string[];
  home_dir: string;
}

export interface WorkspacePermissionDetail {
  kind: "workspace";
  request_id: string;
  agent_id: string;
  ws_name: string;
  rationale: string;
  verbs: WorkspaceVerbChoice[];
  checked_permissions: string[];
  target_workspace_id: string | null;
  target_workspace_name: string | null;
  show_target_choice: boolean;
}

export interface AccountsPermissionDetail {
  kind: "accounts";
  request_id: string;
  agent_id: string;
  ws_name: string;
  rationale: string;
}

export interface UnknownScopeDetail {
  kind: "unknown_scope";
  request_id: string;
  scope: string;
}

export interface UnsupportedDetail {
  kind: "unsupported";
  message: string;
}

export interface UnavailableDetail {
  kind: "unavailable";
  message: string;
}

export type InboxDetail =
  | PredefinedPermissionDetail
  | FileSharingPermissionDetail
  | WorkspacePermissionDetail
  | AccountsPermissionDetail
  | UnknownScopeDetail
  | UnsupportedDetail
  | UnavailableDetail;

export interface GrantResponse {
  outcome: "GRANTED" | "DENIED" | "NEEDS_MANUAL_CREDENTIALS" | "FAILED" | string;
  message?: string;
  manual_credentials?: ManualCredentialsPrompt;
}

/** Expand a leading `~` / `~/` to the home dir (port of the legacy dialog JS;
 * `~user` stays unchanged so the roots check rejects it, matching the server). */
export function expandSharePathHome(value: string, homeDir: string): string {
  if (!homeDir) return value;
  if (value === "~" || value.startsWith("~/")) return homeDir + value.slice(1);
  return value;
}

/** Case-insensitive, purely lexical at-or-beneath check mirroring the server. */
export function isSharePathWithinRoots(value: string, roots: readonly string[]): boolean {
  if (!value) return false;
  const lower = value.toLowerCase();
  return roots.some((root) => {
    const normalized = String(root).replace(/\/+$/, "").toLowerCase() || "/";
    return lower === normalized || lower.startsWith(normalized + "/");
  });
}

/** While the wildcard permission is checked, the specific boxes are disabled
 * (they keep their own state) so only the active side gets submitted. */
export function isPermissionCheckboxDisabled(
  permission: string,
  wildcardPermission: string,
  checked: ReadonlySet<string>,
): boolean {
  return permission !== wildcardPermission && checked.has(wildcardPermission);
}

interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

export interface InboxModelOptions {
  /** Injected in tests; defaults to window.fetch. */
  fetcher?: FetchLike;
  /** Called when resolving a request should dismiss the inbox surface. */
  onClose?: () => void;
  /** Injected in tests; defaults to m.redraw (loaded lazily to keep the model DOM-free). */
  redraw?: () => void;
}

// The requests store the shell attaches at boot so an open inbox page reacts
// to live pending-set changes. Attached from the app entrypoint; null until
// then (the page still works, it just refreshes only on its own actions).
let attachedRequestsStore: RequestsStore | null = null;

export function attachInboxRequestsStore(store: RequestsStore): void {
  attachedRequestsStore = store;
}

export function getAttachedRequestsStore(): RequestsStore | null {
  return attachedRequestsStore;
}

export class InboxModel {
  cards: InboxCard[] = [];
  /** True once a list load ATTEMPT finished (even a failed one). */
  isListLoaded = false;
  /** User-visible reason the last list load failed; null when it succeeded. */
  listErrorMessage: string | null = null;
  autoOpen = true;
  selectedId: string | null = null;
  detail: InboxDetail | null = null;
  isDetailLoading = false;
  isApproveBusy = false;
  isProgressShown = false;
  errorMessage: string | null = null;
  /** Prompt returned by the last Approve; it replaces the detail's own copy so
   * the form explains what went wrong with the attempt. */
  manualCredentialsFeedback: ManualCredentialsPrompt | null = null;
  /** Set when an approval comes back unresolved, so the view scrolls the notice
   * the user has to read into view -- it can be a scroll away from the buttons
   * they just clicked. Consumed once, by whichever notice rendered. */
  private isFailureScrollPending = false;
  /** What the user typed into the credential form, keyed by parameter name. */
  manualCredentialValues: Record<string, string> = {};
  /** Name for the new account the manually-entered credentials belong to. */
  manualAccountName = "";
  /** Ids whose deny POST is in flight (cards fade + become unclickable). */
  denyingIds = new Set<string>();
  /** Whether resolving a request advances to the next one (true) or dismisses (false). */
  isKeepOpen = true;

  // Per-detail editable state (lives here so views stay stateless).
  checkedPermissions = new Set<string>();
  selectedAccount = "";
  filePathValue = "";
  targetScope: "selected" | "all" = "selected";

  private readonly options: InboxModelOptions;
  private lastPendingKey = "";

  constructor(options: InboxModelOptions = {}) {
    this.options = options;
  }

  private fetcher(): FetchLike {
    return this.options.fetcher ?? ((url, init) => fetch(url, { credentials: "same-origin", ...init }));
  }

  private redraw(): void {
    this.options.redraw?.();
  }

  private close(): void {
    this.options.onClose?.();
  }

  async loadList(): Promise<void> {
    try {
      const response = await this.fetcher()("/ui/api/inbox");
      if (!response.ok) {
        this.markListLoadFailed();
        return;
      }
      const body = (await response.json()) as { cards: InboxCard[]; auto_open: boolean };
      this.cards = body.cards;
      this.autoOpen = body.auto_open;
    } catch {
      this.markListLoadFailed();
      return;
    }
    this.listErrorMessage = null;
    this.isListLoaded = true;
    // Prune deny markers for cards the server has dropped.
    this.denyingIds = new Set([...this.denyingIds].filter((id) => this.cards.some((card) => card.id === id)));
    this.redraw();
  }

  private markListLoadFailed(): void {
    this.listErrorMessage = "Could not load requests. They will be retried automatically.";
    // The attempt completed: the page's live-refresh gate (isListLoaded)
    // must open so the store-driven reconciliation keeps running...
    this.isListLoaded = true;
    // ...and the pending-set key must be forgotten so that reconciliation
    // actually retries the load instead of seeing an unchanged set.
    this.lastPendingKey = "";
    this.redraw();
  }

  async select(id: string): Promise<void> {
    if (this.denyingIds.has(id)) return;
    this.selectedId = id;
    this.isDetailLoading = true;
    this.errorMessage = null;
    this.manualCredentialsFeedback = null;
    this.isFailureScrollPending = false;
    this.manualCredentialValues = {};
    this.manualAccountName = "";
    this.isProgressShown = false;
    this.redraw();
    const response = await this.fetcher()(`/ui/api/inbox/${encodeURIComponent(id)}/detail`);
    this.isDetailLoading = false;
    if (!response.ok) {
      this.detail = { kind: "unavailable", message: "" };
      this.redraw();
      return;
    }
    const body = (await response.json()) as { detail: InboxDetail };
    this.detail = body.detail;
    this.seedEditableStateFromDetail(body.detail);
    this.redraw();
  }

  private seedEditableStateFromDetail(detail: InboxDetail): void {
    this.checkedPermissions = new Set();
    this.selectedAccount = "";
    this.filePathValue = "";
    this.targetScope = "selected";
    if (detail.kind === "predefined") {
      this.checkedPermissions = new Set(detail.checked_permissions);
      this.selectedAccount = detail.selected_account_value;
    } else if (detail.kind === "workspace") {
      this.checkedPermissions = new Set(detail.checked_permissions);
      this.targetScope = detail.show_target_choice ? "selected" : "all";
    } else if (detail.kind === "file_sharing") {
      this.filePathValue = detail.file_path;
      this.checkedPermissions = new Set(["file-sharing"]);
    } else if (detail.kind === "accounts") {
      this.checkedPermissions = new Set(["accounts"]);
    } else {
      // Unavailable/unsupported/unknown-scope details have no editable state.
    }
  }

  /** The account radio currently selected, when the detail offers one. */
  selectedAccountChoice(): PermissionAccountChoice | null {
    const detail = this.detail;
    if (detail === null || detail.kind !== "predefined") return null;
    return detail.account_choices.find((choice) => choice.value === this.selectedAccount) ?? null;
  }

  /** The credential form to render, if the selected account needs one. It is
   * part of the detail, so it shows up front rather than after a first Approve. */
  manualCredentialsPrompt(): ManualCredentialsPrompt | null {
    const detail = this.detail;
    if (detail === null || detail.kind !== "predefined" || detail.manual_credentials === null) return null;
    const choice = this.selectedAccountChoice();
    if (choice === null || !choice.is_credential_setup_needed) return null;
    return this.manualCredentialsFeedback ?? detail.manual_credentials;
  }

  /** Whether the visible credential form is reporting a failed attempt rather
   * than giving its opening instruction (a form with no inputs is always one). */
  isManualCredentialsFailureShown(): boolean {
    const prompt = this.manualCredentialsPrompt();
    if (prompt === null) return false;
    return this.manualCredentialsFeedback !== null || prompt.parameters.length === 0;
  }

  /** Whether the view should scroll its failure notice into view now. True at
   * most once per failed approval. */
  takePendingFailureScroll(): boolean {
    if (!this.isFailureScrollPending) return false;
    this.isFailureScrollPending = false;
    return true;
  }

  /** Whether the visible credential form also asks the user to name the account. */
  isManualAccountNameNeeded(): boolean {
    return this.manualCredentialsPrompt() !== null && (this.selectedAccountChoice()?.is_account_name_needed ?? false);
  }

  /** Whether a credential form is open whose inputs are not all filled in yet.
   * A form with no parameters is an error state, not something Approve can fix. */
  private isManualCredentialFormIncomplete(): boolean {
    const prompt = this.manualCredentialsPrompt();
    if (prompt === null) return false;
    if (prompt.parameters.length === 0) return true;
    if (this.isManualAccountNameNeeded() && this.manualAccountName.trim() === "") return true;
    return prompt.parameters.some((parameter) => (this.manualCredentialValues[parameter.name] ?? "").trim() === "");
  }

  /** Whether the Approve button is enabled for the current detail + edits. */
  isApproveAllowed(): boolean {
    const detail = this.detail;
    if (detail === null || this.isApproveBusy) return false;
    if (this.isManualCredentialFormIncomplete()) return false;
    if (detail.kind === "predefined" || detail.kind === "workspace") {
      return this.checkedPermissions.size > 0;
    }
    if (detail.kind === "file_sharing") {
      const expanded = expandSharePathHome(this.filePathValue.trim(), detail.home_dir);
      return expanded.length > 0 && isSharePathWithinRoots(expanded, detail.allowed_roots);
    }
    if (detail.kind === "accounts") return true;
    return false;
  }

  /** Non-empty but out-of-roots file path: show the instant hint (legacy parity). */
  isSharePathHintShown(): boolean {
    const detail = this.detail;
    if (detail === null || detail.kind !== "file_sharing") return false;
    const expanded = expandSharePathHome(this.filePathValue.trim(), detail.home_dir);
    return expanded.length > 0 && !isSharePathWithinRoots(expanded, detail.allowed_roots);
  }

  private buildGrantForm(): FormData {
    const form = new FormData();
    const detail = this.detail;
    if (detail === null) return form;
    if (detail.kind === "predefined") {
      for (const permission of this.checkedPermissions) form.append("permissions", permission);
      form.append("account", this.selectedAccount);
      this.appendManualCredentialFields(form);
    } else if (detail.kind === "workspace") {
      for (const permission of this.checkedPermissions) form.append("permissions", permission);
      form.append("target_scope", this.targetScope);
    } else if (detail.kind === "file_sharing") {
      form.append("permissions", "file-sharing");
      form.append("file_path", expandSharePathHome(this.filePathValue.trim(), detail.home_dir));
    } else {
      // Accounts grants carry no parameters (all-or-nothing approve).
    }
    return form;
  }

  /** Carry the open credential form's values back so the server can run the command. */
  private appendManualCredentialFields(form: FormData): void {
    const prompt = this.manualCredentialsPrompt();
    if (prompt === null || prompt.parameters.length === 0) return;
    const values: Record<string, string> = {};
    for (const parameter of prompt.parameters) {
      values[parameter.name] = this.manualCredentialValues[parameter.name] ?? "";
    }
    form.append("manual_credentials", JSON.stringify(values));
    form.append("account_name", this.manualAccountName);
  }

  async approve(): Promise<void> {
    const resolvedId = this.selectedId;
    if (resolvedId === null || !this.isApproveAllowed()) return;
    const body = this.buildGrantForm();
    this.isApproveBusy = true;
    this.isProgressShown = true;
    this.errorMessage = null;
    // Drop the previous attempt's feedback; the form itself stays visible
    // (it belongs to the detail) with everything the user typed.
    this.manualCredentialsFeedback = null;
    this.redraw();
    try {
      const response = await this.fetcher()(`/requests/${encodeURIComponent(resolvedId)}/grant`, {
        method: "POST",
        body,
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `HTTP ${response.status}`);
      }
      const data = (await response.json()) as GrantResponse;
      if (data.outcome === "GRANTED" || data.outcome === "DENIED") {
        await this.advanceAfterResolution(resolvedId);
        return;
      }
      this.isProgressShown = false;
      // The request stays pending either way, so the reason has to be read.
      this.isFailureScrollPending = true;
      if (data.outcome === "NEEDS_MANUAL_CREDENTIALS") {
        // Keep whatever the user already typed: a rejected credential is
        // usually one field away from being right.
        this.manualCredentialsFeedback = data.manual_credentials ?? {
          parameters: [],
          message: data.message ?? "",
        };
      } else {
        // FAILED (and anything unrecognized): request stays pending; show
        // the reason and let the user retry.
        this.errorMessage = data.message ?? "Approval failed; please try again.";
      }
    } catch (error) {
      this.isProgressShown = false;
      this.isFailureScrollPending = true;
      this.errorMessage = error instanceof Error ? error.message : String(error);
    } finally {
      this.isApproveBusy = false;
      this.redraw();
    }
  }

  deny(): void {
    const resolvedId = this.selectedId;
    if (resolvedId === null) return;
    this.denyingIds.add(resolvedId);
    // Fire-and-forget (keepalive) so the user never waits on the mngr
    // message round trip and the next-item swap starts immediately.
    void this.fetcher()(`/requests/${encodeURIComponent(resolvedId)}/deny`, {
      method: "POST",
      keepalive: true,
    }).catch(() => undefined);
    void this.advanceAfterResolution(resolvedId);
  }

  async advanceAfterResolution(resolvedId: string): Promise<void> {
    if (!this.isKeepOpen) {
      // Opened for a single request (auto-open/notification): resolving it
      // dismisses the surface rather than surfacing an unrelated stale one.
      this.close();
      return;
    }
    const nextId = this.findNextPendingId(resolvedId);
    await this.loadList();
    let target = nextId;
    if (target !== null) {
      const stillSelectable = this.cards.some((card) => card.id === target && !this.denyingIds.has(card.id));
      if (!stillSelectable) {
        const fallback = this.cards.find((card) => !this.denyingIds.has(card.id));
        target = fallback ? fallback.id : null;
      }
    }
    if (target !== null) {
      await this.select(target);
    } else {
      this.close();
    }
  }

  private findNextPendingId(resolvedId: string): string | null {
    const selectable = this.cards.filter((card) => !this.denyingIds.has(card.id));
    const index = selectable.findIndex((card) => card.id === resolvedId);
    if (index === -1) {
      const other = selectable.find((card) => card.id !== resolvedId);
      return other ? other.id : null;
    }
    const after = selectable.slice(index + 1).find((card) => card.id !== resolvedId);
    if (after) return after.id;
    const before = [...selectable.slice(0, index)].reverse().find((card) => card.id !== resolvedId);
    return before ? before.id : null;
  }

  /** React to a live pending-set change (from the requests store). */
  async refreshIfPendingChanged(requestIds: readonly string[]): Promise<void> {
    const key = requestIds.join(",");
    if (key === this.lastPendingKey) return;
    this.lastPendingKey = key;
    const selected = this.selectedId;
    await this.loadList();
    if (selected !== null && !requestIds.includes(selected)) {
      // The selection was resolved elsewhere; the server owns the copy.
      await this.select(selected);
    }
    this.redraw();
  }

  setAutoOpen(enabled: boolean): void {
    this.autoOpen = enabled;
    void this.fetcher()("/ui/api/inbox/auto-open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
      keepalive: true,
    }).catch(() => undefined);
  }
}
