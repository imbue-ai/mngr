// Model for the workspace options panel + settings page: the options-data
// load, the Machine settings actions (rename / color / account / lifecycle),
// and the share panel's own model, which this load constructs.

import m from "mithril";
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
  is_leased_imbue_cloud: boolean;
  /** The account a leased machine belongs to; '' when not leased or not known. */
  leased_owner_email: string;
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
  /** The mail providers a domain grant may never name, so the panel can refuse
   * one before the row. */
  public_email_domains: string[];
  /** "" when discovery reports no SSH endpoint for the machine. */
  ssh_command: string;
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

/** Load + action state for the Machine settings panes. */
export class WorkspaceOptionsModel {
  readonly agentId: string;
  status: "loading" | "load_failed" | "ready" = "loading";
  data: WorkspaceOptionsData | null = null;
  share: SharePanelModel | null = null;
  /** Read-only machine size for leased machines; null until (and unless) it loads. */
  machineSize: WorkspaceMachineSizeData | null = null;
  loadErrorMessage = "";

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

  constructor(
    agentId: string,
    dependencies: {
      fetchJson?: FetchJson;
      redraw?: () => void;
      shareOverrides?: Partial<SharePanelModelOptions>;
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
    this.share = new SharePanelModel({
      hostId: data.host_id || data.agent_id,
      agentId: data.agent_id || this.agentId,
      granterEmail: data.account_email,
      publicEmailDomains: data.public_email_domains,
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
