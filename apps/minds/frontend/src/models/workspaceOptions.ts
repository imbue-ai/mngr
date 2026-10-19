// Model for the workspace options panel + settings page: the options-data
// load, the Workspace settings actions (rename / color / account / lifecycle),
// and the share panel's own model, which this load constructs.

import m from "mithril";
import type {
  MachineSizeDimension,
  MachineSizeDraft,
  MachineSizeNumbers,
} from "./machineSize";
import { parseMachineSizeNumber } from "./machineSize";
import { SharePanelModel } from "./sharePanel";
import type { SharePanelModelOptions } from "./sharePanel";

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
  is_account_owned_provider: boolean;
  /** The account an account-owned machine belongs to; '' when not account-owned or not known. */
  leased_owner_email: string;
  has_account: boolean;
  account_email: string;
  account_display_name: string | null;
  account_profile_picture_url: string | null;
  current_account: WorkspaceOptionsAccount | null;
  accounts: WorkspaceOptionsAccount[];
  app_services: string[];
  system_services?: string[];
  service_labels: Record<string, string>;
  service_icons?: Record<string, string>;
  service_display_names?: Record<string, string>;
  whole_service: string;
  /** The mail providers a domain grant may never name, so the panel can refuse
   * one before the row. */
  public_email_domains: string[];
  /** "" when discovery reports no SSH endpoint for the machine. */
  ssh_command: string;
}

/** Response shape of GET /ui/api/workspaces/<id>/machine-size (ui_api_options.py). */
export interface WorkspaceMachineSizeData extends MachineSizeNumbers {
  is_available: boolean;
  /** Whether an unavailable size is one the app failed to read (the connector
   * could not answer for the machine) rather than one the provider does not report. */
  is_read_failed?: boolean;
  /** CPUs; null when unknown, or not shown (Imbue Cloud, whose count does not follow a resize). */
  cpu_count: number | null;
  /** Whether this page can change the machine's size (local docker and lima
   * machines, and an Imbue Cloud machine on an account offered the resize). */
  is_resizable: boolean;
  /** Whether the data disk can be grown from this page (lima and Imbue Cloud). */
  is_disk_resizable: boolean;
  /** Whether this answer carries the size but not yet whether it can be changed:
   * a second read with include_resize_offer=1 says (Imbue Cloud, whose offer
   * needs the account's plan and usage, a slow connector read). */
  is_resize_offer_pending?: boolean;
  /** Whether the app is restarting the machine right now (a host recovery is running). */
  is_restart_in_progress?: boolean;
  target_memory_gb: number | null;
  target_disk_gb: number | null;
  is_restart_needed_to_apply: boolean;
  /** Whether the machine is running, so a restart would apply a pending size now; absent when unknown. */
  is_running?: boolean | null;
  /** Whether the machine is mid stop or start, during which a resize is refused (Imbue Cloud). */
  is_transitioning?: boolean;
  /** The RAM sizes an Imbue Cloud machine may be set to within its plan, its
   * current size included; empty (or absent) for a machine sized by free numbers. */
  memory_choices_gb?: number[];
  /** Whether the plan's quota, not the fleet's largest size, bounds the choices. */
  is_memory_capped_by_plan?: boolean;
  plan_memory_limit_gb?: number | null;
  other_machines_memory_gb?: number | null;
  /** The largest data disk the machine may be grown to within its plan (Imbue Cloud). */
  max_disk_gb?: number | null;
  plan_disk_limit_gb?: number | null;
  other_machines_disk_gb?: number | null;
}

/** Whether the machine's RAM is picked from a list of sizes its plan allows (Imbue Cloud)
 * rather than typed as a free number (docker, lima). */
export function isMachineSizePickedFromChoices(
  size: WorkspaceMachineSizeData,
): boolean {
  return (size.memory_choices_gb?.length ?? 0) > 0;
}

/** The dimensions the settings page may change for the machine, in the order its fields are drawn. */
export function resizableMachineSizeDimensions(
  size: WorkspaceMachineSizeData,
): MachineSizeDimension[] {
  const withDisk = (
    dimensions: MachineSizeDimension[],
  ): MachineSizeDimension[] =>
    size.is_disk_resizable ? [...dimensions, "disk_gb"] : dimensions;
  return isMachineSizePickedFromChoices(size)
    ? withDisk(["memory_gb"])
    : withDisk(["cpu_count", "memory_gb"]);
}

/** The smallest data disk the machine may be given: its current size, since the disk only grows. */
export function minimumMachineDiskGb(size: WorkspaceMachineSizeData): number {
  return size.disk_gb === null ? 1 : Math.round(size.disk_gb);
}

/**
 * Why a typed size for a machine sized from choices could not be recorded:
 * a RAM size off the plan's list, or a disk outside its grow-only range.
 * Null when the draft is within bounds (or the machine is sized by free
 * numbers, whose provider is the judge).
 */
export function machineSizeDraftError(
  size: WorkspaceMachineSizeData,
  draft: MachineSizeDraft,
): string | null {
  if (!isMachineSizePickedFromChoices(size)) return null;
  const choices = size.memory_choices_gb ?? [];
  const memory = parseMachineSizeNumber(draft.memory_gb);
  if (
    draft.memory_gb.trim() !== "" &&
    (memory === null || !choices.includes(memory))
  ) {
    return `Pick one of the RAM sizes offered (up to ${choices[choices.length - 1]} GB).`;
  }
  if (size.is_disk_resizable && draft.disk_gb.trim() !== "") {
    const disk = parseMachineSizeNumber(draft.disk_gb);
    const current = minimumMachineDiskGb(size);
    const max = size.max_disk_gb ?? null;
    if (disk === null || disk < current) {
      return `The disk only grows: enter a whole number of at least ${current} GB.`;
    }
    if (max !== null && disk > max) {
      return `Your plan allows a disk of up to ${max} GB for this machine.`;
    }
  }
  return null;
}

/** Body of POST /api/v1/workspaces/<id>/machine-size. */
export interface MachineSizeChangeBody {
  cpu_count?: number;
  memory_gb?: number;
  disk_gb?: number;
}

/** Response shape of POST /api/v1/workspaces/<id>/machine-size. */
export interface MachineSizeChangeResponse {
  is_restart_dispatched: boolean;
  operation_id: string | null;
}

/** The pending size a restart would apply ("16 GB RAM · 56 GB disk"), falling
 * back to the current value for the factor that has no pending target; "" when
 * no restart is needed. */
export function formatPendingMachineSize(
  size: WorkspaceMachineSizeData,
): string {
  if (!size.is_restart_needed_to_apply) return "";
  const pendingMemory = size.target_memory_gb ?? size.memory_gb;
  const pendingDisk = size.target_disk_gb ?? size.disk_gb;
  const parts: string[] = [];
  if (pendingMemory !== null) parts.push(`${pendingMemory} GB RAM`);
  if (pendingDisk !== null) parts.push(`${pendingDisk} GB disk`);
  return parts.join(" · ");
}

/**
 * The resize request a draft asks for: only the dimensions the machine can
 * change, each a whole positive number; a dimension left empty keeps its
 * current value. Null when a filled field is not such a number, or when
 * nothing differs from the current size.
 */
export function machineSizeChangeBody(
  size: WorkspaceMachineSizeData,
  draft: MachineSizeDraft,
): MachineSizeChangeBody | null {
  const body: MachineSizeChangeBody = {};
  for (const dimension of resizableMachineSizeDimensions(size)) {
    if (draft[dimension].trim() === "") continue;
    const value = parseMachineSizeNumber(draft[dimension]);
    if (value === null) return null;
    const current = size[dimension];
    if (current !== null && Math.round(current) === value) continue;
    body[dimension] = value;
  }
  return Object.keys(body).length === 0 ? null : body;
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
  /** Set only on the read that moved the share off a content domain the tier
   * retired: the domain it lived at before. */
  migrated_domain_from?: string | null;
  /** Whether the document has reached Imbue Cloud's centralized grants table,
   * which invitations are made from: false when the push did not land (the
   * next save or load sends it again), null while the workspace is
   * unpublished, absent from desktops that predate invitations. */
  grants_synced?: boolean | null;
}

/** One row of GET /api/v1/workspace-sharing/<id>/invitation-outcomes: what the
 * granter may learn about an open user or email grant. */
export interface InvitationOutcomeEntry {
  kind: string;
  /** The account id of a user grant, the address of an email grant. */
  value: string;
  /** The share target, the whole workspace under its service name. */
  app: string;
  /** invited, could_not_invite, or joined; null when nothing is known. */
  outcome: string | null;
  invited_at?: string | null;
  joined_at?: string | null;
  last_visited_at?: string | null;
}

export interface InvitationOutcomesResponse {
  outcomes: InvitationOutcomeEntry[];
}

/** Response shape of POST /api/v1/workspace-sharing/<id>/invitations. */
export interface InvitationResultResponse {
  /** invited, could_not_invite, over_allowance, or too_soon. */
  outcome: string;
  invited_at?: string | null;
}

/** Response shape of POST /api/v1/workspace-sharing/<id>/mobile-access-link. */
export interface MobileAccessLinkResponse {
  /** sent or failed. */
  outcome: string;
  /** The signed-in account's own address the message went to. */
  recipient_email?: string;
  sent_at?: string | null;
}

/** The route that resolves a typed address to an account (a 404 means "store an invite"). */
export const RESOLVE_USER_URL = "/ui/api/users/resolve";

/** Response shape of GET /api/v1/workspace-sharing/<id>/readiness. */
export interface SharingReadinessResponse {
  ready?: boolean;
  cert_not_after?: string | null;
  last_tunnel_login_at?: string | null;
  service_labels?: Record<string, string>;
  /** The workspace gateway's own bring-up report while the link is not live. */
  gateway_state?: string | null;
  gateway_error?: string | null;
  gateway_failed_attempt_count?: number | null;
  gateway_next_retry_at?: string | null;
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
export interface FetchJson {
  (
    url: string,
    init?: RequestInit,
  ): Promise<{ ok: boolean; status: number; body: unknown }>;
}

/** Schedules `refresh` for later and returns how to cancel it. */
export type ScheduleRefresh = (refresh: () => void) => () => void;

/** How long the page waits between re-reads of a restarting machine's size. */
const MACHINE_SIZE_REFRESH_MS = 10_000;

function defaultScheduleRefresh(refresh: () => void): () => void {
  const timer = setTimeout(refresh, MACHINE_SIZE_REFRESH_MS);
  return () => clearTimeout(timer);
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

/** Load + action state for the Workspace settings panes. */
export class WorkspaceOptionsModel {
  readonly agentId: string;
  status: "loading" | "load_failed" | "ready" = "loading";
  data: WorkspaceOptionsData | null = null;
  share: SharePanelModel | null = null;
  /** The machine's size as last read; null until (and unless) it loads. */
  machineSize: WorkspaceMachineSizeData | null = null;
  /** Where the size stands before it is held in `machineSize`: still being read,
   * not reported for this machine (the server's answer), or not readable at all
   * (the read itself failed); "ready" once held. */
  machineSizeState: "loading" | "unavailable" | "load_failed" | "ready" =
    "loading";
  /** Whether the read that says if the size can be changed is still in flight (Imbue Cloud). */
  isResizeOfferLoading = false;
  loadErrorMessage = "";
  resizeErrorMessage = "";
  /** What the last resize did when nothing else on the page says it: a size recorded for a stopped machine's next start; "" when none. */
  resizeNotice = "";
  isResizeBusy = false;
  /** Whether the restart that applies a recorded size is being dispatched. */
  isRestartBusy = false;
  /** Whether this page dispatched a restart the app has not yet reported finished. */
  isRestartDispatched = false;
  private machineSizeReadCount = 0;
  private cancelMachineSizeRefresh: (() => void) | null = null;
  private isDisposed = false;

  renameErrorMessage = "";
  isRenameSaving = false;
  colorErrorMessage = "";
  /** The latest color pick not yet confirmed saved, shown in place of the saved one; null when none. */
  pendingColor: string | null = null;
  lastSavedColor = "";
  private colorPickCount = 0;
  accountErrorMessage = "";
  isAccountBusy = false;
  destroyErrorMessage = "";
  isDestroyPending = false;
  lifecycleErrorMessage = "";
  isLifecycleBusy = false;

  private readonly fetchJsonImpl: FetchJson;
  private readonly redrawImpl: () => void;
  private readonly shareOverrides: Partial<SharePanelModelOptions>;
  private readonly scheduleRefreshImpl: ScheduleRefresh;

  constructor(
    agentId: string,
    dependencies: {
      fetchJson?: FetchJson;
      redraw?: () => void;
      shareOverrides?: Partial<SharePanelModelOptions>;
      /** How a later re-read of the size is scheduled while the machine restarts (a timer by default). */
      scheduleRefresh?: ScheduleRefresh;
    } = {},
  ) {
    this.agentId = agentId;
    this.fetchJsonImpl = dependencies.fetchJson ?? defaultFetchJson;
    this.redrawImpl = dependencies.redraw ?? m.redraw;
    this.shareOverrides = dependencies.shareOverrides ?? {};
    this.scheduleRefreshImpl =
      dependencies.scheduleRefresh ?? defaultScheduleRefresh;
  }

  /** Whether the machine is restarting: dispatched from this page, or reported running by the app. */
  get isRestartInProgress(): boolean {
    return (
      this.isRestartDispatched ||
      this.machineSize?.is_restart_in_progress === true
    );
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
    this.share = new SharePanelModel({
      hostId: data.host_id || data.agent_id,
      agentId: data.agent_id || this.agentId,
      granterEmail: data.account_email,
      publicEmailDomains: data.public_email_domains,
      wholeService: data.whole_service,
      appServices: data.app_services,
      systemServices: data.system_services,
      serviceLabels: data.service_labels,
      serviceIcons: data.service_icons,
      serviceDisplayNames: data.service_display_names,
      fetchJson: this.fetchJsonImpl,
      redraw: this.redrawImpl,
      ...this.shareOverrides,
    });
    this.status = "ready";
    this.redrawImpl();
    void this.share.load();
    void this.loadMachineSize();
  }

  /**
   * Fetch the machine's size lazily, in two reads when the second is slow:
   * the size itself first, then (for an Imbue Cloud machine) whether the
   * account may change it, which waits on the connector's plan and usage.
   * The section shows the size as soon as the first read lands and a
   * loading state in place of the controls until the second does. A failed
   * read leaves whatever was last known; before anything is known it says
   * whether the size is not reported or could not be read, rather than
   * hiding the section.
   */
  async loadMachineSize(): Promise<void> {
    const readId = ++this.machineSizeReadCount;
    // A new read abandons the previous read's offer fetch, if one is in flight.
    this.isResizeOfferLoading = false;
    const isCurrent = await this.readMachineSize(readId);
    if (!isCurrent) return;
    // The re-read that follows a restart is armed once the whole read has
    // settled, whatever its outcome: a read that fails mid-restart must not
    // strand the page, and a re-read must not overlap the slow offer read
    // (it would supersede it, and the offer would never land).
    if (this.isRestartInProgress && this.cancelMachineSizeRefresh === null) {
      this.cancelMachineSizeRefresh = this.scheduleRefreshImpl(() => {
        this.cancelMachineSizeRefresh = null;
        void this.loadMachineSize();
      });
    }
    this.redrawImpl();
  }

  /** The one or two fetches of a read; resolves to whether `readId` is still the current read. */
  private async readMachineSize(readId: number): Promise<boolean> {
    const sizeUrl = `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/machine-size`;
    const result = await this.fetchJsonImpl(sizeUrl);
    if (!this.isCurrentMachineSizeRead(readId)) return false;
    const size = result.ok ? (result.body as WorkspaceMachineSizeData) : null;
    if (size === null || !size.is_available) {
      if (this.machineSize === null) {
        this.machineSizeState =
          size === null || size.is_read_failed === true
            ? "load_failed"
            : "unavailable";
      } else if (this.machineSize.is_resize_offer_pending === true) {
        // The held size's offer fetch was abandoned when this read started,
        // and this read did not answer: nothing is checking the offer any
        // more, so the size stays read-only rather than "being checked".
        this.machineSize = {
          ...this.machineSize,
          is_resize_offer_pending: false,
        };
      }
      // A size the server cannot show still says whether the app is restarting the machine.
      if (size !== null) this.followRestartReport(size);
      return true;
    }
    this.takeMachineSize(size);
    if (size.is_resize_offer_pending !== true) return true;
    this.isResizeOfferLoading = true;
    this.redrawImpl();
    const offer = await this.fetchJsonImpl(`${sizeUrl}?include_resize_offer=1`);
    if (!this.isCurrentMachineSizeRead(readId)) return false;
    this.isResizeOfferLoading = false;
    const offered = offer.ok ? (offer.body as WorkspaceMachineSizeData) : null;
    if (offered !== null && offered.is_available) {
      this.takeMachineSize(offered);
      return true;
    }
    // An offer that cannot be read leaves the size read-only, as the server
    // itself answers when the account's plan cannot be read; a size the
    // server cannot show still says whether the app is restarting the machine.
    this.takeMachineSize({ ...size, is_resize_offer_pending: false });
    if (offered !== null) this.followRestartReport(offered);
    return true;
  }

  /** Whether a read is still the one whose answer the page shows: not superseded, and the page not left. */
  private isCurrentMachineSizeRead(readId: number): boolean {
    return readId === this.machineSizeReadCount && !this.isDisposed;
  }

  private takeMachineSize(size: WorkspaceMachineSizeData): void {
    this.machineSize = size;
    this.machineSizeState = "ready";
    this.followRestartReport(size);
  }

  /** Let go of the restart once the app reports none running: it registers a
   * restart before it answers the dispatch, so a read that reports none means
   * the one this page dispatched, or the one a held size reported, is over. */
  private followRestartReport(size: WorkspaceMachineSizeData): void {
    if (size.is_restart_in_progress !== false) return;
    this.isRestartDispatched = false;
    if (this.machineSize?.is_restart_in_progress === true) {
      this.machineSize = { ...this.machineSize, is_restart_in_progress: false };
    }
  }

  /**
   * Resize the machine to the draft. Resolves to whether the size was
   * recorded. A local machine that was running is restarted by the app and
   * the section says so; an Imbue Cloud size waits for a restart the section
   * offers; only a stopped local machine's recorded size gets a notice here.
   */
  async resizeMachine(draft: MachineSizeDraft): Promise<boolean> {
    const size = this.machineSize;
    if (size === null) return false;
    this.resizeErrorMessage = "";
    this.resizeNotice = "";
    const draftError = machineSizeDraftError(size, draft);
    const body =
      draftError === null ? machineSizeChangeBody(size, draft) : null;
    if (body === null) {
      this.resizeErrorMessage =
        draftError ??
        (isMachineSizePickedFromChoices(size)
          ? "Pick a different size first."
          : "Enter a new size as whole numbers of at least 1.");
      this.redrawImpl();
      return false;
    }
    this.isResizeBusy = true;
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/machine-size`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    this.isResizeBusy = false;
    if (!result.ok) {
      this.resizeErrorMessage = errorMessageFromBody(
        result.body,
        `Resize failed (HTTP ${result.status})`,
      );
      this.redrawImpl();
      return false;
    }
    const outcome = result.body as MachineSizeChangeResponse;
    // A restart under way, or an Imbue Cloud size waiting for one, is said by
    // the section's own status once the size is re-read; only a size recorded
    // for a stopped local machine has nothing else to announce it.
    if (outcome.is_restart_dispatched) this.isRestartDispatched = true;
    this.resizeNotice =
      outcome.is_restart_dispatched || isMachineSizePickedFromChoices(size)
        ? ""
        : "The new size is recorded. It applies when the machine next starts.";
    // What the page shows is what discovery reports, which follows the record
    // on its next pass; re-read rather than trusting the draft.
    await this.loadMachineSize();
    this.redrawImpl();
    return true;
  }

  /**
   * Restart the machine now so its recorded size takes effect: the same host
   * restart the recovery card dispatches (an Imbue Cloud restart backs the
   * machine's disks up before it comes back). Resolves to whether the restart
   * was accepted; the shell's own surfaces follow it from there.
   */
  async restartMachineToApplySize(): Promise<boolean> {
    this.isRestartBusy = true;
    this.resizeErrorMessage = "";
    this.resizeNotice = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}/restart`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: "host" }),
      },
    );
    this.isRestartBusy = false;
    if (!result.ok) {
      this.resizeErrorMessage = errorMessageFromBody(
        result.body,
        `Could not restart the machine (HTTP ${result.status})`,
      );
      this.redrawImpl();
      return false;
    }
    // The restart was only dispatched (the 202 comes before the stop reaches
    // the connector), and the connector keeps reporting the machine running
    // with its size pending until the stop lands, which is exactly what would
    // offer the restart again. The page remembers the dispatch itself, and
    // re-reads until the app reports the restart over.
    this.isRestartDispatched = true;
    await this.loadMachineSize();
    this.redrawImpl();
    return true;
  }

  dispose(): void {
    this.isDisposed = true;
    this.share?.dispose();
    this.cancelMachineSizeRefresh?.();
    this.cancelMachineSizeRefresh = null;
  }

  async rename(newName: string): Promise<boolean> {
    const trimmed = newName.trim();
    if (!trimmed) {
      this.renameErrorMessage = "A workspace name is required.";
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

  /**
   * Show a color pick at once and save it; resolves once this pick's save has answered.
   *
   * Every pick is sent the moment it is made: the server shows it in every
   * window as soon as it arrives, and writes the picks to the machine one at a
   * time, dropping any a newer pick replaced. Only the latest pick's answer
   * counts here: its failure is reported, and the shown color reverts to the
   * saved one.
   */
  async pickColor(normalizedHex: string): Promise<void> {
    this.colorErrorMessage = "";
    if (this.pendingColor === null && normalizedHex === this.lastSavedColor) {
      this.redrawImpl();
      return;
    }
    const pick = ++this.colorPickCount;
    this.pendingColor = normalizedHex;
    this.redrawImpl();
    const result = await this.fetchJsonImpl(
      `/api/v1/workspaces/${encodeURIComponent(this.agentId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ color: normalizedHex }),
      },
    );
    if (pick !== this.colorPickCount) return;
    if (result.ok) {
      this.lastSavedColor = normalizedHex;
      if (this.data) this.data = { ...this.data, color: normalizedHex };
    } else {
      this.colorErrorMessage = colorErrorMessageFor(result.status, result.body);
    }
    this.pendingColor = null;
    this.redrawImpl();
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
      return "This agent isn't a workspace's primary agent; color can't be set.";
    case "stale_provider":
      return "This workspace is currently unreachable; try again later.";
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
