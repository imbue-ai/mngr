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
  account_display_name: string | null;
  account_profile_picture_url: string | null;
  current_account: WorkspaceOptionsAccount | null;
  accounts: WorkspaceOptionsAccount[];
  app_services: string[];
  service_labels: Record<string, string>;
  service_icons?: Record<string, string>;
  whole_service: string;
}

/** Response shape of GET /ui/api/workspaces/<id>/machine-size (ui_api_options.py). */
export interface WorkspaceMachineSizeData {
  is_available: boolean;
  memory_units: number | null;
  target_memory_units: number | null;
  disk_gb: number | null;
  target_disk_gb: number | null;
  is_restart_needed_to_apply: boolean;
}

/** "8 GB RAM · 28 GB disk" for the current size; "" when nothing is known (1 unit = 1 GiB RAM). */
export function formatMachineSize(size: WorkspaceMachineSizeData): string {
  const parts: string[] = [];
  if (size.memory_units !== null) parts.push(`${size.memory_units} GB RAM`);
  if (size.disk_gb !== null) parts.push(`${size.disk_gb} GB disk`);
  return parts.join(" · ");
}

/** The pending size a restart would apply ("16 GB RAM · 56 GB disk"), falling
 * back to the current value for the factor that has no pending target; "" when
 * no restart is needed. */
export function formatPendingMachineSize(
  size: WorkspaceMachineSizeData,
): string {
  if (!size.is_restart_needed_to_apply) return "";
  const pendingUnits = size.target_memory_units ?? size.memory_units;
  const pendingDisk = size.target_disk_gb ?? size.disk_gb;
  const parts: string[] = [];
  if (pendingUnits !== null) parts.push(`${pendingUnits} GB RAM`);
  if (pendingDisk !== null) parts.push(`${pendingDisk} GB disk`);
  return parts.join(" · ");
}

/** One user's identity record as the connector serves it (email only when verified). */
export interface IdentityRecord {
  user_id: string;
  email: string | null;
  display_name: string | null;
  profile_picture_url: string | null;
}

export interface SharingGrantList {
  /** Grantee account ids (matched first by the gateway). Absent on documents
   * written before user-id grants existed. */
  users?: string[];
  /** Invites: an address the gateway upgrades to an account id on first visit. */
  emails: string[];
  email_domains: string[];
}

export interface SharingGrantsDocument {
  workspace: SharingGrantList;
  services: Record<string, SharingGrantList>;
}

export interface MachineSharingResponse {
  enabled: boolean;
  /** The share's base URL (https://<workspace domain>/). The bare domain does
   * not route: a target's link is https://<label>.<workspace domain>/. */
  url: string | null;
  grants: SharingGrantsDocument | null;
  /** Public origin label per share target, as the backend currently knows
   * them; a target absent here has no link yet. */
  service_labels?: Record<string, string>;
  /** Identity record per granted account id the backend knows; an id absent
   * here renders as the bare id. */
  identities?: Record<string, IdentityRecord>;
}

/** One staged grantee of a share target: an account, an invited address, or a whole domain. */
export type ShareEntry =
  | { kind: "user"; userId: string }
  | { kind: "email"; email: string }
  | { kind: "domain"; domain: string };

/** A stable key for an entry (what removal and de-duplication compare). */
export function shareEntryKey(entry: ShareEntry): string {
  switch (entry.kind) {
    case "user":
      return `user:${entry.userId}`;
    case "email":
      return `email:${entry.email}`;
    case "domain":
      return `domain:${entry.domain}`;
  }
}

/** The route that resolves a typed address to an account (a 404 means "store an invite"). */
export const RESOLVE_USER_URL = "/ui/api/users/resolve";

/** Response shape of GET /api/v1/workspace-sharing/<id>/readiness. */
export interface SharingReadinessResponse {
  ready?: boolean;
  cert_not_after?: string | null;
  last_tunnel_login_at?: string | null;
  service_labels?: Record<string, string>;
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
  /** Staged grantees (owner excluded); published on enable. */
  entries: ShareEntry[];
}

export interface ShareModelOptions {
  hostId: string;
  /** The workspace id keying the sharing API; hostId is the legacy fallback. */
  agentId?: string;
  ownerEmail: string;
  ownerDisplayName: string | null;
  ownerProfilePictureUrl: string | null;
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
  /** Identity record per account id, merged from every sharing response and
   * every resolved address, so a user entry renders with a name and profile picture. */
  identities: Record<string, IdentityRecord> = {};

  private readonly options: ShareModelOptions;
  // The label per share target. Seeded from the options snapshot and then
  // merged from every sharing/readiness response, since the snapshot can
  // predate the workspace's registrations reaching the backend (right after
  // app start) and a link is only buildable once its label is known.
  private serviceLabels: Record<string, string>;
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
    this.serviceLabels = { ...options.serviceLabels };
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

  get ownerDisplayName(): string | null {
    return this.options.ownerDisplayName;
  }

  get ownerProfilePictureUrl(): string | null {
    return this.options.ownerProfilePictureUrl;
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
    entries: readonly ShareEntry[];
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

  /** The public link for a target (its label-prefixed origin), '' when it cannot be built yet.
   *
   * Never falls back to the bare machine domain or to the bare service name:
   * only `<label>.<domain>` origins route on a share, so a link without the
   * label would be one that can never work. */
  targetUrl(target: string): string {
    if (!this.machineUrl) return "";
    let host: string;
    try {
      host = new URL(this.machineUrl).host;
    } catch {
      return "";
    }
    const label = this.serviceLabels[target];
    return label ? `https://${label}.${host}/` : "";
  }

  isLabelKnown(target: string): boolean {
    return Boolean(this.serviceLabels[target]);
  }

  /** A shared target whose link is not live yet: the share is still provisioning. */
  isAwaitingLink(target: string): boolean {
    return (
      this.status === "ready" &&
      this.mutableTargetState(target).isEnabled &&
      !this.isLive &&
      this.machineUrl !== ""
    );
  }

  /** A shared target whose link cannot be shown yet because its label has not
   * reached the backend (typically right after app start). */
  isAwaitingLabel(target: string): boolean {
    return (
      this.status === "ready" &&
      this.mutableTargetState(target).isEnabled &&
      this.machineUrl !== "" &&
      !this.isLabelKnown(target)
    );
  }

  private mergeServiceLabels(labels: Record<string, string> | undefined): void {
    if (!labels) return;
    for (const [target, label] of Object.entries(labels)) {
      if (label) this.serviceLabels[target] = label;
    }
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

  /** Stage a typed grantee for the current target.
   *
   * An address is first resolved to an account so the grant survives the
   * grantee changing their email; no account (or a lookup failure) stages it
   * as an invite the gateway upgrades on the invitee's first visit. A bare
   * domain admits everyone at it. */
  async addEntry(rawEntry: string): Promise<void> {
    const trimmed = rawEntry.trim();
    if (!trimmed || trimmed === this.options.ownerEmail) return;
    // The entry belongs to the target it was typed into, even if the user
    // switches targets while the lookup is in flight.
    const target = this.currentTarget;
    const entry = trimmed.includes("@")
      ? await this.resolveAddress(trimmed)
      : ({ kind: "domain", domain: trimmed } as ShareEntry);
    if (this.isDisposed) return;
    const state = this.mutableTargetState(target);
    const key = shareEntryKey(entry);
    if (!state.entries.some((existing) => shareEntryKey(existing) === key))
      state.entries.push(entry);
    this.errorMessage = null;
    if (state.isEnabled) void this.persistEntries();
    this.redraw();
  }

  removeEntry(entry: ShareEntry): void {
    const state = this.mutableTargetState(this.currentTarget);
    const key = shareEntryKey(entry);
    state.entries = state.entries.filter(
      (existing) => shareEntryKey(existing) !== key,
    );
    if (state.isEnabled) void this.persistEntries();
    this.redraw();
  }

  /** The display record for a user entry, when the backend has told us one. */
  identityFor(userId: string): IdentityRecord | null {
    return this.identities[userId] ?? null;
  }

  private async resolveAddress(email: string): Promise<ShareEntry> {
    const result = await this.fetchJson(RESOLVE_USER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const record = result.ok
      ? (result.body as Partial<IdentityRecord> | null)
      : null;
    if (!record || typeof record.user_id !== "string" || !record.user_id)
      return { kind: "email", email };
    this.identities[record.user_id] = {
      user_id: record.user_id,
      email: typeof record.email === "string" ? record.email : null,
      display_name:
        typeof record.display_name === "string" ? record.display_name : null,
      profile_picture_url:
        typeof record.profile_picture_url === "string"
          ? record.profile_picture_url
          : null,
    };
    return { kind: "user", userId: record.user_id };
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
      workspace: { users: [], emails: [], email_domains: [] },
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
    const users: string[] = [];
    const emails = this.options.ownerEmail ? [this.options.ownerEmail] : [];
    const emailDomains: string[] = [];
    for (const entry of this.mutableTargetState(target).entries) {
      if (entry.kind === "user") {
        if (!users.includes(entry.userId)) users.push(entry.userId);
      } else if (entry.kind === "email") {
        if (!emails.includes(entry.email)) emails.push(entry.email);
      } else if (!emailDomains.includes(entry.domain)) {
        emailDomains.push(entry.domain);
      }
    }
    return { users, emails, email_domains: emailDomains };
  }

  private adoptDocument(body: unknown): void {
    const data = body as MachineSharingResponse;
    this.isMachineEnabled = Boolean(data.enabled);
    this.machineUrl = data.url ?? "";
    this.mergeServiceLabels(data.service_labels);
    for (const [userId, record] of Object.entries(data.identities ?? {}))
      this.identities[userId] = record;
    const grants = data.grants ?? {
      workspace: { users: [], emails: [], email_domains: [] },
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
        ...(scope.users ? { users: [...scope.users] } : {}),
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

  /** Keep exactly one readiness poll running while the on-screen target awaits
   * its link -- either still provisioning, or with its label not known yet (the
   * poll's responses carry the labels, so this is also how a late label lands). */
  private syncReadinessPolling(): void {
    if (
      !this.isAwaitingLink(this.currentTarget) &&
      !this.isAwaitingLabel(this.currentTarget)
    ) {
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
      ? (result.body as SharingReadinessResponse | null)
      : null;
    if (body) {
      this.mergeServiceLabels(body.service_labels);
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
    // Ready means the shell's label origin answers; the on-screen target may
    // still lack its own label (a per-app target registered later), so keep
    // polling until this target's link can actually be shown.
    if (body?.ready === true && this.isLabelKnown(target)) {
      this.markLive();
      return;
    }
    if (body?.ready === true) this.isLive = true;
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
    scope &&
    ((scope.users?.length ?? 0) > 0 ||
      scope.emails.length > 0 ||
      scope.email_domains.length > 0),
  );
}

export function scopeEntries(
  scope: SharingGrantList | undefined | null,
  ownerEmail: string,
): ShareEntry[] {
  if (!scope) return [];
  const users: ShareEntry[] = (scope.users ?? []).map((userId) => ({
    kind: "user",
    userId,
  }));
  const emails: ShareEntry[] = scope.emails
    .filter((email) => email !== ownerEmail)
    .map((email) => ({ kind: "email", email }));
  const domains: ShareEntry[] = scope.email_domains.map((domain) => ({
    kind: "domain",
    domain,
  }));
  return [...users, ...emails, ...domains];
}

export function documentGrantsAnyone(doc: SharingGrantsDocument): boolean {
  if (scopeGrantsAnyone(doc.workspace)) return true;
  return Object.values(doc.services).some((scope) => scopeGrantsAnyone(scope));
}

/** Load + action state for the Machine settings panes. */
export class WorkspaceOptionsModel {
  readonly agentId: string;
  status: "loading" | "load_failed" | "ready" = "loading";
  data: WorkspaceOptionsData | null = null;
  share: ShareModel | null = null;
  /** Read-only machine size for leased machines; null until (and unless) it loads. */
  machineSize: WorkspaceMachineSizeData | null = null;
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
      ownerDisplayName: data.account_display_name,
      ownerProfilePictureUrl: data.account_profile_picture_url,
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
    if (data.is_leased_imbue_cloud) void this.loadMachineSize();
  }

  /** Fetch the machine's read-only size lazily; failures just leave the section hidden. */
  async loadMachineSize(): Promise<void> {
    const result = await this.fetchJsonImpl(
      `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/machine-size`,
    );
    if (!result.ok) return;
    const size = result.body as WorkspaceMachineSizeData;
    if (!size.is_available) return;
    this.machineSize = size;
    this.redrawImpl();
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
