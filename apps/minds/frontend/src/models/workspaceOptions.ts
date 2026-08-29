// Model for the workspace options panel + settings page: the options-data
// load, the Machine settings actions (rename / color / account / lifecycle),
// and the Share tab's machine-sharing state machine.
//
// The share model is a simplification of the legacy workspace_options.js
// pane: same capabilities and invariants (whole-document grants writes are
// serialized; a failed status read locks the editor rather than posing as
// "not shared"; staged entries survive enable/disable; scopes granted outside
// this pane are preserved verbatim through every write), with the DOM
// juggling replaced by one explicit state object mithril renders from.

import m from "mithril";

export interface WorkspaceOptionsAccount {
  user_id: string;
  email: string;
  display_name: string | null;
}

/** Response shape of GET /ui/api/workspaces/<id>/options (ui_api_options.py). */
export interface WorkspaceOptionsData {
  agent_id: string;
  host_id: string;
  name: string;
  color: string;
  palette: Record<string, string>;
  is_stale: boolean;
  is_leased_imbue_cloud: boolean;
  has_account: boolean;
  account_email: string;
  current_account: WorkspaceOptionsAccount | null;
  accounts: WorkspaceOptionsAccount[];
  app_services: string[];
  service_labels: Record<string, string>;
  service_icons?: Record<string, string>;
  whole_service: string;
}

export interface SharingGrantList {
  emails: string[];
  email_domains: string[];
}

export interface SharingGrantsDocument {
  workspace: SharingGrantList;
  services: Record<string, SharingGrantList>;
}

export interface MachineSharingResponse {
  enabled: boolean;
  url: string | null;
  grants: SharingGrantsDocument | null;
}

/** The options panel's tabs, in the order the tab strip shows them. */
export const OPTIONS_TABS = ["permissions", "settings", "share"] as const;
export type OptionsTab = (typeof OPTIONS_TABS)[number];

/** The single ?tab parse. The options page and the titlebar's tab highlight
 * both resolve through it, so a tab the titlebar can open is never one the
 * page reads as something else. */
export function toOptionsTab(raw: string | null | undefined): OptionsTab {
  return OPTIONS_TABS.find((tab) => tab === raw) ?? "share";
}

export type SettingsGroup = "general" | "account" | "backup" | "updates";
export type ShareLoadStatus = "idle" | "loading" | "load_failed" | "ready";
export type SharePendingKind = "enable" | "disable" | "emails";

export interface FetchJson {
  (
    url: string,
    init?: RequestInit,
  ): Promise<{ ok: boolean; status: number; body: unknown }>;
}

export async function defaultFetchJson(
  url: string,
  init?: RequestInit,
): Promise<{
  ok: boolean;
  status: number;
  body: unknown;
}> {
  try {
    const response = await fetch(url, { credentials: "same-origin", ...init });
    let body: unknown = null;
    const text = await response.text();
    if (text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        body = { error: text };
      }
    }
    return { ok: response.ok, status: response.status, body };
  } catch {
    // A network-level failure must resolve (not reject): callers await this
    // inside void'd async model methods, so a rejection would be unhandled
    // and their busy flags would stick forever. Status 0 mirrors the
    // browser's "no HTTP response" convention.
    return {
      ok: false,
      status: 0,
      body: { error: "Could not reach the app server." },
    };
  }
}

export function errorMessageFromBody(body: unknown, fallback: string): string {
  if (body !== null && typeof body === "object") {
    const record = body as Record<string, unknown>;
    for (const key of ["error", "detail", "message"]) {
      const value = record[key];
      if (typeof value === "string" && value.length > 0) return value;
    }
  }
  return fallback;
}

const READINESS_FAST_INTERVAL_MS = 2000;
const READINESS_SLOW_INTERVAL_MS = 5000;
const READINESS_FAST_PHASE_MS = 30_000;
const READINESS_DEADLINE_MS = 5 * 60_000;

interface ShareTargetState {
  isEnabled: boolean;
  /** Staged emails/domains (owner excluded); published on enable. */
  entries: string[];
}

export interface ShareModelOptions {
  hostId: string;
  /** The workspace id keying the sharing API; hostId is the legacy fallback. */
  agentId?: string;
  ownerEmail: string;
  wholeService: string;
  appServices: string[];
  serviceLabels: Record<string, string>;
  /** Registered SVG icon markup per app service (server-gated; the view
   * sanitizes again before inlining). Absent = no icon registered. */
  serviceIcons?: Record<string, string>;
  fetchJson?: FetchJson;
  redraw?: () => void;
  /** Injected timer hooks so tests drive the readiness poll deterministically. */
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (timerId: number) => void;
  monotonicNowMs?: () => number;
}

/** The Share tab's machine-sharing state machine (one share per machine). */
export class ShareModel {
  status: ShareLoadStatus = "idle";
  isMachineEnabled = false;
  machineUrl = "";
  isLive = false;
  /** Provisioning-step signals while awaiting the link (machine-level; reset
   * only when a fresh provisioning wait begins, see beginProvisioningWait). */
  isCertIssued = false;
  isTunnelConnected = false;
  currentTarget: string;
  errorMessage: string | null = null;
  isRetryOffered = false;

  private readonly options: ShareModelOptions;
  private readonly stateByTarget = new Map<string, ShareTargetState>();
  private extraServiceGrants: Record<string, SharingGrantList> = {};
  private pendingKindByTarget = new Map<string, SharePendingKind>();
  private writeChain: Promise<unknown> = Promise.resolve();
  private readinessTimerId: number | null = null;
  private pollingTarget: string | null = null;
  private pollStartedAtMs = 0;
  // The tunnel-login stamp seen on the first readiness poll of this
  // provisioning wait (machine-level; it survives poll restarts from target
  // switches, see beginProvisioningWait). The stamp survives re-shares, so
  // "tunnel connected" means the value CHANGED (or appeared) since then --
  // not merely that one exists.
  // undefined = no poll response seen yet this wait.
  private tunnelLoginAtSnapshot: string | null | undefined = undefined;
  private isDisposed = false;

  constructor(options: ShareModelOptions) {
    this.options = options;
    this.currentTarget = options.wholeService;
  }

  get knownTargets(): string[] {
    const targets = [...this.options.appServices];
    if (!targets.includes(this.options.wholeService))
      targets.push(this.options.wholeService);
    return targets;
  }

  get wholeService(): string {
    return this.options.wholeService;
  }

  get ownerEmail(): string {
    return this.options.ownerEmail;
  }

  /** The target's registered SVG icon markup, '' when it has none. */
  targetIcon(target: string): string {
    return this.options.serviceIcons?.[target] ?? "";
  }

  selectTarget(target: string): void {
    if (!this.knownTargets.includes(target)) target = this.options.wholeService;
    if (target === this.currentTarget) {
      if (this.status === "load_failed") this.load();
      return;
    }
    this.currentTarget = target;
    this.errorMessage = null;
    this.isRetryOffered = false;
    if (this.status === "load_failed") this.load();
    this.syncReadinessPolling();
  }

  targetState(target: string): {
    isEnabled: boolean;
    entries: readonly string[];
  } {
    return this.mutableTargetState(target);
  }

  pendingKind(target: string): SharePendingKind | null {
    return this.pendingKindByTarget.get(target) ?? null;
  }

  get isEditorEditable(): boolean {
    return (
      this.status === "ready" &&
      this.pendingKindByTarget.get(this.currentTarget) === undefined
    );
  }

  /** The public link for a target (label-prefixed service origin), '' when unknown. */
  targetUrl(target: string): string {
    if (!this.machineUrl) return "";
    let host: string;
    try {
      host = new URL(this.machineUrl).host;
    } catch {
      return "";
    }
    const label = this.options.serviceLabels[target];
    if (target === this.options.wholeService) {
      return label ? `https://${label}.${host}/` : this.machineUrl;
    }
    return `https://${label ?? target}.${host}/`;
  }

  isAwaitingLink(target: string): boolean {
    return (
      this.status === "ready" &&
      this.mutableTargetState(target).isEnabled &&
      !this.isLive &&
      this.machineUrl !== ""
    );
  }

  async load(): Promise<void> {
    this.status = "loading";
    this.errorMessage = null;
    this.isRetryOffered = false;
    this.redraw();
    const result = await this.fetchJson(this.shareApiBase());
    if (this.isDisposed) return;
    if (!result.ok) {
      this.status = "load_failed";
      this.errorMessage =
        "Could not load sharing status: " +
        errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.isRetryOffered = true;
      this.redraw();
      return;
    }
    const data = result.body as MachineSharingResponse;
    if (data.enabled && data.grants === null) {
      // The share exists but the grants read never landed: a failed read,
      // not an empty policy -- editing against it could replace grants
      // nobody ever saw.
      this.status = "load_failed";
      this.errorMessage =
        "This machine is shared and everyone granted access still has it, but the list of who that is " +
        "could not be loaded, so it cannot be edited right now.";
      this.isRetryOffered = true;
      this.redraw();
      return;
    }
    this.adoptDocument(data);
    // An already-published link is assumed live; the provisioning wait only
    // applies to shares created in this session.
    this.isLive = this.isMachineEnabled;
    this.status = "ready";
    this.syncReadinessPolling();
    this.redraw();
  }

  addEntry(rawEntry: string): void {
    const entry = rawEntry.trim();
    if (!entry || entry === this.options.ownerEmail) return;
    const state = this.mutableTargetState(this.currentTarget);
    if (!state.entries.includes(entry)) state.entries.push(entry);
    this.errorMessage = null;
    if (state.isEnabled) void this.persistEntries();
    this.redraw();
  }

  removeEntry(entry: string): void {
    const state = this.mutableTargetState(this.currentTarget);
    state.entries = state.entries.filter((existing) => existing !== entry);
    if (state.isEnabled) void this.persistEntries();
    this.redraw();
  }

  /** Enable sharing for the current target. `residualInputText` blocks the
   * publish when the add-box still holds un-added text (never drop it silently). */
  async enable(residualInputText: string): Promise<void> {
    const residual = residualInputText.trim();
    if (residual) {
      this.errorMessage = `Either click 'Add' to share with ${residual}, or clear the box first.`;
      this.redraw();
      return;
    }
    this.errorMessage = null;
    const target = this.currentTarget;
    this.startPending(target, "enable");
    const wasMachineEnabled = this.isMachineEnabled;
    try {
      const body = await this.enqueueWrite(() =>
        this.putGrants(this.buildGrantsDocument({ [target]: true })),
      );
      this.adoptDocument(body);
      if (!wasMachineEnabled) this.beginProvisioningWait();
    } catch (error) {
      this.errorMessage =
        "Could not enable sharing: " +
        (error instanceof Error ? error.message : String(error));
    } finally {
      this.endPending(target);
    }
  }

  async disable(): Promise<void> {
    this.errorMessage = null;
    const target = this.currentTarget;
    this.startPending(target, "disable");
    try {
      const remaining = this.buildGrantsDocument({ [target]: false });
      if (!documentGrantsAnyone(remaining)) {
        await this.enqueueWrite(async () => {
          const result = await this.fetchJson(this.shareApiBase(), {
            method: "DELETE",
          });
          if (!result.ok)
            throw new Error(
              errorMessageFromBody(result.body, `HTTP ${result.status}`),
            );
          return null;
        });
        this.isMachineEnabled = false;
        this.machineUrl = "";
        this.beginProvisioningWait();
        for (const knownTarget of this.knownTargets)
          this.mutableTargetState(knownTarget).isEnabled = false;
        this.extraServiceGrants = {};
        this.status = "ready";
      } else {
        const body = await this.enqueueWrite(() => this.putGrants(remaining));
        this.adoptDocument(body);
      }
      this.mutableTargetState(target).isEnabled = false;
    } catch (error) {
      this.errorMessage =
        "Could not disable sharing: " +
        (error instanceof Error ? error.message : String(error));
    } finally {
      this.endPending(target);
    }
  }

  /** Build the grants document as it should be after a write. Exposed for tests. */
  buildGrantsDocument(
    overrides: Record<string, boolean> = {},
  ): SharingGrantsDocument {
    const isOn = (target: string): boolean =>
      Object.prototype.hasOwnProperty.call(overrides, target)
        ? overrides[target]
        : this.mutableTargetState(target).isEnabled;
    const doc: SharingGrantsDocument = {
      workspace: { emails: [], email_domains: [] },
      services: {},
    };
    for (const target of this.knownTargets) {
      if (!isOn(target)) continue;
      const grantList = this.grantListFor(target);
      if (target === this.options.wholeService) doc.workspace = grantList;
      else doc.services[target] = grantList;
    }
    for (const [name, scope] of Object.entries(this.extraServiceGrants)) {
      doc.services[name] = scope;
    }
    return doc;
  }

  dispose(): void {
    this.isDisposed = true;
    this.stopReadinessPolling();
  }

  private redraw(): void {
    (this.options.redraw ?? m.redraw)();
  }

  private fetchJson(url: string, init?: RequestInit): ReturnType<FetchJson> {
    return (this.options.fetchJson ?? defaultFetchJson)(url, init);
  }

  private shareApiBase(): string {
    return `/api/v1/workspace-sharing/${encodeURIComponent(this.options.agentId ?? this.options.hostId)}`;
  }

  private mutableTargetState(target: string): ShareTargetState {
    let state = this.stateByTarget.get(target);
    if (state === undefined) {
      state = { isEnabled: false, entries: [] };
      this.stateByTarget.set(target, state);
    }
    return state;
  }

  private grantListFor(target: string): SharingGrantList {
    const emails = this.options.ownerEmail ? [this.options.ownerEmail] : [];
    const emailDomains: string[] = [];
    for (const entry of this.mutableTargetState(target).entries) {
      if (entry.includes("@")) {
        if (!emails.includes(entry)) emails.push(entry);
      } else if (!emailDomains.includes(entry)) {
        emailDomains.push(entry);
      }
    }
    return { emails, email_domains: emailDomains };
  }

  private adoptDocument(body: unknown): void {
    const data = body as MachineSharingResponse;
    this.isMachineEnabled = Boolean(data.enabled);
    this.machineUrl = data.url ?? "";
    const grants = data.grants ?? {
      workspace: { emails: [], email_domains: [] },
      services: {},
    };
    const services = grants.services ?? {};
    for (const target of this.knownTargets) {
      const scope =
        target === this.options.wholeService
          ? grants.workspace
          : services[target];
      const state = this.mutableTargetState(target);
      if (scopeGrantsAnyone(scope)) {
        state.isEnabled = true;
        state.entries = scopeEntries(scope, this.options.ownerEmail);
      } else {
        state.isEnabled = false;
      }
    }
    this.extraServiceGrants = {};
    for (const [name, scope] of Object.entries(services)) {
      if (this.knownTargets.includes(name)) continue;
      if (!scopeGrantsAnyone(scope)) continue;
      this.extraServiceGrants[name] = {
        emails: [...scope.emails],
        email_domains: [...scope.email_domains],
      };
    }
    this.status = "ready";
    this.syncReadinessPolling();
  }

  private async putGrants(doc: SharingGrantsDocument): Promise<unknown> {
    const result = await this.fetchJson(this.shareApiBase(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    });
    if (!result.ok)
      throw new Error(
        errorMessageFromBody(result.body, `HTTP ${result.status}`),
      );
    return result.body;
  }

  private async persistEntries(): Promise<void> {
    const target = this.currentTarget;
    this.startPending(target, "emails");
    try {
      const body = await this.enqueueWrite(() =>
        this.putGrants(this.buildGrantsDocument()),
      );
      this.adoptDocument(body);
    } catch (error) {
      this.errorMessage =
        "Could not update who this is shared with: " +
        (error instanceof Error ? error.message : String(error));
    } finally {
      this.endPending(target);
    }
  }

  /** Serialize whole-document writes: each builds its body only at its turn. */
  private enqueueWrite<T>(makeRequest: () => Promise<T>): Promise<T> {
    const result = this.writeChain.then(makeRequest, makeRequest);
    this.writeChain = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private startPending(target: string, kind: SharePendingKind): void {
    this.pendingKindByTarget.set(target, kind);
    this.redraw();
  }

  private endPending(target: string): void {
    this.pendingKindByTarget.delete(target);
    this.syncReadinessPolling();
    this.redraw();
  }

  private setTimer(callback: () => void, delayMs: number): number {
    return (this.options.setTimer ?? ((cb, ms) => window.setTimeout(cb, ms)))(
      callback,
      delayMs,
    );
  }

  private clearTimer(timerId: number): void {
    (this.options.clearTimer ?? ((id) => window.clearTimeout(id)))(timerId);
  }

  private nowMs(): number {
    return (this.options.monotonicNowMs ?? (() => performance.now()))();
  }

  private stopReadinessPolling(): void {
    if (this.readinessTimerId !== null) {
      this.clearTimer(this.readinessTimerId);
      this.readinessTimerId = null;
    }
    this.pollingTarget = null;
  }

  /** Start a fresh provisioning wait: nothing is live and no step is done yet.
   *
   * The step flags and the tunnel-stamp snapshot are MACHINE-level (one share
   * per machine), so they are reset only here -- never on a poll restart. A
   * target switch mid-wait restarts polling, and re-baselining the snapshot
   * there could swallow a tunnel reconnect that already happened.
   */
  private beginProvisioningWait(): void {
    this.isLive = false;
    this.isCertIssued = false;
    this.isTunnelConnected = false;
    this.tunnelLoginAtSnapshot = undefined;
  }

  /** Keep exactly one readiness poll running while the on-screen target awaits its link. */
  private syncReadinessPolling(): void {
    if (!this.isAwaitingLink(this.currentTarget)) {
      this.stopReadinessPolling();
      return;
    }
    if (this.pollingTarget === this.currentTarget) return;
    this.stopReadinessPolling();
    if (this.isDisposed) return;
    this.pollingTarget = this.currentTarget;
    this.pollStartedAtMs = this.nowMs();
    this.scheduleReadinessProbe(this.pollingTarget, 0);
  }

  private scheduleReadinessProbe(target: string, elapsedMs: number): void {
    if (this.isDisposed || this.pollingTarget !== target) return;
    const interval =
      elapsedMs < READINESS_FAST_PHASE_MS
        ? READINESS_FAST_INTERVAL_MS
        : READINESS_SLOW_INTERVAL_MS;
    this.readinessTimerId = this.setTimer(() => {
      void this.probeReadiness(target);
    }, interval);
  }

  private async probeReadiness(target: string): Promise<void> {
    this.readinessTimerId = null;
    if (this.isDisposed || this.pollingTarget !== target) return;
    const elapsed = this.nowMs() - this.pollStartedAtMs;
    if (elapsed > READINESS_DEADLINE_MS) {
      // Stop warning at the deadline rather than pretending failure forever.
      this.markLive();
      return;
    }
    const result = await this.fetchJson(`${this.shareApiBase()}/readiness`);
    if (this.isDisposed || this.pollingTarget !== target) return;
    const body = result.ok
      ? (result.body as {
          ready?: boolean;
          cert_not_after?: string | null;
          last_tunnel_login_at?: string | null;
        } | null)
      : null;
    if (body) {
      if (body.cert_not_after != null) this.isCertIssued = true;
      const tunnelStamp = body.last_tunnel_login_at ?? null;
      if (this.tunnelLoginAtSnapshot === undefined) {
        this.tunnelLoginAtSnapshot = tunnelStamp;
      } else if (
        tunnelStamp !== null &&
        tunnelStamp !== this.tunnelLoginAtSnapshot
      ) {
        this.isTunnelConnected = true;
      }
    }
    if (body?.ready === true) {
      this.markLive();
      return;
    }
    this.redraw();
    this.scheduleReadinessProbe(target, this.nowMs() - this.pollStartedAtMs);
  }

  private markLive(): void {
    this.isLive = true;
    this.isCertIssued = true;
    this.isTunnelConnected = true;
    this.stopReadinessPolling();
    this.redraw();
  }
}

export function scopeGrantsAnyone(
  scope: SharingGrantList | undefined | null,
): boolean {
  return Boolean(
    scope && (scope.emails.length > 0 || scope.email_domains.length > 0),
  );
}

export function scopeEntries(
  scope: SharingGrantList | undefined | null,
  ownerEmail: string,
): string[] {
  if (!scope) return [];
  const entries = scope.emails.filter((email) => email !== ownerEmail);
  return [...entries, ...scope.email_domains];
}

export function documentGrantsAnyone(doc: SharingGrantsDocument): boolean {
  if (doc.workspace.emails.length > 0 || doc.workspace.email_domains.length > 0)
    return true;
  return Object.values(doc.services).some((scope) => scopeGrantsAnyone(scope));
}

/** Load + action state for the Machine settings panes. */
export class WorkspaceOptionsModel {
  readonly agentId: string;
  status: "loading" | "load_failed" | "ready" = "loading";
  data: WorkspaceOptionsData | null = null;
  share: ShareModel | null = null;
  loadErrorMessage = "";

  renameErrorMessage = "";
  isRenameSaving = false;
  colorErrorMessage = "";
  isColorSaving = false;
  lastSavedColor = "";
  accountErrorMessage = "";
  isAccountBusy = false;
  destroyErrorMessage = "";
  isDestroyPending = false;
  lifecycleErrorMessage = "";
  isLifecycleBusy = false;

  private readonly fetchJsonImpl: FetchJson;
  private readonly redrawImpl: () => void;
  private readonly shareOverrides: Partial<ShareModelOptions>;

  constructor(
    agentId: string,
    dependencies: {
      fetchJson?: FetchJson;
      redraw?: () => void;
      shareOverrides?: Partial<ShareModelOptions>;
    } = {},
  ) {
    this.agentId = agentId;
    this.fetchJsonImpl = dependencies.fetchJson ?? defaultFetchJson;
    this.redrawImpl = dependencies.redraw ?? m.redraw;
    this.shareOverrides = dependencies.shareOverrides ?? {};
  }

  async load(): Promise<void> {
    this.status = "loading";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/options`,
    );
    if (!result.ok) {
      this.status = "load_failed";
      this.loadErrorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return;
    }
    const data = result.body as WorkspaceOptionsData;
    this.data = data;
    this.lastSavedColor = data.color;
    this.share?.dispose();
    this.share = new ShareModel({
      hostId: data.host_id || data.agent_id,
      agentId: data.agent_id || this.agentId,
      ownerEmail: data.account_email,
      wholeService: data.whole_service,
      appServices: data.app_services,
      serviceLabels: data.service_labels,
      serviceIcons: data.service_icons,
      fetchJson: this.fetchJsonImpl,
      redraw: this.redrawImpl,
      ...this.shareOverrides,
    });
    this.status = "ready";
    this.redrawImpl();
    void this.share.load();
  }

  dispose(): void {
    this.share?.dispose();
  }

  async rename(newName: string): Promise<boolean> {
    const trimmed = newName.trim();
    if (!trimmed) {
      this.renameErrorMessage = "A machine name is required.";
      this.redrawImpl();
      return false;
    }
    this.renameErrorMessage = "";
    this.isRenameSaving = true;
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/rename`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      },
    );
    this.isRenameSaving = false;
    if (!result.ok) {
      this.renameErrorMessage = errorMessageFromBody(
        result.body,
        `Rename failed (HTTP ${result.status})`,
      );
      this.redrawImpl();
      return false;
    }
    if (this.data) this.data = { ...this.data, name: trimmed };
    this.redrawImpl();
    return true;
  }

  async saveColor(
    normalizedHex: string,
    previewAccent: (hex: string) => void,
  ): Promise<boolean> {
    if (normalizedHex === this.lastSavedColor) return true;
    previewAccent(normalizedHex);
    this.isColorSaving = true;
    this.colorErrorMessage = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ color: normalizedHex }),
      },
    );
    this.isColorSaving = false;
    if (result.ok) {
      this.lastSavedColor = normalizedHex;
      if (this.data) this.data = { ...this.data, color: normalizedHex };
      this.redrawImpl();
      return true;
    }
    this.colorErrorMessage = colorErrorMessageFor(result.status, result.body);
    // Revert the optimistic paint to the persisted color.
    previewAccent(this.lastSavedColor);
    this.redrawImpl();
    return false;
  }

  async setAccount(accountId: string | null): Promise<boolean> {
    this.isAccountBusy = true;
    this.accountErrorMessage = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: accountId }),
      },
    );
    this.isAccountBusy = false;
    if (!result.ok) {
      this.accountErrorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return false;
    }
    // The association changes the options payload wholesale (owner email
    // drives the share pane); reload rather than patching locally.
    await this.load();
    return true;
  }

  async destroy(): Promise<boolean> {
    this.isDestroyPending = true;
    this.destroyErrorMessage = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/destroy`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    );
    this.isDestroyPending = false;
    if (!result.ok) {
      this.destroyErrorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return false;
    }
    return true;
  }

  async setLifecycle(action: "start" | "stop"): Promise<boolean> {
    this.isLifecycleBusy = true;
    this.lifecycleErrorMessage = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/${action}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    );
    this.isLifecycleBusy = false;
    if (!result.ok) {
      this.lifecycleErrorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return false;
    }
    this.redrawImpl();
    return true;
  }
}

export function colorErrorMessageFor(status: number, body: unknown): string {
  const code = errorMessageFromBody(body, "");
  switch (code) {
    case "invalid_hex":
      return "That hex value is not valid. Use #rrggbb or #rgb.";
    case "not_primary":
      return "This agent isn't a primary machine; color can't be set.";
    case "stale_provider":
      return "This machine is currently unreachable; try again later.";
    case "host_unreachable":
      return "Could not reach the machine host. Try again in a moment.";
    default:
      return code || `Save failed (HTTP ${status}).`;
  }
}

/** Lenient hex normalizer, mirroring workspace_color.normalize_workspace_color. */
export function normalizeWorkspaceColorHex(raw: string): string | null {
  const trimmed = raw.trim().toLowerCase();
  const withHash = trimmed.startsWith("#") ? trimmed : `#${trimmed}`;
  const shortMatch = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/.exec(withHash);
  if (shortMatch) {
    return `#${shortMatch[1]}${shortMatch[1]}${shortMatch[2]}${shortMatch[2]}${shortMatch[3]}${shortMatch[3]}`;
  }
  return /^#[0-9a-f]{6}$/.test(withHash) ? withHash : null;
}
