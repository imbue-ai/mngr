// The share panel's model: publication, the grants per target, and the
// coalesced writes that save them. Every edit lands here at once and its write
// runs behind it; one write is in flight per workspace, and each answers with
// the status document, read back over the edits that have not landed yet.
//
// The status document is read through the shared query client, so a panel
// opened again draws what it drew last time at once and reads afresh behind
// it; the switch is a mutation in the client's ledger, so a toggle outlives
// the panel that threw it and cancels the read it would otherwise race.

import type {
  Mutation,
  MutationCacheNotifyEvent,
  MutationKey,
  QueryClient,
  QueryKey,
} from "@tanstack/query-core";
import {
  MutationObserver as QueryMutationObserver,
  QueryObserver,
  matchMutation,
} from "@tanstack/query-core";
import m from "mithril";
import { getAppQueryClient } from "./queryClient";
import type {
  FetchJson,
  IdentityRecord,
  InvitationOutcomeEntry,
  InvitationOutcomesResponse,
  InvitationResultResponse,
  MachineSharingResponse,
  MobileAccessLinkResponse,
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

const SHARING_QUERY_SCOPE = "workspace-sharing";

/** The cache entry holding a workspace's status document. */
function sharingQueryKey(coordinate: string): QueryKey {
  return [SHARING_QUERY_SCOPE, coordinate];
}

/** The ledger entry under which a workspace's switch is thrown. */
function publicationMutationKey(coordinate: string): MutationKey {
  return [SHARING_QUERY_SCOPE, coordinate, "publication"];
}

/** A sharing route that answered with a failure, in its own words. */
export class SharingRequestError extends Error {}

/** One throw of the switch, as its mutation remembers it: the token by which
 * an answer is told to be the newest attempt's, and the place of the write
 * among the panel's status requests. */
interface PublicationAttempt {
  readonly owner: SharePanelModel;
  readonly sequence: StatusSequence;
}

function isPublicationAttempt(value: unknown): value is PublicationAttempt {
  return (
    typeof value === "object" &&
    value !== null &&
    "owner" in value &&
    "sequence" in value
  );
}

// A document that did not reach Imbue Cloud is sent again by the next load.
const GRANTS_SYNC_RETRY_MS = 15_000;

/** What the panel says for each refusal an invite can come back with. The
 * allowance and the cooldown are named only at the moment of refusal (spec
 * P5, O5); every other code is the desktop's or the connector's own. */
const INVITE_REFUSAL_MESSAGES: Record<string, string> = {
  over_allowance: "You have reached your invitation limit for now.",
  too_soon: "You invited this person too recently.",
  grants_out_of_date:
    "The permissions have not reached Imbue Cloud yet. Try again in a moment.",
  not_invitable: "This person has already joined.",
  not_published: "Publish this workspace before inviting anyone.",
};

const INVITE_FAILURE_MESSAGE = "Could not invite";
/** What the panel says when the route refuses: the workspace has no address to
 * send yet, whether because sharing is off or because it is still coming up.
 * Which of the two it is names an internal state, so the panel says neither. */
const MOBILE_LINK_NO_ACCESS_POINT_MESSAGE =
  "This workspace does not have a secure access point yet. If you've just " +
  "enabled sharing and web access, please wait a few minutes before trying " +
  "again.";

const MOBILE_LINK_FAILURE_MESSAGE = "Could not send the email";

/** What the panel's mobile-link control is doing, as a single typed value so
 * exactly one of "idle", "in flight", "sent" and "refused" is ever on screen. */
export type MobileLinkState =
  | { readonly state: "idle" }
  | { readonly state: "sending" }
  /** Shown until the panel is loaded again, so the confirmation outlives the
   * redraw that follows the send. */
  | { readonly state: "sent"; readonly recipientEmail: string }
  | { readonly state: "refused"; readonly message: string };
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

/** What the granter may learn about a grant (spec O1): invited, could not
 * invite, or joined. Nothing else about a delivery ever reaches the panel. */
export type GrantOutcomeKind = "invited" | "could_not_invite" | "joined";

export interface GrantOutcome {
  readonly kind: GrantOutcomeKind;
  /** The last successful delivery, when invited. */
  readonly invitedAt: string | null;
  /** The first visit and the latest one, when joined. */
  readonly joinedAt: string | null;
  readonly lastVisitedAt: string | null;
}

export type InviteState =
  | { readonly state: "idle" }
  | { readonly state: "inviting" }
  /** Said on the row at the moment of refusal, and never again after the
   * next invite or load (spec O5). */
  | { readonly state: "refused"; readonly message: string };

/** The one thing a grant's invitation status indicator shows, as a single typed value so exactly one
 * state is ever on screen (never a status and a stale button at once). The first invitation is
 * automatic -- granting a person requests one -- so the only button this ever carries is a
 * re-invite, offered when the invitation can be requested again. */
export type InvitationStatusIndicator =
  | { readonly kind: "none" }
  | { readonly kind: "inviting" }
  | { readonly kind: "invited"; readonly at: string | null; readonly canReinvite: boolean }
  | { readonly kind: "could_not_invite"; readonly canReinvite: boolean }
  | { readonly kind: "refused"; readonly message: string; readonly canReinvite: boolean }
  | { readonly kind: "joined"; readonly firstAt: string | null; readonly lastAt: string | null };

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
  /** What users read for each app service; one with none is called by its name. */
  serviceDisplayNames?: Record<string, string>;
  /** The mail providers a domain grant may never name. */
  publicEmailDomains: string[];
  fetchJson?: FetchJson;
  redraw?: () => void;
  /** Injected timer hooks so tests drive the readiness poll deterministically. */
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (timerId: number) => void;
  monotonicNowMs?: () => number;
  /** The cache the status read is answered from and the ledger the switch is
   * thrown in, shared across panels; the app's unless a test brings its own. */
  queryClient?: QueryClient;
}

/** A row taken out of the list whose removal has not been saved yet. */
interface PendingRemoval {
  grant: Grant;
  idx: number;
}

export class SharePanelModel {
  loadStatus: ShareLoadStatus = "idle";
  loadErrorMessage: string | null = null;
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
  /** Whether the document has reached Imbue Cloud's grants table, which
   * invitations are made from: null while unpublished or not yet known. */
  grantsSynced: boolean | null = null;

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
  /** The publication as the newest document has it; the switch reads this
   * only while no toggle is in flight. */
  private isPublishedByDocument = false;
  private readonly queryClient: QueryClient;
  private readonly queryKey: QueryKey;
  private readonly publicationMutationKey: MutationKey;
  private readonly statusQuery: QueryObserver<MachineSharingResponse, Error>;
  private readonly publicationWrite: QueryMutationObserver<
    MachineSharingResponse,
    Error,
    boolean,
    PublicationAttempt
  >;
  private readonly unsubscribeStatusQuery: () => void;
  private readonly unsubscribeMutations: () => void;
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
  // Keyed by target and grantee, as the outcomes route lists them.
  private outcomeByKey = new Map<string, GrantOutcome>();
  private readonly inviteStateByKey = new Map<GrantKey, InviteState>();
  // What the mobile-link control is doing. One per panel rather than per
  // target: the message carries the whole workspace's link, whichever target
  // the pane is on.
  private mobileLink: MobileLinkState = { state: "idle" };
  // A person is auto-invited once, when their grant first settles and the document has reached
  // Imbue Cloud; a later re-invite is the granter's own doing.
  private readonly autoInvitedKeys = new Set<GrantKey>();
  // Outcome reads are numbered as sent, like status requests, so an older
  // answer cannot overwrite a newer one.
  private outcomesRequestCount = 0;
  private adoptedOutcomesSequence = 0;
  private grantsSyncRetryTimerId: TimerHandle | null = null;

  constructor(options: SharePanelModelOptions) {
    this.options = options;
    this.serviceLabels = { ...options.serviceLabels };
    this.publicEmailDomains = new Set(
      options.publicEmailDomains.map(normalizeGrantDomain),
    );
    this.currentTarget = options.wholeService;
    this.queryClient = options.queryClient ?? getAppQueryClient();
    const coordinate = options.agentId ?? options.hostId;
    this.queryKey = sharingQueryKey(coordinate);
    this.publicationMutationKey = publicationMutationKey(coordinate);
    this.statusQuery = new QueryObserver<MachineSharingResponse, Error>(
      this.queryClient,
      {
        queryKey: this.queryKey,
        queryFn: () => this.readStatusDocument(),
        // Read only when the panel asks: a read costs the desktop client a
        // connector lookup and an exec into the workspace.
        enabled: false,
      },
    );
    this.unsubscribeStatusQuery = this.statusQuery.subscribe(() =>
      this.redraw(),
    );
    this.publicationWrite = new QueryMutationObserver(this.queryClient, {
      mutationKey: this.publicationMutationKey,
      mutationFn: (isPublishing) => this.writePublication(isPublishing),
      onMutate: (): PublicationAttempt => {
        // A read still out answers with the publication as it was before the
        // switch was thrown.
        void this.queryClient.cancelQueries({ queryKey: this.queryKey });
        return { owner: this, sequence: this.nextStatusSequence() };
      },
      onSuccess: (document, _isPublishing, attempt) => {
        // The newest attempt's answer is the one a panel opened later starts
        // from. This runs whether or not the panel that threw the switch is
        // still open.
        if (this.latestPublicationMutation()?.state.context === attempt)
          this.queryClient.setQueryData(this.queryKey, document);
      },
    });
    this.unsubscribeMutations = this.queryClient
      .getMutationCache()
      .subscribe((event) => this.onMutationEvent(event));
  }

  /** Flipped the moment the switch is thrown, before the route answers. */
  get isPublished(): boolean {
    const pending = this.pendingPublication;
    return pending === null ? this.isPublishedByDocument : pending;
  }

  get publishWrite(): PublishWrite {
    const latest = this.latestPublicationMutation();
    if (latest === undefined) return { state: "idle" };
    const { status, variables, error } = latest.state;
    if (status === "pending")
      return { state: variables === true ? "publishing" : "unpublishing" };
    if (status === "error")
      return {
        state: "failed",
        message: error?.message ?? "Could not change sharing",
      };
    return { state: "idle" };
  }

  /** Whether there is anything to draw the switch from: a document read or
   * shown from the cache, or a toggle whose direction is known. */
  get isPublicationKnown(): boolean {
    return this.isPublishWriteInFlight || this.adoptedStatusSequence !== null;
  }

  /** Whether a document has arrived to draw the target pane from.
   *
   * Until one has, every answer the pane would give is a default dressed as a
   * fact: no link section because publishing reads false, an empty grant list
   * that reads as "nobody", and a mobile-link control disabled for a reason
   * that may not be the real one. Unlike the switch's own
   * ``isPublicationKnown``, a toggle in flight does not count -- it settles
   * the publication, not the grants the pane lists. */
  get isSharingDocumentKnown(): boolean {
    return this.adoptedStatusSequence !== null;
  }

  /** Whether the pane has nothing real to draw and a read is still out.
   *
   * Only a read still in flight counts: a read that failed also leaves
   * nothing adopted, and standing in a placeholder for it would pulse
   * forever with no way out. The publish widget's own error notice carries
   * that case. */
  get isSharingDocumentLoading(): boolean {
    return (
      !this.isSharingDocumentKnown &&
      this.statusQuery.getCurrentResult().isFetching
    );
  }

  /** Nothing is known yet, and the read that will say is still out. */
  get isCheckingPublication(): boolean {
    return (
      !this.isPublicationKnown && this.statusQuery.getCurrentResult().isFetching
    );
  }

  /** An earlier answer is on screen while a fresh read runs behind it. */
  get isRevalidating(): boolean {
    return (
      this.adoptedStatusSequence !== null &&
      this.statusQuery.getCurrentResult().isFetching
    );
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

  /** What to call a target: the name the workspace gives it, else its own. A
   * target's name is an identifier, free of hostname rules, and need not be a
   * thing anyone would want to read. */
  targetDisplayName(target: string): string {
    return this.options.serviceDisplayNames?.[target] || target;
  }

  /** Whether a target's own name has to be shown beside what it is called,
   * because another target is called the same: display names need not be
   * unique, and two rows reading alike would grant different things. */
  isDisplayNameAmbiguous(target: string): boolean {
    const shown = this.targetDisplayName(target);
    if (shown === target) return false;
    return this.appTargets.some(
      (other) => other !== target && this.targetDisplayName(other) === shown,
    );
  }

  /** Whether a grant can be made: the workspace has an address to admit anyone
   * to, and the list it is admitting on is known. */
  get canAdd(): boolean {
    return this.isPublished && this.isGrantsListKnown;
  }

  get isAwaitingLink(): boolean {
    return this.isPublished && !this.isLive && this.machineUrl !== null;
  }

  /** The document is saved on the workspace but has not reached Imbue Cloud,
   * so nobody can be invited until the next load sends it again (spec P7). */
  get isGrantsSyncPending(): boolean {
    return this.isPublished && this.grantsSynced === false;
  }

  /** The granter-visible outcome of a row, or null while nothing is known. */
  outcomeFor(target: string, grant: Grant): GrantOutcome | null {
    const key = outcomeKey(target, grant.grantee);
    return key === null ? null : (this.outcomeByKey.get(key) ?? null);
  }

  inviteStateFor(key: GrantKey): InviteState {
    return this.inviteStateByKey.get(key) ?? { state: "idle" };
  }

  /** Whether the row can be invited now: a person rather than a domain, saved,
   * not yet joined, while the workspace is published and its document has
   * reached Imbue Cloud (spec P3, P4, P7). */
  canInvite(target: string, grant: Grant): boolean {
    if (grant.grantee.kind === "email_domain") return false;
    if (!this.isPublished || this.grantsSynced !== true) return false;
    if (grant.status.state !== "settled") return false;
    if (this.inviteStateFor(grant.key).state === "inviting") return false;
    return this.outcomeFor(target, grant)?.kind !== "joined";
  }

  /** The single value the row's invitation status indicator shows (spec P2, P5, P8), reduced from the
   * in-flight request, the last refusal, and the granter-visible outcome, in that order, so the
   * view only ever renders one of them. A domain row, and a row with nothing known yet, show
   * nothing. */
  invitationStatusIndicator(target: string, grant: Grant): InvitationStatusIndicator {
    if (grant.grantee.kind === "email_domain") return { kind: "none" };
    const invite = this.inviteStateFor(grant.key);
    if (invite.state === "inviting") return { kind: "inviting" };
    const canReinvite = this.canInvite(target, grant);
    if (invite.state === "refused")
      return { kind: "refused", message: invite.message, canReinvite };
    const outcome = this.outcomeFor(target, grant);
    if (outcome === null) return { kind: "none" };
    if (outcome.kind === "joined")
      return { kind: "joined", firstAt: outcome.joinedAt, lastAt: outcome.lastVisitedAt };
    if (outcome.kind === "invited")
      return { kind: "invited", at: outcome.invitedAt, canReinvite };
    return { kind: "could_not_invite", canReinvite };
  }

  /** Request one invitation for every newly granted person whose grant has just settled and
   * reached Imbue Cloud -- granting a person is what asks, so the granter never presses a first
   * Invite. Fires once per grant (a re-invite is manual); a grant read back from the stored
   * document, a domain, or a person already invited or joined is left alone. The connector still
   * applies the cooldown, the allowance, and the suppression list, and its answer becomes the
   * row's status indicator. */
  private maybeAutoInviteGrantedPeople(): void {
    if (!this.isPublished || this.grantsSynced !== true) return;
    for (const target of this.knownTargets) {
      for (const grant of this.mutableGrants(target)) {
        if (grant.addedAtMs === null) continue;
        if (this.autoInvitedKeys.has(grant.key)) continue;
        if (this.outcomeFor(target, grant) !== null) continue;
        if (!this.canInvite(target, grant)) continue;
        this.autoInvitedKeys.add(grant.key);
        void this.invite(target, grant.key);
      }
    }
  }

  /** A target whose link cannot be shown yet because its label has not reached
   * the backend (typically right after app start). */
  isAwaitingLabel(target: string): boolean {
    return (
      this.isPublished && this.machineUrl !== null && !this.isLabelKnown(target)
    );
  }

  private get isPublishWriteInFlight(): boolean {
    return this.pendingPublication !== null;
  }

  /** The direction of the newest toggle while it has not answered; null once
   * it has, or when none was ever thrown. */
  private get pendingPublication(): boolean | null {
    const latest = this.latestPublicationMutation();
    if (latest === undefined || latest.state.status !== "pending") return null;
    return latest.state.variables === true;
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
    this.beginProvisioningWait();
    await this.throwSwitch(true);
  }

  /** Take the workspace's address away, keeping every grant it was admitting on. */
  async unpublish(): Promise<void> {
    await this.throwSwitch(false);
  }

  /** The toggle runs in the shared ledger, where every panel on this workspace
   * reads it (onMutationEvent); it resolves once the write has answered. */
  private async throwSwitch(isPublishing: boolean): Promise<void> {
    // A failure is reported through publishWrite, not thrown at the caller.
    await this.publicationWrite.mutate(isPublishing).catch(() => undefined);
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

  /**
   * Draw the document the cache holds for this workspace, if any, and read a
   * fresh one behind it. The first read of a session leaves the switch
   * undrawn (isCheckingPublication) until it answers.
   */
  async load(): Promise<void> {
    this.loadErrorMessage = null;
    // A refusal is said once, at the moment it happened (spec O5).
    for (const [key, state] of this.inviteStateByKey)
      if (state.state === "refused") this.inviteStateByKey.delete(key);
    // The mobile link's confirmation and refusal are about the moment they
    // happened too, so a fresh look at the panel starts over.
    if (this.mobileLink.state !== "sending")
      this.mobileLink = { state: "idle" };
    const cached = this.statusQuery.getCurrentResult().data;
    if (cached !== undefined && this.adoptedStatusSequence === null)
      this.adoptLoadedDocument(cached, this.nextStatusSequence());
    if (this.adoptedStatusSequence === null) this.loadStatus = "loading";
    this.redraw();
    const sequence = this.nextStatusSequence();
    const query = this.statusQuery.getCurrentQuery();
    const documentsBefore = query.state.dataUpdateCount;
    const result = await this.statusQuery.refetch({ cancelRefetch: false });
    if (this.isDisposed) return;
    if (result.isError) {
      this.loadStatus = "load_failed";
      this.loadErrorMessage =
        "Could not load sharing status: " + result.error.message;
      this.redraw();
      return;
    }
    // A read the switch cancelled leaves the document as it was; the toggle
    // that cancelled it owns the switch now.
    if (
      result.data === undefined ||
      query.state.dataUpdateCount === documentsBefore
    )
      return;
    this.adoptLoadedDocument(result.data, sequence);
  }

  /** Take in a document a read answered with, or the cache held. */
  private adoptLoadedDocument(
    data: MachineSharingResponse,
    sequence: StatusSequence,
  ): void {
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
    this.maybeAutoInviteGrantedPeople();
    void this.refreshOutcomes();
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
    this.inviteStateByKey.delete(key);
    this.autoInvitedKeys.delete(key);
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

  /**
   * Invite the person on a row. The row shows its pending state and nothing
   * else waits (spec P10); the answer replaces it, and the outcomes route is
   * read again so the row says what Imbue Cloud now knows (spec P2).
   */
  async invite(target: string, key: GrantKey): Promise<void> {
    const grant = this.mutableGrants(target).find(
      (candidate) => candidate.key === key,
    );
    if (grant === undefined || !this.canInvite(target, grant)) return;
    const grantee = grant.grantee;
    const subject =
      grantee.kind === "user"
        ? { user_id: grantee.userId }
        : { email: grantee.value };
    this.inviteStateByKey.set(key, { state: "inviting" });
    this.redraw();
    const result = await this.fetchJson(`${this.shareApiBase()}/invitations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...subject,
        app: target === this.options.wholeService ? null : target,
      }),
    });
    if (this.isDisposed) return;
    this.inviteStateByKey.set(
      key,
      this.adoptInviteAnswer(target, grantee, result),
    );
    this.redraw();
    void this.refreshOutcomes();
  }

  /** The row's state after an invite answered, with the outcome the answer
   * itself carries applied so the row does not wait on the re-read. */
  private adoptInviteAnswer(
    target: string,
    grantee: Grantee,
    result: { ok: boolean; status: number; body: unknown },
  ): InviteState {
    const key = outcomeKey(target, grantee);
    if (result.ok) {
      const body = result.body as InvitationResultResponse;
      if (body.outcome === "invited" || body.outcome === "could_not_invite") {
        if (key !== null)
          this.outcomeByKey.set(key, {
            kind: body.outcome,
            invitedAt: body.invited_at ?? null,
            joinedAt: null,
            lastVisitedAt: null,
          });
        return { state: "idle" };
      }
      return {
        state: "refused",
        message:
          INVITE_REFUSAL_MESSAGES[body.outcome] ?? INVITE_FAILURE_MESSAGE,
      };
    }
    const code = refusalCodeOf(result.body);
    const message =
      code !== null && INVITE_REFUSAL_MESSAGES[code] !== undefined
        ? INVITE_REFUSAL_MESSAGES[code]
        : errorMessageFromBody(result.body, INVITE_FAILURE_MESSAGE);
    return { state: "refused", message };
  }

  /** What the mobile-link control is doing. */
  get mobileLinkState(): MobileLinkState {
    return this.mobileLink;
  }

  /** Whether the link can be emailed now: sharing is on, and no request of
   * this panel's is already in flight. A workspace whose address has not
   * arrived yet is still askable -- the route answers with the wait. */
  get canSendMobileAccessLink(): boolean {
    return this.isPublished && this.mobileLink.state !== "sending";
  }

  /**
   * Ask Imbue Cloud to email the signed-in account the link to this workspace.
   *
   * Where it goes is not the panel's to choose: the connector reads the
   * address off the session, so the control cannot be aimed at anyone else.
   */
  async sendMobileAccessLink(): Promise<void> {
    if (!this.canSendMobileAccessLink) return;
    this.mobileLink = { state: "sending" };
    this.redraw();
    const result = await this.fetchJson(
      `${this.shareApiBase()}/mobile-access-link`,
      { method: "POST", headers: { "Content-Type": "application/json" } },
    );
    if (this.isDisposed) return;
    this.mobileLink = adoptMobileLinkAnswer(result, this.granterEmail);
    this.redraw();
  }

  /** Read what the granter may learn about every row. Nothing is readable
   * while unpublished: no invitation can be made until publishing again. */
  private async refreshOutcomes(): Promise<void> {
    if (!this.isPublished) return;
    const sequence = ++this.outcomesRequestCount;
    const result = await this.fetchJson(
      `${this.shareApiBase()}/invitation-outcomes`,
    );
    if (this.isDisposed || sequence <= this.adoptedOutcomesSequence) return;
    if (!result.ok) return;
    this.adoptedOutcomesSequence = sequence;
    const entries = (result.body as InvitationOutcomesResponse).outcomes ?? [];
    this.outcomeByKey = outcomesByKey(entries);
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
    // A read still out finishes into the cache for the next panel; a toggle
    // still out keeps running in the ledger. This panel just stops watching.
    this.unsubscribeStatusQuery();
    this.unsubscribeMutations();
    this.publicationWrite.reset();
    if (this.grantsSyncRetryTimerId !== null) {
      this.cancel(this.grantsSyncRetryTimerId);
      this.grantsSyncRetryTimerId = null;
    }
  }

  /** One load, a moment from now, while the document has not reached Imbue
   * Cloud: the load sends it again (spec P7). */
  private scheduleGrantsSyncRetry(): void {
    if (this.grantsSyncRetryTimerId !== null || this.isDisposed) return;
    this.grantsSyncRetryTimerId = this.schedule(() => {
      this.grantsSyncRetryTimerId = null;
      if (this.isGrantsSyncPending && !this.isDisposed) void this.load();
    }, GRANTS_SYNC_RETRY_MS);
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
      const document = result.body as MachineSharingResponse;
      if (this.adoptStatusDocument(document, sequence))
        this.queryClient.setQueryData(this.queryKey, document);
      this.markCarriedRows(carriedRows, { state: "settled" });
      this.maybeAutoInviteGrantedPeople();
      this.redraw();
      void this.refreshOutcomes();
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
    if ("grants_synced" in data) {
      this.grantsSynced = data.grants_synced ?? null;
      if (this.isGrantsSyncPending) this.scheduleGrantsSyncRetry();
    }
    return true;
  }

  private adoptPublication(data: MachineSharingResponse): void {
    this.isPublishedByDocument = Boolean(data.enabled);
    this.machineUrl = data.url ?? null;
  }

  /** The newest throw of this workspace's switch, from any panel, answered or
   * not; undefined when none is remembered. */
  private latestPublicationMutation(): Mutation | undefined {
    return this.queryClient
      .getMutationCache()
      .findAll({ mutationKey: this.publicationMutationKey, exact: true })
      .at(-1);
  }

  /** A throw of this workspace's switch moved on, in this panel or another. */
  private onMutationEvent(event: MutationCacheNotifyEvent): void {
    const mutation = event.mutation;
    if (
      mutation === undefined ||
      !matchMutation(
        { mutationKey: this.publicationMutationKey, exact: true },
        mutation,
      )
    )
      return;
    if (
      event.type === "updated" &&
      event.action.type === "success" &&
      mutation === this.latestPublicationMutation()
    ) {
      const document = event.action.data as MachineSharingResponse;
      const attempt = mutation.state.context;
      // A toggle thrown from an earlier panel predates everything this one has
      // read, so only this panel's own write takes a place among its status
      // requests; the grants arrive with the read this panel has out.
      if (isPublicationAttempt(attempt) && attempt.owner === this)
        this.adoptStatusDocument(document, attempt.sequence);
      // The route that created the link is the authority on it, even when a
      // write served after it has already brought the grants forward.
      this.adoptPublication(document);
      // Only a workspace that now has a link can be invited to; taking the
      // address away leaves the outcomes alone (spec O5).
      if (this.isPublished) {
        this.maybeAutoInviteGrantedPeople();
        void this.refreshOutcomes();
      }
      this.syncReadinessPolling();
    }
    this.redraw();
  }

  private async readStatusDocument(): Promise<MachineSharingResponse> {
    const result = await this.fetchJson(this.shareApiBase());
    if (!result.ok)
      throw new SharingRequestError(
        errorMessageFromBody(result.body, `HTTP ${result.status}`),
      );
    return result.body as MachineSharingResponse;
  }

  private async writePublication(
    isPublishing: boolean,
  ): Promise<MachineSharingResponse> {
    const result = await this.fetchJson(this.shareApiBase(), {
      method: isPublishing ? "PUT" : "DELETE",
    });
    if (!result.ok)
      throw new SharingRequestError(
        errorMessageFromBody(result.body, `HTTP ${result.status}`),
      );
    return result.body as MachineSharingResponse;
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

/** The name the outcomes route lists a row under, or null for a domain row,
 * which is never invited. */
function outcomeKey(target: string, grantee: Grantee): string | null {
  if (grantee.kind === "user") return `${target}|user:${grantee.userId}`;
  if (grantee.kind === "email")
    return `${target}|email:${normalizeGrantAddress(grantee.value)}`;
  return null;
}

function outcomesByKey(
  entries: InvitationOutcomeEntry[],
): Map<string, GrantOutcome> {
  const outcomes = new Map<string, GrantOutcome>();
  for (const entry of entries) {
    const kind = entry.outcome;
    if (kind !== "invited" && kind !== "could_not_invite" && kind !== "joined")
      continue;
    const grantee: Grantee =
      entry.kind === "user"
        ? { kind: "user", userId: entry.value, value: null }
        : { kind: "email", value: entry.value };
    const key = outcomeKey(entry.app, grantee);
    if (key === null) continue;
    outcomes.set(key, {
      kind,
      invitedAt: entry.invited_at ?? null,
      joinedAt: entry.joined_at ?? null,
      lastVisitedAt: entry.last_visited_at ?? null,
    });
  }
  return outcomes;
}

/** The panel's reading of what the mobile-link route answered. */
function adoptMobileLinkAnswer(
  result: { ok: boolean; status: number; body: unknown },
  granterEmail: string,
): MobileLinkState {
  if (result.ok) {
    const body = result.body as MobileAccessLinkResponse;
    if (body.outcome === "sent")
      return {
        state: "sent",
        recipientEmail: body.recipient_email || granterEmail,
      };
    return { state: "refused", message: MOBILE_LINK_FAILURE_MESSAGE };
  }
  // Every refusal this route makes is one of the two 409s, and both mean the
  // same thing to a reader: there is no address to send yet.
  if (result.status === 409)
    return { state: "refused", message: MOBILE_LINK_NO_ACCESS_POINT_MESSAGE };
  return {
    state: "refused",
    message: errorMessageFromBody(result.body, MOBILE_LINK_FAILURE_MESSAGE),
  };
}

/** The ``error`` code of a refusal body, whatever route answered it, or null. */
function errorCodeOf(body: unknown): string | null {
  if (body === null || typeof body !== "object") return null;
  const code = (body as { error?: unknown }).error;
  return typeof code === "string" && code !== "" ? code : null;
}

/** The refusal code of an invite the desktop answered with 409, or null. */
function refusalCodeOf(body: unknown): string | null {
  const code = errorCodeOf(body);
  return code !== null && code in INVITE_REFUSAL_MESSAGES ? code : null;
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
