// The recipient's side of sharing (specs/shared-with-me/spec.md, 4.7): the
// desktop routes the "Shared with me" rows go through, and the pure helpers
// the home page and the shell read a shared row with.
//
// A shared row is a `UiWorkspaceEntry` with `is_owned === false` and a
// `share`; it is someone else's workspace, so the only things the app does
// with it are open, decline, report, retry, and remove.

import type { UiWorkspaceEntry } from "../channel/messages";
import type { UiSharedWorkspace, UiSharedWorkspaceTarget } from "../generated/ui";

export type SharedVerdict =
  "admitted" | "refused" | "misconfigured" | "offline" | "sign_in_failed";

/** The route alias prefix of a shared row whose share key is not a workspace
 * coordinate: a pasted link's key is a bare digest, and the workspace routes
 * accept only `agent-`, `host-`, or `shared-` ids. */
const SHARED_ROUTE_PREFIX = "shared-";
const COORDINATE_RE = /^(?:agent|host)-[a-f0-9]+$/i;

/** A workspace-list entry that is a share: its `share` is present. */
export type SharedEntry = UiWorkspaceEntry & { share: UiSharedWorkspace };

/** Whether the entry is a workspace shared with the window's account. */
export function isSharedEntry(entry: UiWorkspaceEntry): entry is SharedEntry {
  return (
    entry.is_owned === false &&
    entry.share !== null &&
    entry.share !== undefined
  );
}

/** The account's own rows: every entry that is not shared with it. The
 * pickers that act on a workspace as its owner (the sidebar switcher, the
 * template target list) read these; a shared row is opened only from the
 * home page's own section, through `openSharedWorkspace`. */
export function ownedWorkspaceEntries(
  entries: readonly UiWorkspaceEntry[],
): UiWorkspaceEntry[] {
  return entries.filter((entry) => !isSharedEntry(entry));
}

/** The id a shared row's workspace route carries: its share key when that is a
 * coordinate, else the `shared-<key>` alias the route regexes accept. */
export function sharedRouteId(shareKey: string): string {
  return COORDINATE_RE.test(shareKey)
    ? shareKey
    : `${SHARED_ROUTE_PREFIX}${shareKey}`;
}

/** Whether a workspace route id is the `shared-<key>` alias of a shared row. */
export function isSharedRouteId(routeId: string): boolean {
  return routeId.startsWith(SHARED_ROUTE_PREFIX);
}

/** The share key a workspace route id names: the alias stripped, else the id itself. */
export function shareKeyOfRouteId(routeId: string): string {
  return routeId.startsWith(SHARED_ROUTE_PREFIX)
    ? routeId.slice(SHARED_ROUTE_PREFIX.length)
    : routeId;
}

/** What a tile calls the owner: their display name, else their verified email, else "someone". */
export function ownerLabel(share: UiSharedWorkspace): string {
  return share.owner.display_name || share.owner.email || "someone";
}

/** The status a tile shows for the share. */
export function statusLabel(share: UiSharedWorkspace): string {
  if (share.is_manual) return "Pasted link";
  switch (share.status) {
    case "invited":
      return "Invitation";
    case "accepted":
      return "Accepted";
    case "declined":
      return "Declined";
    default:
      return "Shared";
  }
}

// How long a tunnel may go unstamped before the tile calls it offline: twice
// the connector's fifteen-minute stamp interval.
const OFFLINE_AFTER_MS = 30 * 60 * 1000;

/** Whether the share's tunnel was seen alive recently; null when never stamped (nothing to say). */
export function isShareLive(
  share: UiSharedWorkspace,
  nowMs: number,
): boolean | null {
  if (!share.last_seen_at) return null;
  const seenMs = Date.parse(share.last_seen_at);
  if (Number.isNaN(seenMs)) return null;
  return nowMs - seenMs <= OFFLINE_AFTER_MS;
}

/** The one-line reading of the last open's verdict, or null before any open. */
export function verdictLabel(share: UiSharedWorkspace): string | null {
  return verdictLabelFor(share.verdict ?? null);
}

/** The one-line reading of a verdict (what the row and the frame's overlay show); null for admitted or none. */
export function verdictLabelFor(verdict: string | null): string | null {
  switch (verdict) {
    case "admitted":
      return null;
    case "refused":
      return "No longer shared with you";
    case "misconfigured":
      return "The owner's sharing is misconfigured";
    case "offline":
      return "Could not reach the workspace";
    case "sign_in_failed":
      return "Could not sign in to open it";
    default:
      return null;
  }
}

/** The target to open by default: the shell when it has a link, else the first target with one. */
export function primaryLink(share: UiSharedWorkspace): string | null {
  const shell = share.targets.find(
    (target) => target.app === "system_interface",
  );
  if (shell?.link) return shell.link;
  return share.targets.find((target) => target.link)?.link ?? null;
}

/** Whether a frame document's host is one of the share's origins (a label under its domain, or the domain). */
export function isHostOfShare(
  hostname: string,
  share: UiSharedWorkspace,
): boolean {
  const host = hostname.toLowerCase();
  const domain = share.workspace_domain.toLowerCase();
  return host === domain || host.endsWith(`.${domain}`);
}

/** The shared row whose domain the host belongs to, or null. */
export function sharedEntryForHost(
  entries: readonly UiWorkspaceEntry[],
  hostname: string,
): UiWorkspaceEntry | null {
  return (
    entries.find(
      (entry) => isSharedEntry(entry) && isHostOfShare(hostname, entry.share),
    ) ?? null
  );
}

/** What the `open` route answers: where to load the target and how to sign the partition in first. */
export interface SharedOpenPlan {
  share_key: string;
  host_id: string;
  link: string;
  redeem_url: string | null;
  status: string;
}

interface SharedRowResponse {
  share_key: string;
  row: UiSharedWorkspace | null;
}

/** What the `remove` route answers: a manual entry is removed outright, a listed share is declined. */
interface SharedRemoveResponse extends SharedRowResponse {
  outcome: "removed" | "declined";
}

interface ApiErrorBody {
  error?: string;
  code?: string;
}

/** A refusal the desktop answered with a code the page can act on. */
export class SharedWithMeError extends Error {
  readonly code: string | null;
  readonly status: number;

  constructor(message: string, code: string | null, status: number) {
    super(message);
    this.name = "SharedWithMeError";
    this.code = code;
    this.status = status;
  }
}

async function call<T>(
  method: string,
  path: string,
  accountId: string,
  body?: unknown,
): Promise<T> {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(
    `${path}${separator}account=${encodeURIComponent(accountId)}`,
    {
      method,
      credentials: "same-origin",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
  );
  if (!response.ok) {
    const parsed = await response.json().then(
      (body) => body as ApiErrorBody,
      () => ({}) as ApiErrorBody,
    );
    throw new SharedWithMeError(
      parsed.error ?? `${method} ${path} failed (${response.status})`,
      parsed.code ?? null,
      response.status,
    );
  }
  return (await response.json()) as T;
}

const BASE = "/ui/api/shared-with-me";

/** What the refresh route answers: the account's rows as refreshed. */
export interface RefreshedSharedListing {
  account_id: string;
  shares: Pick<UiSharedWorkspace, "share_key" | "host_id">[];
}

export const sharedWithMeApi = {
  refresh(accountId: string): Promise<RefreshedSharedListing> {
    return call("POST", `${BASE}/refresh`, accountId);
  },
  open(
    accountId: string,
    shareKey: string,
    app: string | null,
  ): Promise<SharedOpenPlan> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/open`,
      accountId,
      app === null ? {} : { app },
    );
  },
  decline(accountId: string, shareKey: string): Promise<SharedRowResponse> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/decline`,
      accountId,
    );
  },
  undoDecline(accountId: string, shareKey: string): Promise<SharedRowResponse> {
    return call(
      "DELETE",
      `${BASE}/${encodeURIComponent(shareKey)}/decline`,
      accountId,
    );
  },
  report(accountId: string, shareKey: string): Promise<SharedRowResponse> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/report`,
      accountId,
    );
  },
  retry(accountId: string, shareKey: string): Promise<SharedRowResponse> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/retry`,
      accountId,
    );
  },
  remove(accountId: string, shareKey: string): Promise<SharedRemoveResponse> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/remove`,
      accountId,
    );
  },
  recordVerdict(
    accountId: string,
    shareKey: string,
    verdict: SharedVerdict,
  ): Promise<SharedRowResponse> {
    return call(
      "POST",
      `${BASE}/${encodeURIComponent(shareKey)}/verdict`,
      accountId,
      { verdict },
    );
  },
  addLink(
    accountId: string,
    link: string,
    label: string,
  ): Promise<{ row: UiSharedWorkspace }> {
    return call("POST", `${BASE}/links`, accountId, { link, label });
  },
};

/** The words a row shows for the refusals a code names; the desktop's own message for every other failure. */
const SHARED_REFUSAL_MESSAGES: Record<string, string> = {
  already_reported: "You reported this share; a report cannot be undone.",
  rate_limited: "Too many reports today; try again tomorrow.",
};

export function sharedActionErrorMessage(error: unknown): string {
  if (error instanceof SharedWithMeError && error.code !== null) {
    const named = SHARED_REFUSAL_MESSAGES[error.code];
    if (named !== undefined) return named;
  }
  return error instanceof Error ? error.message : String(error);
}

/** The confirmation a report asks for, in the words the tile shows. */
export const REPORT_CONFIRMATION =
  "Report this share as unwanted? This counts as a strike against the owner's account, hides everything they share with you, and cannot be undone.";

// How the home page reads a shared row (specs/home-screen-redesign, D5 to D8).

/** The first hostname label of a share link, lower-cased, or null for a link that is not one. */
export function pastedLinkLabel(link: string): string | null {
  try {
    const hostname = new URL(link).hostname.toLowerCase();
    const label = hostname.split(".")[0];
    return label === "" ? null : label;
  } catch {
    return null;
  }
}

/** Whether an origin label is the shell's: `system-interface-<rand>`, or the legacy underscore spelling. */
export function isShellLabel(label: string): boolean {
  return label.startsWith("system-interface-") || label.startsWith("system_interface-");
}

// An origin label is `<name>-<8 random characters>`; the name is what is left
// once the suffix is cut off.
const LABEL_SUFFIX_LENGTH = 9;

/** The app name an origin label was minted from (the label without its random suffix). */
export function pastedLinkAppName(label: string): string {
  return label.length > LABEL_SUFFIX_LENGTH ? label.slice(0, -LABEL_SUFFIX_LENGTH) : label;
}

/** Whether the share is a whole workspace (a card) rather than one or more apps (icons). A pasted link is read
 * by its label: the shell's label is a workspace, any other an app. */
export function isWorkspaceShare(share: UiSharedWorkspace): boolean {
  if (share.is_manual) {
    const link = share.targets.find((target) => target.link)?.link ?? null;
    const label = link === null ? null : pastedLinkLabel(link);
    return label === null || isShellLabel(label);
  }
  return share.targets.some((target) => target.app === "system_interface");
}

/** A listed share the account has not answered: an invitation to join or decline. */
export function isInvitation(share: UiSharedWorkspace): boolean {
  return !share.is_manual && share.status === "invited";
}

/** A listed share the account declined: hidden from the lists, undoable from the Invitations tab. */
export function isDeclinedInvitation(share: UiSharedWorkspace): boolean {
  return !share.is_manual && share.status === "declined";
}

/** The `app` an open of `target` names: null for the shell (the whole workspace), else the app itself. */
export function openTargetApp(target: UiSharedWorkspaceTarget): string | null {
  return target.app === "system_interface" ? null : target.app;
}

/** Whether Retry (forget the last verdict and open again) is offered: the share reads offline, or its last
 * verdict was anything but admitted. */
export function isRetryOffered(share: UiSharedWorkspace, isOffline: boolean): boolean {
  return isOffline || (share.verdict !== null && share.verdict !== undefined && share.verdict !== "admitted");
}

/** The target a plain open of the share goes to: the shell when it has a link, else the first linked target. */
export function primaryTarget(share: UiSharedWorkspace): UiSharedWorkspaceTarget | null {
  const shell = share.targets.find((target) => target.app === "system_interface");
  if (shell?.link) return shell;
  return share.targets.find((target) => target.link) ?? share.targets[0] ?? null;
}

/** One app shared with the account, as the Apps tab draws it. */
export interface SharedApp {
  entry: UiWorkspaceEntry;
  share: UiSharedWorkspace;
  target: UiSharedWorkspaceTarget;
  /** What the icon is called: the target's display name, else the app name the pasted link was minted from. */
  name: string;
}

/** The apps a shared row puts on the Apps tab: every target of an app share (a pasted link's named by its label), and,
 * on a workspace share, the apps a grant names in their own right, which are icons as well as part of the card. */
export function sharedAppsOf(entry: UiWorkspaceEntry): SharedApp[] {
  if (!isSharedEntry(entry)) return [];
  const share = entry.share;
  const targets = isWorkspaceShare(share)
    ? share.targets.filter((target) => target.app !== "system_interface" && target.is_granted_as_app === true)
    : share.targets;
  return targets.map((target) => {
    const label = target.link === null || target.link === undefined ? null : pastedLinkLabel(target.link);
    const labelName = label === null ? target.app : pastedLinkAppName(label);
    return {
      entry,
      share,
      target,
      name: share.is_manual ? labelName : target.display_name || target.app,
    };
  });
}

/** Newest `latest_grant_at` first; a share with none sorts last. */
export function byNewestGrant(a: SharedEntry, b: SharedEntry): number {
  return (b.share.latest_grant_at ?? "").localeCompare(a.share.latest_grant_at ?? "");
}

/** The shared rows sorted onto the home page's tabs (specs/home-screen-redesign, D5 to D8). */
export interface HomeSharedRows {
  /** Listed shares still unanswered, newest grant first. */
  invitations: SharedEntry[];
  /** Declined shares holding their place on the Invitations tab until it is left. */
  pendingDeclined: SharedEntry[];
  /** Declined shares filed under the declined disclosure. */
  settledDeclined: SharedEntry[];
  /** Accepted or pasted shares of a whole workspace: guest cards. */
  workspaceShares: SharedEntry[];
  /** One icon per target of an accepted or pasted app share, and per directly granted app of a workspace share. */
  sharedApps: SharedApp[];
}

/** Sort the workspace list's shared rows onto the home page's tabs; owned rows are left out. */
export function homeSharedRows(
  entries: readonly UiWorkspaceEntry[],
  pendingDeclinedKeys: ReadonlySet<string>,
): HomeSharedRows {
  const shared = entries.filter(isSharedEntry);
  const declined = shared.filter((entry) => isDeclinedInvitation(entry.share));
  const accepted = shared.filter((entry) => !isInvitation(entry.share) && !isDeclinedInvitation(entry.share));
  return {
    invitations: shared.filter((entry) => isInvitation(entry.share)).sort(byNewestGrant),
    pendingDeclined: declined.filter((entry) => pendingDeclinedKeys.has(entry.share.share_key)),
    settledDeclined: declined.filter((entry) => !pendingDeclinedKeys.has(entry.share.share_key)),
    workspaceShares: accepted.filter((entry) => isWorkspaceShare(entry.share)),
    sharedApps: accepted.flatMap((entry) => sharedAppsOf(entry)),
  };
}

/** "just now", "5m ago", "2h ago", "3d ago", else the date; "" for no or an unreadable time. */
export function relativeTimeLabel(iso: string, nowMs: number): string {
  if (!iso) return "";
  const thenMs = Date.parse(iso);
  if (Number.isNaN(thenMs)) return "";
  const minutes = Math.floor((nowMs - thenMs) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d ago`;
  return new Date(thenMs).toLocaleDateString();
}
