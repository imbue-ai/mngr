// Inbox model: the pending-request queue + per-kind detail payloads + the
// grant/deny flow behind the request-review popup.
//
// Ports the legacy Inbox.jinja shell semantics onto the /ui/api inbox routes:
// Approve busy states (progress -> granted/denied / needs-manual-credentials /
// failed), fire-and-forget Deny, the file-sharing path validation (home
// expansion + within-roots), and the predefined dialog's wildcard-permission
// exclusivity. Grants/denies submit the SAME form fields to the legacy
// /requests/<id>/grant|deny routes.
//
// ONE request per open, and answering it closes: the page is opened on the
// request the reader picked -- a "Waiting on you" row, an in-chat card -- so
// the review they asked for is over when that request has a verdict, and
// swapping the next pending one in under the click that finished it would
// answer a question nobody had asked yet. The list they picked from is where
// closing takes them (see returnToPanelAfterRequest).

import type { UiPermissionGrantGroup } from "../generated/ui";
import { forgetWarmedRequestDetails, readWarmedRequestDetail, requestDetailUrl } from "./requestDetailPrefetch";

export interface InboxCard {
  id: string;
  kind_label: string;
  ws_name: string;
  display_name: string;
  accent: string;
  /** The WORKSPACE the request belongs to, not the sibling agent that filed
   * it (see UiInboxCard). Empty when it could not be resolved. */
  workspace_agent_id: string;
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
  /** Catalog service whose brand mark leads the dialog; "" when it has none. */
  service_name: string;
  /** Every offered permission, grouped: full access first, the wildcard last. */
  permission_groups: UiPermissionGrantGroup[];
  checked_permissions: string[];
  account_choices: PermissionAccountChoice[];
  selected_account_value: string;
  new_account_value: string;
  wildcard_permission: string;
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
  outcome:
    "GRANTED" | "DENIED" | "NEEDS_MANUAL_CREDENTIALS" | "FAILED" | string;
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

/** The inverse, for display: a path under the home directory reads as `~/...`. */
export function collapseSharePathHome(value: string, homeDir: string): string {
  if (!homeDir) return value;
  if (value === homeDir) return "~";
  if (value.startsWith(homeDir + "/")) return "~" + value.slice(homeDir.length);
  return value;
}

/** Case-insensitive, purely lexical at-or-beneath check mirroring the server. */
export function isSharePathWithinRoots(
  value: string,
  roots: readonly string[],
): boolean {
  if (!value) return false;
  const lower = value.toLowerCase();
  return roots.some((root) => {
    const normalized = String(root).replace(/\/+$/, "").toLowerCase() || "/";
    return lower === normalized || lower.startsWith(normalized + "/");
  });
}

/** While the wildcard permission is checked, the specific boxes are disabled.
 * They keep their own state, so unticking the wildcard restores the earlier
 * selection; `submittedPermissions` is what keeps that state off the wire. */
export function isPermissionCheckboxDisabled(
  permission: string,
  wildcardPermission: string,
  checked: ReadonlySet<string>,
): boolean {
  return permission !== wildcardPermission && checked.has(wildcardPermission);
}

/** The permissions a predefined grant actually submits. The wildcard is
 * mutually exclusive with the specific ones, and disabling a checkbox does
 * not unset it: a permission ticked BEFORE the wildcard would otherwise ride
 * along and grant more than the dialog shows. Enforced here, at the only
 * place the set leaves the client, so no click order (and no server-seeded
 * set) can defeat it. */
export function submittedPermissions(
  checked: ReadonlySet<string>,
  wildcardPermission: string,
): string[] {
  if (wildcardPermission !== "" && checked.has(wildcardPermission))
    return [wildcardPermission];
  return [...checked];
}

interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

export type RequestVerdict = "granted" | "denied";

export interface ResolvedRequest {
  requestId: string;
  /** The agent that asked, or null for the detail kinds that name none. */
  agentId: string | null;
  verdict: RequestVerdict;
}

/** Shortest gap between two attempts at the pending list once one has failed.
 * The page reconciles from the requests store on every redraw, and a failed
 * load makes itself reconcilable again (there is nothing to say the set was
 * handled), so without a floor the two close a loop: a machine whose gateway
 * is down would be asked for its list as fast as the window redraws. */
const LIST_RETRY_MS = 3000;

export interface InboxModelOptions {
  /** Injected in tests; defaults to window.fetch. */
  fetcher?: FetchLike;
  /** Injected in tests; defaults to Date.now. */
  nowMs?: () => number;
  /** Called when the request under review turns out to be gone -- resolved in
   * another window, or withdrawn by the agent -- so the list it was in can drop
   * it without waiting for its own read. No verdict: this page did not give
   * one, and nothing downstream should be told there was one. */
  onGone?: (requestId: string) => void;
  /** Called when the request under review has a verdict (or is gone), so the
   * page should dismiss. Fires at most once. */
  onClose?: () => void;
  /** Called the moment a request gets a verdict, before the page closes. */
  onResolved?: (resolved: ResolvedRequest) => void;
  /** Injected in tests; defaults to m.redraw (loaded lazily to keep the model DOM-free). */
  redraw?: () => void;
}

export class InboxModel {
  cards: InboxCard[] = [];
  /** True once a list load ATTEMPT finished (even a failed one). */
  isListLoaded = false;
  /** User-visible reason the last list load failed; null when it succeeded. */
  listErrorMessage: string | null = null;
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
  /** Ids whose deny POST is in flight, so a re-selection never lands back on one. */
  denyingIds = new Set<string>();

  // Per-detail editable state (lives here so views stay stateless).
  checkedPermissions = new Set<string>();
  selectedAccount = "";
  filePathValue = "";
  targetScope: "selected" | "all" = "selected";
  /** Whether the predefined dialog shows its full editor instead of the summary. */
  isPermissionEditorShown = false;

  /** Whether a list load is in flight, so a reconciliation that lands while
   * one is running joins it rather than starting a second. */
  private isListLoading = false;
  /** Earliest the list may be asked for again after a failure. */
  private nextListRetryAtMs = 0;
  /** The paced retry after a failure, so the promise the error notice makes
   * ("they will be retried automatically") is kept without a redraw to drive
   * it -- and cancelled with the page, so a closed one stops asking. */
  private listRetryTimer: ReturnType<typeof setTimeout> | null = null;

  private readonly options: InboxModelOptions;
  /** The pending set this model has already reflected, or null for "none yet".
   * Null rather than "" so an empty pending set is still a change worth
   * reacting to (it is exactly the set that closes the popup). */
  private lastPendingKey: string | null = null;

  constructor(options: InboxModelOptions = {}) {
    this.options = options;
  }

  private fetcher(): FetchLike {
    return (
      this.options.fetcher ??
      ((url, init) => fetch(url, { credentials: "same-origin", ...init }))
    );
  }

  private redraw(): void {
    this.options.redraw?.();
  }

  private nowMs(): number {
    return (this.options.nowMs ?? Date.now)();
  }

  /** Stop the retry timer. Called when the page closes and when it is torn
   * down, so a timer never outlives the page that wanted the list. */
  dispose(): void {
    if (this.listRetryTimer === null) return;
    clearTimeout(this.listRetryTimer);
    this.listRetryTimer = null;
  }

  /** Whether the page has already been told to dismiss. The same resolution
   * arrives twice -- once from the grant/deny that made it, and again from the
   * pending-set reconciliation that the same verdict triggers -- and the second
   * one would navigate a page that is already on its way out. */
  private isClosed = false;

  private close(): void {
    if (this.isClosed) return;
    this.isClosed = true;
    this.dispose();
    this.options.onClose?.();
  }

  /** Announce a verdict, naming the workspace the resolved request belongs to.
   *
   * Read from the CARD for `requestId`, never from the live `detail`: an
   * approval can await a browser sign-in, and anything that re-selects the
   * popup meanwhile (a second entry point picking another request) swaps
   * `detail` out from under it. Attributing the verdict to whatever was on
   * screen would post one workspace's request id into another's page. */
  private announceResolved(requestId: string, verdict: RequestVerdict): void {
    // A resolution can change what any pending request is offered (an account
    // just signed in is one the next dialog can pick), so the warms are spent
    // rather than trusted: the next open fetches afresh.
    forgetWarmedRequestDetails();
    const card = this.cards.find((entry) => entry.id === requestId) ?? null;
    const agentId =
      card !== null && card.workspace_agent_id !== ""
        ? card.workspace_agent_id
        : null;
    this.options.onResolved?.({ requestId, agentId, verdict });
  }

  async loadList(): Promise<void> {
    // One at a time. Every redraw reconciles, so without this a slow list would
    // have a second attempt started on top of it before the first came back.
    if (this.isListLoading) return;
    this.isListLoading = true;
    try {
      const response = await this.fetcher()("/ui/api/inbox");
      if (!response.ok) {
        this.markListLoadFailed();
        return;
      }
      const body = (await response.json()) as { cards: InboxCard[] };
      this.cards = body.cards;
    } catch {
      this.markListLoadFailed();
      return;
    } finally {
      this.isListLoading = false;
    }
    this.listErrorMessage = null;
    this.isListLoaded = true;
    // Prune deny markers for cards the server has dropped.
    this.denyingIds = new Set(
      [...this.denyingIds].filter((id) =>
        this.cards.some((card) => card.id === id),
      ),
    );
    this.redraw();
  }

  private markListLoadFailed(): void {
    this.listErrorMessage =
      "Could not load requests. They will be retried automatically.";
    // The attempt completed: the popup's live-refresh gate (isListLoaded)
    // must open so the store-driven reconciliation keeps running...
    this.isListLoaded = true;
    // ...and the pending-set key must be forgotten so that reconciliation
    // actually retries the load instead of seeing an unchanged set.
    this.lastPendingKey = null;
    // Paced, and driven by its own timer rather than by the redraw this
    // failure is about to cause: reconciling is what asks for the list, and a
    // failure that makes itself reconcilable again would otherwise ask for it
    // once per redraw for as long as the machine stayed down.
    this.nextListRetryAtMs = this.nowMs() + LIST_RETRY_MS;
    if (this.listRetryTimer === null && !this.isClosed) {
      this.listRetryTimer = setTimeout(() => {
        this.listRetryTimer = null;
        void this.loadList();
      }, LIST_RETRY_MS);
    }
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
    // Warmed on the way in (pointing at a "Waiting on you" row starts the
    // fetch), so the common open spends a wait that has already happened.
    const warmed = readWarmedRequestDetail(id);
    if (warmed !== null) {
      const detail = await warmed;
      // A warm that failed says nothing about why; fall through and let the
      // real fetch below report for itself.
      if (detail !== null) {
        this.isDetailLoading = false;
        this.detail = detail;
        this.seedEditableStateFromDetail(detail);
        this.redraw();
        return;
      }
    }
    const response = await this.fetcher()(requestDetailUrl(id));
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
    // Each request is reviewed from its summary; only this user's Adjust
    // click opens the editor, and never for the request that follows.
    this.isPermissionEditorShown = false;
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

  /** The account currently picked, when the detail offers a choice. */
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
      const expanded = expandSharePathHome(
        this.filePathValue.trim(),
        detail.home_dir,
      );
      return (
        expanded.length > 0 &&
        isSharePathWithinRoots(expanded, detail.allowed_roots)
      );
    }
    if (detail.kind === "accounts") return true;
    return false;
  }

  /** Reveal the predefined dialog's full permission editor. The summary is
   * the default reading of the request; only a user who wants to change the
   * grant leaves it, and `hidePermissionEditor` brings them back. */
  showPermissionEditor(): void {
    this.isPermissionEditorShown = true;
    this.redraw();
  }

  /** Leave the editor for the summary, discarding the edits made in it: the
   * way back is "back to the agent's picks", so it restores exactly the set
   * the detail arrived with rather than keeping a half-made selection. */
  hidePermissionEditor(): void {
    const detail = this.detail;
    if (detail !== null && detail.kind === "predefined") {
      this.checkedPermissions = new Set(detail.checked_permissions);
    }
    this.isPermissionEditorShown = false;
    this.redraw();
  }

  /** Non-empty but out-of-roots file path: show the instant hint (legacy parity). */
  isSharePathHintShown(): boolean {
    const detail = this.detail;
    if (detail === null || detail.kind !== "file_sharing") return false;
    const expanded = expandSharePathHome(
      this.filePathValue.trim(),
      detail.home_dir,
    );
    return (
      expanded.length > 0 &&
      !isSharePathWithinRoots(expanded, detail.allowed_roots)
    );
  }

  private buildGrantForm(): FormData {
    const form = new FormData();
    const detail = this.detail;
    if (detail === null) return form;
    if (detail.kind === "predefined") {
      for (const permission of submittedPermissions(
        this.checkedPermissions,
        detail.wildcard_permission,
      )) {
        form.append("permissions", permission);
      }
      form.append("account", this.selectedAccount);
      this.appendManualCredentialFields(form);
    } else if (detail.kind === "workspace") {
      for (const permission of this.checkedPermissions)
        form.append("permissions", permission);
      form.append("target_scope", this.targetScope);
    } else if (detail.kind === "file_sharing") {
      form.append("permissions", "file-sharing");
      form.append(
        "file_path",
        expandSharePathHome(this.filePathValue.trim(), detail.home_dir),
      );
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
        this.announceResolved(
          resolvedId,
          data.outcome === "GRANTED" ? "granted" : "denied",
        );
        this.close();
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
        this.errorMessage =
          data.message ?? "Approval failed; please try again.";
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
    this.announceResolved(resolvedId, "denied");
    // Fire-and-forget (keepalive) so the user never waits on the mngr
    // message round trip and the next-item swap starts immediately.
    void this.fetcher()(`/requests/${encodeURIComponent(resolvedId)}/deny`, {
      method: "POST",
      keepalive: true,
    }).catch(() => undefined);
    this.close();
  }

  /** Record `requestIds` as the pending set already on screen, so the first
   * live reconciliation after an open does not refetch what it just loaded. */
  markPendingSetSeen(requestIds: readonly string[]): void {
    this.lastPendingKey = requestIds.join(",");
  }

  /** React to a live pending-set change (from the requests store). */
  async refreshIfPendingChanged(requestIds: readonly string[]): Promise<void> {
    // A running approval owns the pane -- it may be waiting on a browser
    // sign-in -- and the key is left unconsumed so this reconciles once it
    // finishes.
    if (this.isApproveBusy) return;
    // A failed attempt has its own retry running; asking again before it is due
    // is the loop this paces. The key is left unconsumed, so the retry that
    // does land still reconciles this set.
    if (this.listErrorMessage !== null && this.nowMs() < this.nextListRetryAtMs) return;
    const key = requestIds.join(",");
    if (key === this.lastPendingKey) return;
    this.lastPendingKey = key;
    const selected = this.selectedId;
    // Only a request this popup HAD pending can have been resolved out from
    // under it. A selection that was never in the queue is a stale link the
    // user followed on purpose -- it is already saying the request is gone, and
    // advancing would swap an unrelated machine's live Approve/Deny form in
    // under a click aimed at dismissing it (see requestedSelection in
    // InboxPage.ts).
    const wasPending =
      selected !== null && this.cards.some((card) => card.id === selected);
    if (wasPending && !requestIds.includes(selected)) {
      // Resolved on another surface (another window, the agent giving up):
      // close rather than leave a form up that can no longer be submitted.
      // The list it came from is told first -- it is what the closing page
      // hands the reader back to, and a row for a request that is already gone
      // is exactly what it must not hand them back to.
      this.options.onGone?.(selected);
      this.close();
      return;
    }
    await this.loadList();
  }
}
