// The share panel's model: publication, the grants per target, and the
// coalesced writes that save them. Every edit lands here at once and its write
// runs behind it; one write is in flight per workspace, and each answers with
// the status document, read back over the edits that have not landed yet.

import m from "mithril";
import type {
  FetchJson,
  IdentityRecord,
  MachineSharingResponse,
  ShareLoadStatus,
  SharingGrantList,
  SharingGrantsDocument,
  SharingReadinessResponse,
} from "./workspaceOptions";
import {
  RESOLVE_USER_URL,
  defaultFetchJson,
  errorMessageFromBody,
} from "./workspaceOptions";

// Mirrors share_grant_validation.py, which the grants route runs over the same
// entries.
const DOMAIN_LABEL = String.raw`[\p{L}\p{N}](?:[\p{L}\p{N}]|-)*(?<!-)`;
const DOMAIN_PATTERN = new RegExp(
  `^${DOMAIN_LABEL}(?:\\.${DOMAIN_LABEL})*\\.\\p{L}{2,}$`,
  "u",
);
const EMAIL_LOCAL_PART_PATTERN = /^[^@\s]+$/;

const READINESS_FAST_INTERVAL_MS = 2000;
const READINESS_SLOW_INTERVAL_MS = 5000;
const READINESS_FAST_PHASE_MS = 30_000;
const READINESS_DEADLINE_MS = 5 * 60_000;

/** What a grant can be typed as, in the order the add row offers them. */
export const GRANT_ADD_KINDS = ["email", "email_domain"] as const;

export type GrantAddKind = (typeof GRANT_ADD_KINDS)[number];

export type GrantKind = GrantAddKind | "user";

/** The kind a control names, read as one of the panel's own. */
export function toGrantAddKind(raw: string): GrantAddKind {
  return GRANT_ADD_KINDS.find((kind) => kind === raw) ?? "email";
}

/** Who a grant admits, in a document entry and in a row alike. */
export type Grantee =
  | { readonly kind: "email"; readonly value: string }
  | { readonly kind: "email_domain"; readonly value: string }
  | {
      readonly kind: "user";
      readonly userId: string;
      readonly value: string | null;
    };

export type GrantOperation = "add" | "remove";

export type GrantStatus =
  | { readonly state: "settled" }
  | { readonly state: "saving" }
  | {
      readonly state: "failed";
      readonly failedOperation: GrantOperation;
      readonly failureMessage: string;
    };

/** A row's name for itself, minted from the grantee it was created for. */
export type GrantKey = string & { readonly __brand: "GrantKey" };

export type PublishWrite =
  | { readonly state: "idle" }
  | { readonly state: "publishing" }
  | { readonly state: "unpublishing" }
  | { readonly state: "failed"; readonly message: string };

/** The workspace gateway's own bring-up report while the link is not live. */
const GATEWAY_STATES = ["up", "retrying", "halted"] as const;

export type GatewayState = (typeof GATEWAY_STATES)[number];

export type TimerHandle = number;

/** The ticket a status request carries, so the answers can be ordered. */
type StatusSequence = number & { readonly __brand: "StatusSequence" };

type TunnelLoginSnapshot =
  | { readonly state: "unseen" }
  | { readonly state: "seen"; readonly stamp: string | null };

export interface ProvisioningStep {
  label: string;
  isDone: boolean;
}

export interface Grant {
  readonly key: GrantKey;
  grantee: Grantee;
  status: GrantStatus;
  /** When this panel added the row, by the panel's clock. */
  readonly addedAtMs: number | null;
}

export interface GrantAddRow {
  kind: GrantAddKind;
  value: string;
  refusalMessage: string | null;
}

/** One entry the grants route refused, as the 400 body names it. */
interface GrantRefusal {
  scope: string;
  kind: string;
  value: string;
  message: string;
}

/** The scope a refusal names for the workspace-level lists (share_grant_validation.py). */
const WORKSPACE_GRANT_SCOPE = "workspace";

const GRANT_SAVE_FAILURE_MESSAGE = "Could not save";

export interface SharePanelModelOptions {
  hostId: string;
  /** The workspace id keying the sharing API; hostId is the legacy fallback. */
  agentId?: string;
  /** The granter's own address, which may never be granted. */
  granterEmail: string;
  wholeService: string;
  appServices: string[];
  serviceLabels: Record<string, string>;
  /** Registered SVG icon markup per app service. */
  serviceIcons?: Record<string, string>;
  /** The mail providers a domain grant may never name. */
  publicEmailDomains: string[];
  fetchJson?: FetchJson;
  redraw?: () => void;
  /** Injected timer hooks so tests drive the readiness poll deterministically. */
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (timerId: number) => void;
  monotonicNowMs?: () => number;
}

/** A row taken out of the list whose removal has not been saved yet. */
interface PendingRemoval {
  grant: Grant;
  idx: number;
}

export class SharePanelModel {
  loadStatus: ShareLoadStatus = "idle";
  loadErrorMessage: string | null = null;
  /** Flipped the moment the switch is thrown, before the route answers. */
  isPublished = false;
  publishWrite: PublishWrite = { state: "idle" };
  machineUrl: string | null = null;
  isLive = false;
  isCertIssued = false;
  isTunnelConnected = false;
  /** "halted" clears only by publishing again. */
  gatewayState: GatewayState | null = null;
  gatewayError: string | null = null;
  gatewayFailedAttemptCount = 0;
  gatewayNextRetryAt: string | null = null;
  /** The domain the share lived at before this session's load moved it to the
   * tier's current address; null when the share did not move. */
  migratedDomainFrom: string | null = null;
  currentTarget: string;
  /** Merged from every sharing response and every resolved address. */
  identities: Record<string, IdentityRecord> = {};
  /** The rows of each target: the whole workspace under its service name,
   * every other target under its app name. */
  readonly grantsByTarget = new Map<string, Grant[]>();

  private readonly options: SharePanelModelOptions;
  // Seeded from the options snapshot, then merged from every response, since
  // the snapshot can predate the workspace's registrations.
  private serviceLabels: Record<string, string>;
  private readonly publicEmailDomains: Set<string>;
  private readonly addRowByTarget = new Map<string, GrantAddRow>();
  private readonly removalsByTarget = new Map<string, PendingRemoval[]>();
  // Scopes of services this panel has no target for, carried verbatim through
  // every write so a whole-document replace never drops them.
  private extraServiceGrants: Record<string, SharingGrantList> = {};
  // A write built before the first read would revoke everything the workspace
  // admits on.
  private isGrantsListKnown = false;
  private isGrantsWriteInFlight = false;
  private isGrantsWriteDirty = false;
  // Status requests are numbered as sent, so an older one that answers later
  // cannot overwrite a newer document.
  private statusRequestCount = 0;
  private adoptedStatusSequence: StatusSequence | null = null;
  // Bumped by every publish and unpublish, so the answer to the call the
  // switch is waiting on is the only one that moves it.
  private publishGeneration = 0;
  private readinessTimerId: number | null = null;
  private pollingTarget: string | null = null;
  // Bumped whenever polling stops, so a probe whose round trip outlived its
  // poll schedules nothing.
  private pollGeneration = 0;
  private pollStartedAtMs = 0;
  // The tunnel-login stamp at the first poll of this wait: "connected to the
  // relay" means it changed since, because the stamp survives re-publishing.
  private tunnelLoginSnapshot: TunnelLoginSnapshot = { state: "unseen" };
  private isDisposed = false;

  constructor(options: SharePanelModelOptions) {
    this.options = options;
    this.serviceLabels = { ...options.serviceLabels };
    this.publicEmailDomains = new Set(
      options.publicEmailDomains.map(normalizeGrantDomain),
    );
    this.currentTarget = options.wholeService;
  }

  get wholeService(): string {
    return this.options.wholeService;
  }

  get appTargets(): string[] {
    return this.options.appServices.filter(
      (target) => target !== this.options.wholeService,
    );
  }

  /** The whole workspace first, then each app. */
  get knownTargets(): string[] {
    return [this.options.wholeService, ...this.appTargets];
  }

  get granterEmail(): string {
    return this.options.granterEmail;
  }

  targetIcon(target: string): string | null {
    return this.options.serviceIcons?.[target] ?? null;
  }

  /** Whether a grant can be made: the workspace has an address to admit anyone
   * to, and the list it is admitting on is known. */
  get canAdd(): boolean {
    return this.isPublished && this.isGrantsListKnown;
  }

  get isAwaitingLink(): boolean {
    return this.isPublished && !this.isLive && this.machineUrl !== null;
  }

  /** A target whose link cannot be shown yet because its label has not reached
   * the backend (typically right after app start). */
  isAwaitingLabel(target: string): boolean {
    return (
      this.isPublished && this.machineUrl !== null && !this.isLabelKnown(target)
    );
  }

  private get isPublishWriteInFlight(): boolean {
    return (
      this.publishWrite.state === "publishing" ||
      this.publishWrite.state === "unpublishing"
    );
  }

  get isProvisioningHalted(): boolean {
    return this.gatewayState === "halted";
  }

  /** The bring-up in order, each step named as the work it is doing. */
  get provisioningSteps(): ProvisioningStep[] {
    return [
      { label: "Creating link", isDone: this.machineUrl !== null },
      { label: "Setting up encryption", isDone: this.isCertIssued },
      { label: "Connecting to the relay", isDone: this.isTunnelConnected },
      { label: "Verifying end to end", isDone: this.isLive },
    ];
  }

  /** The step under way: the first not yet done, or the last once all are, so
   * there is always one to name for as long as the bring-up is shown. */
  get activeProvisioningStep(): ProvisioningStep {
    const steps = this.provisioningSteps;
    return steps.find((step) => !step.isDone) ?? steps[steps.length - 1];
  }

  selectTarget(target: string): void {
    if (!this.knownTargets.includes(target)) target = this.options.wholeService;
    if (target === this.currentTarget) {
      if (this.loadStatus === "load_failed") void this.load();
      return;
    }
    this.currentTarget = target;
    if (this.loadStatus === "load_failed") void this.load();
    this.syncReadinessPolling();
  }

  /** Give the workspace an address on the internet. Admits nobody by itself. */
  async publish(): Promise<void> {
    const generation = ++this.publishGeneration;
    this.publishWrite = { state: "publishing" };
    this.isPublished = true;
    this.beginProvisioningWait();
    this.redraw();
    const sequence = this.nextStatusSequence();
    const result = await this.fetchJson(this.shareApiBase(), { method: "PUT" });
    if (this.isDisposed || generation !== this.publishGeneration) return;
    if (!result.ok) {
      this.isPublished = false;
      this.publishWrite = {
        state: "failed",
        message: errorMessageFromBody(result.body, `HTTP ${result.status}`),
      };
    } else {
      this.publishWrite = { state: "idle" };
      this.adoptStatusDocument(result.body, sequence);
      // The route that created the link is the authority on it, even when a
      // write served after it has already brought the grants forward.
      this.adoptPublication(result.body as MachineSharingResponse);
    }
    this.syncReadinessPolling();
    this.redraw();
  }

  /** Take the workspace's address away, keeping every grant it was admitting on. */
  async unpublish(): Promise<void> {
    const generation = ++this.publishGeneration;
    this.publishWrite = { state: "unpublishing" };
    this.isPublished = false;
    this.redraw();
    const sequence = this.nextStatusSequence();
    const result = await this.fetchJson(this.shareApiBase(), {
      method: "DELETE",
    });
    if (this.isDisposed || generation !== this.publishGeneration) return;
    if (!result.ok) {
      this.isPublished = true;
      this.publishWrite = {
        state: "failed",
        message: errorMessageFromBody(result.body, `HTTP ${result.status}`),
      };
    } else {
      this.publishWrite = { state: "idle" };
      this.adoptStatusDocument(result.body, sequence);
      this.adoptPublication(result.body as MachineSharingResponse);
    }
    this.syncReadinessPolling();
    this.redraw();
  }

  grantsFor(target: string): readonly Grant[] {
    return this.mutableGrants(target);
  }

  grantCount(target: string): number {
    return this.mutableGrants(target).length;
  }

  /** What the whole workspace's grants admit on top of an app's own list. */
  inheritedGrants(): readonly Grant[] {
    return this.mutableGrants(this.options.wholeService);
  }

  /** The add row of a target, created empty on first use. */
  addRow(target: string): GrantAddRow {
    let row = this.addRowByTarget.get(target);
    if (row === undefined) {
      row = { kind: "email", value: "", refusalMessage: null };
      this.addRowByTarget.set(target, row);
    }
    return row;
  }

  identityFor(userId: string): IdentityRecord | null {
    return this.identities[userId] ?? null;
  }

  /** Only `<label>.<domain>` origins route, so there is no fallback to the
   * bare domain. */
  targetUrl(target: string): string | null {
    if (this.machineUrl === null) return null;
    let host: string;
    try {
      host = new URL(this.machineUrl).host;
    } catch {
      return null;
    }
    const label = this.serviceLabels[target];
    return label ? `https://${label}.${host}/` : null;
  }

  /** An app that has not registered an address yet has no link to show. */
  hasLink(target: string): boolean {
    return this.targetUrl(target) !== null;
  }

  isLabelKnown(target: string): boolean {
    return Boolean(this.serviceLabels[target]);
  }

  /**
   * On the panel's clock rather than the window's, so a view's waits are as
   * testable as the model's.
   */
  schedule(callback: () => void, delayMs: number): TimerHandle {
    return (this.options.setTimer ?? ((cb, ms) => window.setTimeout(cb, ms)))(
      callback,
      delayMs,
    );
  }

  cancel(handle: TimerHandle): void {
    (this.options.clearTimer ?? ((id) => window.clearTimeout(id)))(handle);
  }

  /** The panel's monotonic clock, which everything timing a wait must read. */
  nowMs(): number {
    return (this.options.monotonicNowMs ?? (() => performance.now()))();
  }

  async load(): Promise<void> {
    this.loadStatus = "loading";
    this.loadErrorMessage = null;
    this.redraw();
    const sequence = this.nextStatusSequence();
    const result = await this.fetchJson(this.shareApiBase());
    if (this.isDisposed) return;
    if (!result.ok) {
      this.loadStatus = "load_failed";
      this.loadErrorMessage =
        "Could not load sharing status: " +
        errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.redraw();
      return;
    }
    const data = result.body as MachineSharingResponse;
    if (data.grants === null) {
      // A failed read of the workspace, not an empty policy.
      this.loadStatus = "load_failed";
      this.loadErrorMessage =
        "Everyone granted access to this workspace still has it, but the list of who that is " +
        "could not be loaded, so it cannot be edited right now.";
    } else {
      this.loadStatus = "ready";
    }
    const isProvisioningElsewhere = this.isProvisioningInProgress;
    if (!this.adoptStatusDocument(data, sequence)) {
      this.redraw();
      return;
    }
    if (data.grants === null) this.isGrantsListKnown = false;
    const migratedDomainFrom = data.migrated_domain_from ?? null;
    if (migratedDomainFrom !== null) {
      // The read itself moved the share to a new address: the new link is not
      // live until the workspace's share stack restarts on it, so it gets the
      // same provisioning wait as a fresh publish.
      this.beginProvisioningWait();
      this.migratedDomainFrom = migratedDomainFrom;
    } else if (!isProvisioningElsewhere) {
      // A link published before this session is assumed live. A publish or a
      // wait already running owns that question, and its answer is the one
      // that ends the checklist.
      this.isLive = this.isPublished;
    }
    this.syncReadinessPolling();
    this.redraw();
  }

  /**
   * The row appears now; the write and the account lookup follow it. A refused
   * entry never becomes a row.
   */
  addGrant(target: string, kind: GrantAddKind, rawValue: string): void {
    if (!this.canAdd) return;
    const value = normalizeGrantValue(kind, rawValue);
    if (!value) return;
    const row = this.addRow(target);
    const refusalMessage = this.refusalFor(target, kind, value);
    if (refusalMessage !== null) {
      row.refusalMessage = refusalMessage;
      this.redraw();
      return;
    }
    row.refusalMessage = null;
    row.value = "";
    const grantee: Grantee = { kind, value };
    const grant: Grant = {
      key: grantKey(grantee),
      grantee,
      status: { state: "saving" },
      addedAtMs: this.nowMs(),
    };
    this.discardPendingRemoval(target, grantee);
    const grants = this.mutableGrants(target);
    grants.push(grant);
    this.grantsByTarget.set(target, orderGrants(grants, this.identities));
    this.enqueueGrantsWrite();
    if (kind === "email")
      void this.upgradeGrantToAccount(target, grant.key, value);
    this.redraw();
  }

  /** Take a row out now; a failed write puts it back where it was, marked. */
  removeGrant(target: string, key: GrantKey): void {
    const grants = this.mutableGrants(target);
    const idx = grants.findIndex((grant) => grant.key === key);
    if (idx < 0) return;
    const [removed] = grants.splice(idx, 1);
    this.grantsByTarget.set(target, grants);
    const removals = this.removalsByTarget.get(target) ?? [];
    removals.push({ grant: removed, idx });
    this.removalsByTarget.set(target, removals);
    this.enqueueGrantsWrite();
    this.redraw();
  }

  /**
   * Removal is allowed while publishing is off, so only a failed add waits on
   * canAdd.
   */
  retryGrant(target: string, key: GrantKey): void {
    const grant = this.mutableGrants(target).find(
      (candidate) => candidate.key === key,
    );
    if (grant === undefined) return;
    if (
      grant.status.state === "failed" &&
      grant.status.failedOperation === "remove"
    ) {
      if (this.isGrantsListKnown) this.removeGrant(target, key);
      return;
    }
    if (!this.canAdd) return;
    grant.status = { state: "saving" };
    this.enqueueGrantsWrite();
    this.redraw();
  }

  private buildGrantsDocument(): SharingGrantsDocument {
    const document: SharingGrantsDocument = {
      workspace: this.grantListFor(this.options.wholeService),
      services: {},
    };
    for (const target of this.appTargets) {
      const grants = this.grantListFor(target);
      if (isGrantListEmpty(grants)) continue;
      document.services[target] = grants;
    }
    for (const [name, scope] of Object.entries(this.extraServiceGrants))
      document.services[name] = scope;
    return document;
  }

  dispose(): void {
    this.isDisposed = true;
    this.stopReadinessPolling();
  }

  /**
   * The step flags and the tunnel-stamp snapshot belong to the workspace's one
   * share, so they reset only here, never on a poll restart.
   */
  private beginProvisioningWait(): void {
    this.isLive = false;
    this.migratedDomainFrom = null;
    this.isCertIssued = false;
    this.isTunnelConnected = false;
    this.tunnelLoginSnapshot = { state: "unseen" };
    this.gatewayState = null;
    this.gatewayError = null;
    this.gatewayFailedAttemptCount = 0;
    this.gatewayNextRetryAt = null;
  }

  private get isProvisioningInProgress(): boolean {
    return this.isPublishWriteInFlight || this.pollingTarget !== null;
  }

  private stopReadinessPolling(): void {
    this.pollGeneration += 1;
    if (this.readinessTimerId !== null) {
      this.cancel(this.readinessTimerId);
      this.readinessTimerId = null;
    }
    this.pollingTarget = null;
  }

  /**
   * One poll while the on-screen target awaits its link or its label; the
   * responses carry the labels, so a late label lands through it too.
   */
  private syncReadinessPolling(): void {
    if (!this.isAwaitingLink && !this.isAwaitingLabel(this.currentTarget)) {
      this.stopReadinessPolling();
      return;
    }
    if (this.pollingTarget === this.currentTarget) return;
    this.stopReadinessPolling();
    if (this.isDisposed) return;
    this.pollingTarget = this.currentTarget;
    this.pollStartedAtMs = this.nowMs();
    this.scheduleReadinessProbe(this.currentTarget, this.pollGeneration, 0);
  }

  private scheduleReadinessProbe(
    target: string,
    generation: number,
    elapsedMs: number,
  ): void {
    if (this.isDisposed || generation !== this.pollGeneration) return;
    const interval =
      elapsedMs < READINESS_FAST_PHASE_MS
        ? READINESS_FAST_INTERVAL_MS
        : READINESS_SLOW_INTERVAL_MS;
    this.readinessTimerId = this.schedule(() => {
      void this.probeReadiness(target, generation);
    }, interval);
  }

  private async probeReadiness(
    target: string,
    generation: number,
  ): Promise<void> {
    if (this.isDisposed || generation !== this.pollGeneration) return;
    this.readinessTimerId = null;
    const elapsed = this.nowMs() - this.pollStartedAtMs;
    if (elapsed > READINESS_DEADLINE_MS) {
      // Stop warning at the deadline rather than pretending failure forever.
      this.markLive();
      return;
    }
    const result = await this.fetchJson(`${this.shareApiBase()}/readiness`);
    if (this.isDisposed || generation !== this.pollGeneration) return;
    const body = result.ok
      ? (result.body as SharingReadinessResponse | null)
      : null;
    if (body) {
      this.mergeServiceLabels(body.service_labels);
      if (body.cert_not_after != null) this.isCertIssued = true;
      this.adoptGatewayStatus(body);
      const tunnelStamp = body.last_tunnel_login_at ?? null;
      if (this.tunnelLoginSnapshot.state === "unseen") {
        this.tunnelLoginSnapshot = { state: "seen", stamp: tunnelStamp };
      } else if (
        tunnelStamp !== null &&
        tunnelStamp !== this.tunnelLoginSnapshot.stamp
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
    if (this.isProvisioningHalted) {
      // The gateway will not try again until the workspace is published again,
      // so polling would only repeat the same answer.
      this.stopReadinessPolling();
      this.redraw();
      return;
    }
    this.redraw();
    this.scheduleReadinessProbe(
      target,
      generation,
      this.nowMs() - this.pollStartedAtMs,
    );
  }

  private adoptGatewayStatus(body: SharingReadinessResponse): void {
    const reported = body.gateway_state ?? null;
    this.gatewayState =
      GATEWAY_STATES.find((state) => state === reported) ?? null;
    this.gatewayError = body.gateway_error ?? null;
    this.gatewayFailedAttemptCount = body.gateway_failed_attempt_count ?? 0;
    this.gatewayNextRetryAt = body.gateway_next_retry_at ?? null;
  }

  private markLive(): void {
    this.isLive = true;
    this.isCertIssued = true;
    this.isTunnelConnected = true;
    this.stopReadinessPolling();
    this.redraw();
  }

  private refusalFor(
    target: string,
    kind: GrantAddKind,
    normalized: string,
  ): string | null {
    if (this.isAlreadyGranted(target, kind, normalized))
      return `${normalized} is already granted here.`;
    if (kind === "email") {
      if (!isEmailAddress(normalized))
        return `${normalized} is not an email address.`;
      if (normalized === normalizeGrantAddress(this.options.granterEmail))
        return `${normalized} is your own address.`;
      return null;
    }
    if (!isEmailDomain(normalized)) return `${normalized} is not a domain.`;
    if (this.publicEmailDomains.has(normalized))
      return `${normalized} cannot be granted permissions because it is a public email provider.`;
    return null;
  }

  /** An address already listed here counts however it was granted: the account
   * rows carry the address their grant was made with. */
  private isAlreadyGranted(
    target: string,
    kind: GrantAddKind,
    normalized: string,
  ): boolean {
    return this.mutableGrants(target).some(
      (grant) =>
        isDomainGrant(grant) === (kind === "email_domain") &&
        normalizedValueOf(grant.grantee) === normalized,
    );
  }

  /** The row keeps its key and its place; only what it saves changes. */
  private async upgradeGrantToAccount(
    target: string,
    key: GrantKey,
    email: string,
  ): Promise<void> {
    const result = await this.fetchJson(RESOLVE_USER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (this.isDisposed) return;
    const record = result.ok
      ? (result.body as Partial<IdentityRecord> | null)
      : null;
    if (!record || typeof record.user_id !== "string" || !record.user_id)
      return;
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
    const grant = this.mutableGrants(target).find(
      (candidate) => candidate.key === key,
    );
    if (grant === undefined) return;
    grant.grantee = {
      kind: "user",
      userId: record.user_id,
      value: grant.grantee.value,
    };
    grant.status = { state: "saving" };
    this.enqueueGrantsWrite();
    this.redraw();
  }

  private mutableGrants(target: string): Grant[] {
    let grants = this.grantsByTarget.get(target);
    if (grants === undefined) {
      grants = [];
      this.grantsByTarget.set(target, grants);
    }
    return grants;
  }

  /** What a target grants as the document carries it. A row whose add failed is
   * left out until Retry sends it again; a row whose removal failed is still
   * granted, so it stays in until Retry takes it out. */
  private grantListFor(target: string): SharingGrantList {
    const users: string[] = [];
    const emails: string[] = [];
    const emailDomains: string[] = [];
    for (const { grantee, status } of this.mutableGrants(target)) {
      if (status.state === "failed" && status.failedOperation === "add")
        continue;
      if (grantee.kind === "user") pushUnique(users, grantee.userId);
      else if (grantee.kind === "email_domain")
        pushUnique(emailDomains, grantee.value);
      else pushUnique(emails, grantee.value);
    }
    return { users, emails, email_domains: emailDomains };
  }

  /**
   * A re-added address keys as an address while the document may still name
   * that person by account id, so the grantee decides, not the key.
   */
  private discardPendingRemoval(target: string, grantee: Grantee): void {
    const removals = this.removalsByTarget.get(target);
    if (removals === undefined) return;
    const kept = removals.filter(
      (removal) => !isSameGrantee(removal.grant.grantee, grantee),
    );
    if (kept.length === 0) this.removalsByTarget.delete(target);
    else this.removalsByTarget.set(target, kept);
  }

  private enqueueGrantsWrite(): void {
    if (this.isGrantsWriteInFlight) {
      this.isGrantsWriteDirty = true;
      return;
    }
    void this.runGrantsWrites();
  }

  /** One write at a time, each built from the state as it then stands, so the
   * edits made during one all ride the single write that follows it. */
  private async runGrantsWrites(): Promise<void> {
    this.isGrantsWriteInFlight = true;
    try {
      await this.writeGrantsOnce();
      while (this.isGrantsWriteDirty && !this.isDisposed) {
        this.isGrantsWriteDirty = false;
        await this.writeGrantsOnce();
      }
    } finally {
      this.isGrantsWriteInFlight = false;
    }
  }

  private async writeGrantsOnce(): Promise<void> {
    const document = this.buildGrantsDocument();
    const carriedRows = this.carriedRowSignatures();
    const carriedRemovals = this.carriedRemovals();
    const sequence = this.nextStatusSequence();
    const result = await this.fetchJson(`${this.shareApiBase()}/grants`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grants: document }),
    });
    if (this.isDisposed) return;
    if (result.ok) {
      // Read back before settling: the merge keeps every row still saving, and
      // this write's rows are settled on top of what came back.
      this.adoptStatusDocument(result.body, sequence);
      this.markCarriedRows(carriedRows, { state: "settled" });
      this.redraw();
      return;
    }
    // A refusal saves nothing, so the rest of the document is sent again
    // without the rows it named. A refusal naming nothing this panel can drop
    // would send the same document forever, so it is reported as a failure.
    const refusals = grantRefusalsFrom(result.status, result.body);
    if (refusals !== null && this.applyRefusals(refusals) > 0) {
      this.enqueueGrantsWrite();
    } else {
      // A refusal body names entries, not a sentence for the row, so only a
      // plain failure's own words are worth reporting.
      const failureMessage =
        refusals === null
          ? errorMessageFromBody(result.body, GRANT_SAVE_FAILURE_MESSAGE)
          : GRANT_SAVE_FAILURE_MESSAGE;
      this.markCarriedRows(carriedRows, {
        state: "failed",
        failedOperation: "add",
        failureMessage,
      });
      this.restoreRemovals(carriedRemovals, failureMessage);
    }
    this.redraw();
  }

  /** What each in-flight row looked like when the write was built, so an edit
   * that lands during the write is not reported as that write's outcome. */
  private carriedRowSignatures(): Map<string, Map<GrantKey, GrantKey>> {
    const carried = new Map<string, Map<GrantKey, GrantKey>>();
    for (const [target, grants] of this.grantsByTarget) {
      const signatures = new Map<GrantKey, GrantKey>();
      for (const grant of grants) {
        if (grant.status.state === "saving")
          signatures.set(grant.key, grantSignature(grant));
      }
      if (signatures.size > 0) carried.set(target, signatures);
    }
    return carried;
  }

  private carriedRemovals(): Map<string, PendingRemoval[]> {
    const carried = new Map<string, PendingRemoval[]>();
    for (const [target, removals] of this.removalsByTarget)
      carried.set(target, [...removals]);
    return carried;
  }

  private markCarriedRows(
    carried: Map<string, Map<GrantKey, GrantKey>>,
    status: GrantStatus,
  ): void {
    for (const [target, signatures] of carried) {
      for (const grant of this.mutableGrants(target)) {
        const signature = signatures.get(grant.key);
        if (
          signature === undefined ||
          grant.status.state !== "saving" ||
          grantSignature(grant) !== signature
        )
          continue;
        grant.status = status;
      }
    }
  }

  private restoreRemovals(
    carried: Map<string, PendingRemoval[]>,
    failureMessage: string,
  ): void {
    for (const [target, removals] of carried) {
      const grants = this.mutableGrants(target);
      for (const removal of removals) {
        if (!this.forgetRemoval(target, removal)) continue;
        removal.grant.status = {
          state: "failed",
          failedOperation: "remove",
          failureMessage,
        };
        grants.splice(Math.min(removal.idx, grants.length), 0, removal.grant);
      }
      this.grantsByTarget.set(target, orderGrants(grants, this.identities));
    }
  }

  private forgetRemoval(target: string, removal: PendingRemoval): boolean {
    const removals = this.removalsByTarget.get(target);
    if (removals === undefined) return false;
    const kept = removals.filter((pending) => pending !== removal);
    if (kept.length === removals.length) return false;
    if (kept.length === 0) this.removalsByTarget.delete(target);
    else this.removalsByTarget.set(target, kept);
    return true;
  }

  private applyRefusals(refusals: GrantRefusal[]): number {
    let markedCount = 0;
    for (const refusal of refusals) {
      const target =
        refusal.scope === WORKSPACE_GRANT_SCOPE
          ? this.options.wholeService
          : refusal.scope;
      const isDomainRefusal = refusal.kind === "email_domain";
      const normalized = normalizeGrantValue(
        isDomainRefusal ? "email_domain" : "email",
        refusal.value,
      );
      const grant = this.mutableGrants(target).find(
        (candidate) =>
          normalizedValueOf(candidate.grantee) === normalized &&
          isDomainGrant(candidate) === isDomainRefusal,
      );
      if (grant === undefined || grant.status.state === "failed") continue;
      grant.status = {
        state: "failed",
        failedOperation: "add",
        failureMessage: refusal.message,
      };
      markedCount += 1;
    }
    return markedCount;
  }

  private nextStatusSequence(): StatusSequence {
    this.statusRequestCount += 1;
    return this.statusRequestCount as StatusSequence;
  }

  private adoptStatusDocument(
    body: unknown,
    sequence: StatusSequence,
  ): boolean {
    if (
      this.adoptedStatusSequence !== null &&
      sequence < this.adoptedStatusSequence
    )
      return false;
    this.adoptedStatusSequence = sequence;
    const data = body as MachineSharingResponse;
    // A write sent before the switch was thrown answers with the publication
    // as it was then, which must not move the switch back under the publish.
    if (!this.isPublishWriteInFlight) this.adoptPublication(data);
    this.mergeServiceLabels(data.service_labels);
    for (const [userId, record] of Object.entries(data.identities ?? {}))
      this.identities[userId] = record;
    if (data.grants) {
      this.isGrantsListKnown = true;
      this.adoptGrants(data.grants);
    }
    return true;
  }

  private adoptPublication(data: MachineSharingResponse): void {
    this.isPublished = Boolean(data.enabled);
    this.machineUrl = data.url ?? null;
  }

  private mergeServiceLabels(labels: Record<string, string> | undefined): void {
    if (!labels) return;
    for (const [target, label] of Object.entries(labels)) {
      if (label) this.serviceLabels[target] = label;
    }
  }

  /** Replace the settled rows from the document, keeping the edits that have
   * not landed yet: rows still saving or failed stay, and a row removed here
   * stays gone until a readback no longer carries it. */
  private adoptGrants(document: SharingGrantsDocument): void {
    const services = document.services ?? {};
    for (const target of this.knownTargets) {
      const scope =
        target === this.options.wholeService
          ? document.workspace
          : services[target];
      this.adoptScope(target, scope);
    }
    this.extraServiceGrants = {};
    for (const [name, scope] of Object.entries(services)) {
      if (this.knownTargets.includes(name)) continue;
      this.extraServiceGrants[name] = scope;
    }
  }

  private adoptScope(
    target: string,
    scope: SharingGrantList | undefined,
  ): void {
    const entries = scopeEntries(scope, this.identities);
    this.clearLandedRemovals(target, entries);
    const removals = this.removalsByTarget.get(target) ?? [];
    const live = entries.filter(
      (entry) =>
        !removals.some((removal) =>
          isSameGrantee(removal.grant.grantee, entry),
        ),
    );
    const matchedIdxes = new Set<number>();
    const kept: Grant[] = [];
    for (const grant of this.mutableGrants(target)) {
      const idx = live.findIndex(
        (entry, entryIdx) =>
          !matchedIdxes.has(entryIdx) && isSameGrantee(grant.grantee, entry),
      );
      if (idx >= 0) {
        matchedIdxes.add(idx);
        if (grant.status.state === "settled") adoptIntoGrant(grant, live[idx]);
        kept.push(grant);
      } else if (grant.status.state !== "settled") {
        kept.push(grant);
      }
    }
    const added = live
      .filter((_entry, idx) => !matchedIdxes.has(idx))
      .map(grantFromEntry);
    this.grantsByTarget.set(
      target,
      orderGrants([...kept, ...added], this.identities),
    );
  }

  /** Forget the removals this document no longer carries: they have landed. */
  private clearLandedRemovals(target: string, entries: Grantee[]): void {
    const removals = this.removalsByTarget.get(target);
    if (removals === undefined) return;
    const pending = removals.filter((removal) =>
      entries.some((entry) => isSameGrantee(removal.grant.grantee, entry)),
    );
    if (pending.length === 0) this.removalsByTarget.delete(target);
    else this.removalsByTarget.set(target, pending);
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
}

function normalizeGrantAddress(value: string): string {
  return value.trim().toLowerCase();
}

/** `@acme.com` is the same grant as `acme.com`. */
function normalizeGrantDomain(value: string): string {
  const normalized = normalizeGrantAddress(value);
  return normalized.startsWith("@") ? normalized.slice(1) : normalized;
}

function normalizeGrantValue(kind: GrantKind, value: string): string {
  return kind === "email_domain"
    ? normalizeGrantDomain(value)
    : normalizeGrantAddress(value);
}

function normalizedValueOf(grantee: Grantee): string | null {
  return grantee.value === null
    ? null
    : normalizeGrantValue(grantee.kind, grantee.value);
}

function isEmailDomain(value: string): boolean {
  return DOMAIN_PATTERN.test(normalizeGrantDomain(value));
}

function isEmailAddress(value: string): boolean {
  const normalized = normalizeGrantAddress(value);
  const separatorIdx = normalized.indexOf("@");
  if (separatorIdx < 0) return false;
  return (
    EMAIL_LOCAL_PART_PATTERN.test(normalized.slice(0, separatorIdx)) &&
    isEmailDomain(normalized.slice(separatorIdx + 1))
  );
}

function isDomainGrant(grant: Grant): boolean {
  return grant.grantee.kind === "email_domain";
}

/** Domain grants first, then individuals, each keeping the order it arrived in. */
/**
 * The one order every grant list in the panel is drawn in: domains first,
 * then people, each group alphabetical by what its row reads as.
 *
 * Domains lead because one of them admits more people than any row below it,
 * and a reader scanning for who can get in should meet the widest grants
 * first. Sorting on the rendered text rather than the stored value is what
 * makes the list scannable: a row showing an account's display name sorts
 * under that name, not under the address or the id behind it.
 */
function orderGrants(
  grants: Grant[],
  identities: Record<string, IdentityRecord>,
): Grant[] {
  const byRenderedName = (one: Grant, other: Grant): number =>
    grantSortKey(one, identities).localeCompare(
      grantSortKey(other, identities),
    );
  return [
    ...grants.filter(isDomainGrant).sort(byRenderedName),
    ...grants.filter((grant) => !isDomainGrant(grant)).sort(byRenderedName),
  ];
}

/** What a row reads as, folded for comparison; mirrors the view's grantText. */
function grantSortKey(
  grant: Grant,
  identities: Record<string, IdentityRecord>,
): string {
  const grantee = grant.grantee;
  if (grantee.kind !== "user") return (grantee.value ?? "").toLowerCase();
  const record = identities[grantee.userId];
  const shown =
    record?.display_name || record?.email || grantee.value || grantee.userId;
  return shown.toLowerCase();
}

function grantKey(grantee: Grantee): GrantKey {
  const name =
    grantee.kind === "user"
      ? grantee.userId
      : normalizeGrantValue(grantee.kind, grantee.value);
  return `${grantee.kind}:${name}` as GrantKey;
}

/** What a row would put in the document, so a row edited during a write is
 * told apart from the one that write carried: the key its grantee mints now,
 * which the row's own fixed key stops matching once the grantee changes. */
function grantSignature(grant: Grant): GrantKey {
  return grantKey(grant.grantee);
}

/** Every name one grantee answers to across a document and the panel's rows. */
function granteeIdentities(grantee: Grantee): string[] {
  const identities: string[] = [];
  if (grantee.kind === "user") identities.push(`user:${grantee.userId}`);
  const normalized = normalizedValueOf(grantee);
  if (normalized !== null)
    identities.push(
      `${grantee.kind === "email_domain" ? "domain" : "individual"}:${normalized}`,
    );
  return identities;
}

function isSameGrantee(one: Grantee, other: Grantee): boolean {
  const otherIdentities = granteeIdentities(other);
  return granteeIdentities(one).some((identity) =>
    otherIdentities.includes(identity),
  );
}

/** A scope's entries in the order the document lists them. */
function scopeEntries(
  scope: SharingGrantList | undefined,
  identities: Record<string, IdentityRecord>,
): Grantee[] {
  if (!scope) return [];
  return [
    ...(scope.users ?? []).map((userId) => ({
      kind: "user" as const,
      userId,
      value: identities[userId]?.email ?? null,
    })),
    ...scope.emails.map((email) => ({
      kind: "email" as const,
      value: email,
    })),
    ...scope.email_domains.map((domain) => ({
      kind: "email_domain" as const,
      value: domain,
    })),
  ];
}

function grantFromEntry(grantee: Grantee): Grant {
  return {
    key: grantKey(grantee),
    grantee,
    status: { state: "settled" },
    addedAtMs: null,
  };
}

/** Take the document's account of a row, keeping the address it was typed with
 * when the document knows no better one. */
function adoptIntoGrant(grant: Grant, entry: Grantee): void {
  grant.grantee =
    entry.kind === "user" && entry.value === null
      ? { ...entry, value: grant.grantee.value }
      : entry;
}

function isGrantListEmpty(grants: SharingGrantList): boolean {
  return (
    (grants.users?.length ?? 0) === 0 &&
    grants.emails.length === 0 &&
    grants.email_domains.length === 0
  );
}

function pushUnique(values: string[], value: string): void {
  if (!values.includes(value)) values.push(value);
}

function grantRefusalsFrom(
  status: number,
  body: unknown,
): GrantRefusal[] | null {
  if (status !== 400 || body === null || typeof body !== "object") return null;
  const record = body as { error?: unknown; refusals?: unknown };
  if (record.error !== "grant_refused" || !Array.isArray(record.refusals))
    return null;
  return record.refusals as GrantRefusal[];
}
