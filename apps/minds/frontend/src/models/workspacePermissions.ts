// Model for the options panel's Permissions tab: the machine's whole toggle
// tree plus the single-flip writes that replace it.
//
// A flip POSTs ONLY the flipped permission and its new state; the server
// recomputes the affected rule's complete permission set and answers with the
// refreshed FULL view, which is adopted verbatim. The pane therefore renders
// what was actually stored rather than an optimistic guess, at the cost of one
// round trip per flip.
//
// For a remote workspace the server also carries the change to that workspace's
// own machine before answering, and the read fetches that machine's state, so
// both are as slow as the network to it. Nothing here is optimistic: `isWriting`
// locks the whole pane for the duration, so what is on screen is always
// something the machine has confirmed.
//
// Loading is separate from WorkspaceOptionsModel's: an unreachable latchkey
// gateway must not take the Share or Settings tabs down with it.
//
// Add connection has two writes, because a service has two ways of being
// connected: the settings page's browser sign-in, and -- for the services
// latchkey cannot sign in to -- this pane's own credential form. Both end the
// same way, on the connection they produced.

import m from "mithril";
import type {
  FolderSyncConflict,
  FolderSyncState,
  FileSharingAccess,
  UiFolderSyncRow,
  UiFolderSyncs,
  UiFolderSyncDiscardCopyRequest,
  UiFolderSyncRetryRequest,
  UiPathSync,
  UiFolderSyncToggleRequest,
  UiPermissionConnection,
  UiServiceSignIn,
  UiSharedPath,
  UiSharedPathRemoveRequest,
  UiSharedPathRequest,
  UiWorkspacePermissions,
} from "../generated/ui";
import type { FetchJson } from "./workspaceOptions";
import { defaultFetchJson, errorMessageFromBody } from "./workspaceOptions";
import {
  forgetWarmedPermissionsOverview,
  readWarmedPermissionsOverview,
} from "./permissionsPrefetch";

export type PermissionsLoadStatus =
  "idle" | "loading" | "load_failed" | "ready";

export interface PermissionsModelOptions {
  fetchJson?: FetchJson;
  redraw?: () => void;
}

export const WAITING_SECTION = "waiting";
export const ADD_CONNECTION_SECTION = "add-connection";
export const LOCAL_FILES_SECTION = "local-files";
export const OTHER_MACHINES_SECTION = "other-machines";

const FIXED_SECTIONS: readonly string[] = [
  WAITING_SECTION,
  ADD_CONNECTION_SECTION,
  LOCAL_FILES_SECTION,
  OTHER_MACHINES_SECTION,
];

/** Left-nav key for a connection. Keyed by (service, account) rather than by
 * position so a ?section deep link survives a connection being added or
 * revoked out from under it. */
export function connectionSectionId(
  connection: UiPermissionConnection,
): string {
  return `conn:${connection.service_name}:${connection.account}`;
}

/** The section to render: the requested one when it still exists, else the
 * first connection (or Add connection when there are none). */
export function resolvePermissionsSection(
  data: UiWorkspacePermissions | null,
  requested: string | null,
): string {
  const hasWaiting = (data?.waiting_requests ?? []).length > 0;
  // Waiting on you is only a place while something is waiting: answering the
  // last one has to leave, not sit on an empty list.
  if (requested === WAITING_SECTION && hasWaiting) return requested;
  if (
    requested !== null &&
    requested !== WAITING_SECTION &&
    FIXED_SECTIONS.includes(requested)
  ) {
    return requested;
  }
  const connectionIds = (data?.connections ?? []).map(connectionSectionId);
  if (requested !== null && connectionIds.includes(requested)) return requested;
  // Nothing asked for in particular: lead with what is asking for an answer.
  // Everything else in this pane is what past answers built, and none of it is
  // waiting on the reader the way a pending request is.
  if (hasWaiting) return WAITING_SECTION;
  return connectionIds[0] ?? ADD_CONNECTION_SECTION;
}

/** Busy-set identity of one control: at most one write per row is in flight.
 * NUL joins the parts so no combination of names can alias another key. */
export function connectorToggleRowKey(
  scope: string,
  account: string,
  permission: string,
): string {
  return ["connector", scope, account, permission].join("\u0000");
}

export function selfToggleRowKey(permission: string): string {
  return ["self", permission].join("\u0000");
}

export function revokeAllRowKey(serviceName: string, account: string): string {
  return ["revoke-all", serviceName, account].join("\u0000");
}

/** Distinct from revokeAllRowKey on purpose: a revoke-all in flight must not
 * gray out Disconnect, since the two are different actions on different scopes. */
export function disconnectRowKey(serviceName: string, account: string): string {
  return ["disconnect", serviceName, account].join("\u0000");
}

export function connectServiceRowKey(serviceName: string): string {
  return ["connect", serviceName].join("\u0000");
}

/** Busy keys for the controls on one shared path's row. Distinct from each
 * other so a write on one never grays out the others. */
export function folderSyncToggleRowKey(path: string): string {
  return ["folder-sync-toggle", path].join("\u0000");
}

export function pathAccessRowKey(path: string): string {
  return ["path-access", path].join("\u0000");
}

export function pathRemoveRowKey(path: string): string {
  return ["path-remove", path].join("\u0000");
}

export function folderSyncDiscardCopyRowKey(path: string): string {
  return ["folder-sync-discard-copy", path].join("\u0000");
}

export function folderSyncRetryRowKey(path: string): string {
  return ["folder-sync-retry", path].join("\u0000");
}

/** Busy key for adding a path. Not keyed by path, unlike every other write
 * here: there is no row yet to show the spinner on, so the buttons at the foot
 * of the list wear it. */
export const ADD_SHARED_PATH_ROW_KEY = "add-shared-path";

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** A byte count for a person to read, in the units unison measured it in.
 *
 * Powers of 1024 under decimal names, matching what a file manager shows and
 * what `mngr pair` reports; one decimal place above bytes, because a transfer
 * that reads "1.4 GB" is more use than one that reads "1 GB". */
export function formatByteSize(byteCount: number): string {
  let size = byteCount;
  for (const unit of BYTE_UNITS) {
    if (Math.abs(size) < 1024 || unit === BYTE_UNITS[BYTE_UNITS.length - 1]) {
      return unit === "B" ? `${Math.round(size)} ${unit}` : `${size.toFixed(1)} ${unit}`;
    }
    size /= 1024;
  }
  return `${byteCount} B`;
}

/** How far through the transfer running now, when unison said. Empty when it
 * said nothing, which is every state except a transfer in flight. */
export function syncProgressLabel(sync: UiPathSync | null | undefined): string {
  if (!sync || sync.state !== "SYNCING" || !sync.bytes_total) return "";
  return `${formatByteSize(sync.bytes_done ?? 0)} of ${formatByteSize(sync.bytes_total)}`;
}

/** Whether the machine is still holding a copy of a folder whose sync is off.
 * The one state that offers to delete it -- a running sync's files are in use,
 * and a discarded copy is already gone. */
export function hasInactiveCopy(row: UiSharedPath): boolean {
  return row.sync?.activity === "INACTIVE";
}

/** Whether the machine is holding a copy of this folder at all, kept up to
 * date or not. What decides whether removing the shared path takes files with
 * it or only revokes a grant, which is a difference the button has to name. */
export function hasCopyOnMachine(row: UiSharedPath): boolean {
  const activity = row.sync?.activity;
  return activity === "ACTIVE" || activity === "INACTIVE";
}

/** What the agent may do with a shared path. WRITE is a superset of READ, so
 * there are two states, not three -- latchkey has no write-without-read. */
const FILE_SHARING_ACCESS_LABELS: Record<FileSharingAccess, string> = {
  READ: "Read only",
  WRITE: "Read and write",
};

export function fileSharingAccessLabel(access: FileSharingAccess): string {
  return FILE_SHARING_ACCESS_LABELS[access];
}

export const FILE_SHARING_ACCESSES = Object.keys(FILE_SHARING_ACCESS_LABELS) as FileSharingAccess[];

const FOLDER_SYNC_CONFLICT_LABELS: Record<FolderSyncConflict, string> = {
  NEWER: "Whichever was changed more recently",
  THIS_COMPUTER: "The change from your computer",
  WORKSPACE: "The change from this machine",
};

const FOLDER_SYNC_STATE_LABELS: Record<FolderSyncState, string> = {
  STARTING: "Starting",
  RESTARTING: "Restarting",
  SYNCING: "Syncing",
  SYNCED: "Synced",
  DEACTIVATING: "Stopping",
  DISCARDING: "Removing",
  STOPPED: "Stopped",
  UNKNOWN: "Not running",
  FAILED: "Failed",
};

export function folderSyncConflictLabel(conflict: FolderSyncConflict): string {
  return FOLDER_SYNC_CONFLICT_LABELS[conflict];
}

export function folderSyncStateLabel(state: FolderSyncState): string {
  return FOLDER_SYNC_STATE_LABELS[state];
}

/** Every direction and conflict value, in the order the controls offer them.
 * Derived from the label maps so a value added to the wire contract cannot be
 * silently left out of the controls. */
export const FOLDER_SYNC_CONFLICTS = Object.keys(
  FOLDER_SYNC_CONFLICT_LABELS,
) as FolderSyncConflict[];

/** Whether a row's switch reads as on.
 *
 * The destination the user chose, not how far the app has got towards it: a
 * click flips this at once and the status beside it says the rest (Starting,
 * Deactivating, ...). A sync that failed on its own is the exception -- the
 * user still wants it, so the switch stays on and the reason sits beside it. */
export function isPathSyncOn(row: UiSharedPath): boolean {
  return row.sync?.activity === "ACTIVE";
}

/** Whether a row is still going to change on its own, so the pane must keep
 * re-reading it. Liveness, not intent: a sync the user still wants but that
 * failed on its own has nothing left to report, and polling it forever would
 * cost a request every couple of seconds for no news. */
export function isPathSyncLive(row: UiSharedPath): boolean {
  const state = row.sync?.state;
  return (
    state === "STARTING" ||
    state === "RESTARTING" ||
    state === "SYNCING" ||
    state === "SYNCED" ||
    state === "DEACTIVATING" ||
    state === "DISCARDING"
  );
}

/** What connecting a service actually does. Latchkey signs most services in
 * through a browser; the rest (AWS, Coolify, ...) are connected by typing in
 * the credentials they ask for. A service with neither -- no browser sign-in
 * and no command Mind can turn into inputs -- cannot be connected from here,
 * so its row says so rather than opening a form nothing can submit. */
export type ConnectAction =
  "browser_sign_in" | "credential_form" | "unconnectable";

export function connectActionFor(signIn: UiServiceSignIn): ConnectAction {
  if (signIn.is_browser_supported) return "browser_sign_in";
  return signIn.credential_parameters.length > 0
    ? "credential_form"
    : "unconnectable";
}

/** Whether an open credential form carries everything the server needs. Blank
 * values are refused server-side, so the submit waits for all of them. */
export function isCredentialFormComplete(
  signIn: UiServiceSignIn,
  valueByParameterName: Record<string, string>,
  accountName: string,
): boolean {
  if (signIn.credential_parameters.length === 0) return false;
  if (signIn.is_account_name_required && accountName.trim() === "")
    return false;
  return signIn.credential_parameters.every(
    (parameter) => (valueByParameterName[parameter.name] ?? "").trim() !== "",
  );
}

/** Load + write state for one workspace's permission toggles. */
export class PermissionsModel {
  readonly agentId: string;
  status: PermissionsLoadStatus = "idle";
  data: UiWorkspacePermissions | null = null;
  errorMessage = "";
  /** A connection attempt's failure, raised as a popup rather than a line of
   * text above the pane.
   *
   * These read differently from the rest: a service refusing a sign-in explains
   * something only the user can act on somewhere else entirely ("ask an
   * administrator of your Zoom account to grant you the developer privilege"),
   * and it is several lines long. As a thin banner at the top of a pane the
   * user has probably scrolled down, that is easy to miss outright. A load
   * failure or a refused toggle stays inline: the first explains the empty pane
   * it sits on, and the second belongs beside the row that would not move. */
  alertMessage = "";
  /** Service whose credential form is open in Add connection, or null. */
  credentialFormServiceName: string | null = null;
  /** What the user typed into that form, keyed by parameter name. */
  credentialValues: Record<string, string> = {};
  /** Name for the account those credentials will create, when one is needed. */
  credentialAccountName = "";
  /** Why the last credential submission was refused, shown inside the form. */
  credentialErrorMessage = "";
  /** The clash rule a row's sync will start with, until it has one: the control
   * next to an off switch is client state, because there is nothing stored to
   * hold it. Once the sync runs, its own rule is what the row shows. */
  private readonly pendingSyncByPath = new Map<string, FolderSyncConflict>();

  private readonly fetchJsonImpl: FetchJson;
  private readonly redrawImpl: () => void;
  private readonly busyRowKeys = new Set<string>();
  private writeChain: Promise<unknown> = Promise.resolve();
  /** The pending-request set the view on screen was built from, or null before
   * the first reconciliation has anything to compare against. */
  private lastPendingKey: string | null = null;

  constructor(agentId: string, dependencies: PermissionsModelOptions = {}) {
    this.agentId = agentId;
    this.fetchJsonImpl = dependencies.fetchJson ?? defaultFetchJson;
    this.redrawImpl = dependencies.redraw ?? m.redraw;
  }

  /** Start the first read. The tab mounts on every visit, so later calls are
   * no-ops -- reopening the tab must not refetch or blank the pane. */
  ensureLoaded(): void {
    if (this.status !== "idle") return;
    void this.load();
  }

  isRowBusy(rowKey: string): boolean {
    return this.busyRowKeys.has(rowKey);
  }

  /** Whether any write is still in flight.
   *
   * Every write goes to the workspace's own machine and does not answer until
   * it has landed there, so the pane locks while one runs: the acting control
   * spins and everything else grays out. Without that the pane would invite a
   * second click onto a state the first one is still deciding -- and, since
   * the responses are the whole view, the two answers would fight over what is
   * on screen. */
  isWriting(): boolean {
    return this.busyRowKeys.size > 0;
  }

  /** Drop the last write's error. The left nav calls this on a section switch:
   * an error about a row that is no longer on screen is noise. */
  clearErrorMessage(): void {
    if (this.errorMessage === "") return;
    this.errorMessage = "";
    this.redrawImpl();
  }

  /** Dismiss the connection-failure popup. Only the user closes it: it holds
   * something they have to go and act on, so it does not time out or clear
   * itself on the next click the way the inline error does. */
  dismissAlert(): void {
    if (this.alertMessage === "") return;
    this.alertMessage = "";
    this.redrawImpl();
  }

  async load(): Promise<void> {
    this.status = "loading";
    this.errorMessage = "";
    this.redrawImpl();
    // Started when the key was pointed at, so the first open usually spends a
    // read that has already happened rather than watching one. Spent on use:
    // what this shows changes as grants are made and requests arrive, so every
    // later read is its own.
    const warmed = readWarmedPermissionsOverview(this.agentId);
    forgetWarmedPermissionsOverview();
    const result =
      warmed !== null ? await warmed : await this.fetchJsonImpl(this.apiBase());
    if (!result.ok) {
      this.status = "load_failed";
      this.errorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return;
    }
    this.data = result.body as UiWorkspacePermissions;
    this.status = "ready";
    this.redrawImpl();
  }

  /** Drop a request from the pane the moment it is answered.
   *
   * What is pending is the server's to say, and it says so on the next read --
   * but the answer was given HERE, and a row that sits there after it reads as
   * an answer that did not take. So it goes at once, and the refresh that the
   * resolution triggers anyway is what reconciles: if the server still has it
   * pending, it comes back.
   */
  forgetWaitingRequest(requestId: string): void {
    const data = this.data;
    if (data === null) return;
    const waiting = data.waiting_requests.filter(
      (entry) => entry.id !== requestId,
    );
    if (waiting.length === data.waiting_requests.length) return;
    this.data = { ...data, waiting_requests: waiting };
    this.redrawImpl();
  }

  /** Whether anything is still waiting on this machine's reader. Read by the
   * request page on the way out, to decide whether there is a list to go back
   * to; a pane that has not loaded yet has nothing to offer either way. */
  hasWaitingRequests(): boolean {
    return (this.data?.waiting_requests.length ?? 0) > 0;
  }

  /** Re-read the pane when the set of pending requests changes.
   *
   * Resolving a request changes both halves of what this pane shows: the
   * "Waiting on you" strip loses a row, and the permission it granted turns
   * on. Nothing else re-reads -- the panel deliberately stays mounted under
   * the request popup, so the tab is never re-created on the way back -- which
   * left the strip offering rows that answer "no longer available" and toggles
   * showing the state from before the grant.
   *
   * The first call only records what is on screen: the payload was just built,
   * so it already matches. Later changes go through the write chain, so a
   * refresh cannot land out of order with a flip the user made meanwhile.
   */
  async refreshIfPendingChanged(requestIds: readonly string[]): Promise<void> {
    const key = requestIds.join(",");
    if (this.lastPendingKey === null || key === this.lastPendingKey) {
      this.lastPendingKey = key;
      return;
    }
    this.lastPendingKey = key;
    await this.enqueueWrite(() => this.reloadInPlace());
  }

  /** Re-read without blanking the pane, for a refresh the user did not ask for.
   * `load()`'s spinner belongs to an empty pane; here there is a good view on
   * screen and it stays up until the new one arrives. */
  private async reloadInPlace(): Promise<void> {
    const result = await this.fetchJsonImpl(this.apiBase());
    if (!result.ok) {
      // Inline rather than silent: the pane is now knowingly out of date.
      this.errorMessage =
        "Could not refresh permissions: " +
        errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.redrawImpl();
      return;
    }
    this.data = result.body as UiWorkspacePermissions;
    this.errorMessage = "";
    this.redrawImpl();
  }

  async toggleConnector(
    scope: string,
    account: string,
    permission: string,
    enabled: boolean,
  ): Promise<void> {
    await this.writeFlip(
      connectorToggleRowKey(scope, account, permission),
      "connector-toggle",
      { scope, account, permission, enabled },
      "Could not save the change: ",
    );
  }

  async toggleSelf(permission: string, enabled: boolean): Promise<void> {
    await this.writeFlip(
      selfToggleRowKey(permission),
      "self-toggle",
      { permission, enabled },
      "Could not save the change: ",
    );
  }

  async revokeAll(
    serviceName: string,
    account: string,
    serviceLabel: string,
  ): Promise<void> {
    await this.writeFlip(
      revokeAllRowKey(serviceName, account),
      "connector-revoke-all",
      { service_name: serviceName, account },
      `Could not revoke ${serviceLabel}: `,
    );
  }

  /** Disconnect this account from latchkey, then land the pane somewhere that
   * still exists.
   *
   * Unlike Revoke all, this clears the stored credential itself -- the one held
   * by the store this machine reads, which is its own when it has one and this
   * computer's (shared by every local machine) when it does not -- and the
   * server strips this workspace's now-inert grants. The connection therefore
   * leaves the refreshed view entirely, and the section it occupied has to be
   * given up.
   *
   * Resolves to the section to show next, or null when the write was refused
   * and the pane must stay where it is. */
  async disconnect(connection: UiPermissionConnection): Promise<string | null> {
    const sectionId = connectionSectionId(connection);
    const isDisconnected = await this.writeFlip(
      disconnectRowKey(connection.service_name, connection.account),
      "connector-disconnect",
      { service_name: connection.service_name, account: connection.account },
      `Could not disconnect ${connection.account_label} from ${connection.display_name}: `,
    );
    if (!isDisconnected) return null;
    return resolvePermissionsSection(this.data, sectionId);
  }

  /** Run the blocking connector sign-in for a service, then re-read the pane
   * so the new connection arrives with its toggles.
   *
   * Resolves to the section id of the connection this sign-in added, so the
   * caller can show it. A service may already own other accounts, so the added
   * one is identified by the ids that were not there before; when the reload
   * carries no new connection for the service the answer is null rather than
   * some other account of the same service. */
  async connectService(serviceName: string): Promise<string | null> {
    const rowKey = connectServiceRowKey(serviceName);
    if (this.busyRowKeys.has(rowKey)) return null;
    this.busyRowKeys.add(rowKey);
    this.errorMessage = "";
    this.alertMessage = "";
    this.redrawImpl();
    const sectionIdsBefore = new Set(
      (this.data?.connections ?? []).map(connectionSectionId),
    );
    const result = await this.enqueueWrite(() =>
      this.fetchJsonImpl(`${this.apiBase()}/connect-browser`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ service_name: serviceName }),
      }),
    );
    this.busyRowKeys.delete(rowKey);
    if (!result.ok) {
      this.alertMessage =
        "Could not connect: " +
        errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.redrawImpl();
      return null;
    }
    // In the same queue as the flips: the sign-in blocks for seconds while the
    // rest of the pane stays live, so an un-chained re-read could land after a
    // toggle made meanwhile and put its old state back on screen.
    await this.enqueueWrite(() => this.reloadInPlace());
    const added = (this.data?.connections ?? []).find(
      (connection) =>
        connection.service_name === serviceName &&
        !sectionIdsBefore.has(connectionSectionId(connection)),
    );
    return added === undefined ? null : connectionSectionId(added);
  }

  /** Open (or reopen) a service's credential form, empty. */
  openCredentialForm(serviceName: string): void {
    this.resetCredentialForm();
    this.credentialFormServiceName = serviceName;
    this.errorMessage = "";
    this.redrawImpl();
  }

  closeCredentialForm(): void {
    this.resetCredentialForm();
    this.redrawImpl();
  }

  /** Store what the open credential form carries, then land on the connection
   * it created -- the same ending as a completed browser sign-in.
   *
   * A refusal is reported inside the form rather than pane-wide, and nothing
   * the user typed is cleared: a rejected credential is usually one field away
   * from being right. */
  async connectWithCredentials(serviceName: string): Promise<string | null> {
    const rowKey = connectServiceRowKey(serviceName);
    if (this.busyRowKeys.has(rowKey)) return null;
    this.busyRowKeys.add(rowKey);
    this.credentialErrorMessage = "";
    this.errorMessage = "";
    this.redrawImpl();
    const sectionIdsBefore = new Set(
      (this.data?.connections ?? []).map(connectionSectionId),
    );
    const result = await this.enqueueWrite(() =>
      this.fetchJsonImpl(`${this.apiBase()}/connect-credentials`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          service_name: serviceName,
          value_by_parameter_name: this.credentialValues,
          account_name: this.credentialAccountName,
        }),
      }),
    );
    this.busyRowKeys.delete(rowKey);
    if (!result.ok) {
      this.credentialErrorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return null;
    }
    // The write answers with the refreshed view, so the new connection arrives
    // with the response rather than needing a re-read.
    this.data = result.body as UiWorkspacePermissions;
    this.status = "ready";
    this.resetCredentialForm();
    this.redrawImpl();
    const added = (this.data.connections ?? []).find(
      (connection) =>
        connection.service_name === serviceName &&
        !sectionIdsBefore.has(connectionSectionId(connection)),
    );
    return added === undefined ? null : connectionSectionId(added);
  }

  /** The clash rule this row's sync should use.
   *
   * A choice the user has made on this row wins over what the running sync
   * currently uses -- that is exactly the case where the two differ, and the
   * running one is about to be restarted onto it. With no choice made, the
   * running sync's own rule is the answer, and the default before that.
   *
   * Only the clash rule: which way changes travel follows the access, and the
   * server derives it there rather than taking a value that could disagree. */
  syncConflictFor(row: UiSharedPath): FolderSyncConflict {
    const pending = this.pendingSyncByPath.get(row.path);
    if (pending !== undefined) return pending;
    return row.sync?.conflict ?? "NEWER";
  }

  async setSyncConflict(
    row: UiSharedPath,
    conflict: FolderSyncConflict,
  ): Promise<void> {
    this.pendingSyncByPath.set(row.path, conflict);
    this.redrawImpl();
    await this.restartSyncIfRunning(row);
  }

  /** Carry a changed setting to a sync that is already running.
   *
   * One write, not an off and an on. The server compares the settings it was
   * given with the ones the running sync has and replaces just the process;
   * turning the sync off first would rename the machine's copy out of the way
   * and straight back again -- two round trips and two renames to change a
   * flag. */
  private async restartSyncIfRunning(row: UiSharedPath): Promise<void> {
    if (!isPathSyncLive(row)) return;
    await this.toggleSync(row, true);
  }

  /** Turn syncing on or off for one shared path.
   *
   * Never dropped, unlike the other row writes. This one records where the
   * folder should end up rather than doing the work, and the server coalesces:
   * clicking back and forth while a move is in flight settles on the last
   * click. Dropping one here would instead leave the folder somewhere the user
   * did not choose, with the radio showing the click that happened to win. */
  async toggleSync(row: UiSharedPath, enabled: boolean): Promise<void> {
    // No direction: it follows the access the agent was granted, and the
    // server derives it there rather than taking one that could disagree.
    const body: UiFolderSyncToggleRequest = {
      path: row.path,
      enabled,
      conflict: this.syncConflictFor(row),
    };
    try {
      this.data = await this.enqueueWrite(() => this.post("toggle", body, this.folderSyncBase()));
      this.status = "ready";
      this.errorMessage = "";
    } catch (error) {
      this.errorMessage =
        (enabled ? "Could not start syncing: " : "Could not stop syncing: ") +
        (error instanceof Error ? error.message : String(error));
    }
    this.redrawImpl();
  }


  /** Bring a failed sync up again.
   *
   * Its own action rather than a second meaning for the checkbox: the checkbox
   * is already on, so there is no flip to send, and flipping it off and back on
   * to retry would set the machine's copy aside and fetch it again for nothing.
   */
  async retrySync(row: UiSharedPath): Promise<void> {
    const body: UiFolderSyncRetryRequest = { path: row.path };
    await this.writeFlip(
      folderSyncRetryRowKey(row.path),
      "retry",
      body,
      "Could not start syncing again: ",
      this.folderSyncBase(),
    );
  }

  /** Delete the copy the machine kept when this folder's sync was turned off.
   * The only step here that cannot be undone, which is why it is its own
   * action rather than part of turning the switch off. */
  async discardCopy(row: UiSharedPath): Promise<void> {
    const body: UiFolderSyncDiscardCopyRequest = { path: row.path };
    await this.writeFlip(
      folderSyncDiscardCopyRowKey(row.path),
      "discard-copy",
      body,
      "Could not delete that copy: ",
      this.folderSyncBase(),
    );
  }

  /** Share a new path, or change the access on one already shared. */
  async setSharedPath(path: string, access: FileSharingAccess): Promise<void> {
    const body: UiSharedPathRequest = { path, access };
    await this.writeFlip(
      pathAccessRowKey(path),
      "shared-path",
      body,
      "Could not share that path: ",
    );
  }

  /** Share a path that is not on the list yet.
   *
   * The same write as changing a row's access, under a different busy key: an
   * access change has a row to show it on, and this one does not until the
   * write comes back with it. */
  async addSharedPath(path: string): Promise<void> {
    const body: UiSharedPathRequest = { path, access: "READ" };
    await this.writeFlip(
      ADD_SHARED_PATH_ROW_KEY,
      "shared-path",
      body,
      "Could not share that path: ",
    );
  }

  /** Stop sharing a path. Its row leaves with the refreshed view. */
  async removeSharedPath(path: string): Promise<void> {
    const body: UiSharedPathRemoveRequest = { path };
    await this.writeFlip(
      pathRemoveRowKey(path),
      "shared-path-remove",
      body,
      "Could not stop sharing: ",
    );
  }

  /** Re-read the pane while any sync is alive, so its status stays current.
   *
   * A sync's state changes in a subprocess this pane knows nothing about, and
   * it moves both ways: starting, then between syncing and synced every time
   * something is carried across. Watching only the starting edge left a row
   * frozen on "Synced" through every later transfer.
   *
   * The condition is what bounds the cost: with no live sync on screen this is
   * a no-op, and the pane is only mounted while it is being looked at. */
  async refreshLiveFolderSyncs(): Promise<void> {
    if (this.status !== "ready") return;
    const rows = this.data?.shared_paths ?? [];
    if (!rows.some((row) => isPathSyncLive(row))) return;
    await this.enqueueWrite(() => this.reloadFolderSyncs());
  }

  /** Re-read only the sync halves, from the endpoint that answers without
   * leaving this computer.
   *
   * Deliberately not the whole pane. Everything a running sync reports is
   * already here -- its `mngr pair` subprocess tells us -- while the rest of
   * the view is the workspace machine's own policy, fetched over SSH in about
   * a second. Polling that put a remote round trip between the user and every
   * click, which is also what makes the once-a-second tick affordable now. A
   * failure is swallowed: the badge goes stale, which is not worth an error
   * banner over a view the user did not ask to refresh. */
  private async reloadFolderSyncs(): Promise<void> {
    const result = await this.fetchJsonImpl(this.folderSyncBase());
    const current = this.data;
    if (!result.ok || current === null) return;
    const byPath = new Map<string, UiFolderSyncRow>(
      ((result.body as UiFolderSyncs).rows ?? []).map((row) => [row.path, row]),
    );
    this.data = {
      ...current,
      shared_paths: (current.shared_paths ?? []).map((row) => {
        const fresh = byPath.get(row.path);
        if (fresh === undefined) return row;
        // The overlap warning rides along because it is about what *other*
        // workspaces are doing, which this pane cannot otherwise learn: a full
        // read only happens on open and after a write here, so a pane sitting
        // on a poll would keep a warning that stopped being true, or miss one
        // that started.
        return { ...row, sync: fresh.sync ?? null, sync_overlap_warning: fresh.overlap_warning ?? "" };
      }),
    };
    this.redrawImpl();
  }

  private resetCredentialForm(): void {
    this.credentialFormServiceName = null;
    this.credentialValues = {};
    this.credentialAccountName = "";
    this.credentialErrorMessage = "";
  }

  /** Resolves to whether the refreshed view was adopted, so a caller that has
   * to act on the result (disconnect, which gives up its section) can tell a
   * stored change from a refused or dropped one. */
  private async writeFlip(
    rowKey: string,
    path: string,
    body: unknown,
    errorPrefix: string,
    baseUrl: string = this.apiBase(),
  ): Promise<boolean> {
    // A second click while this row's write runs is dropped rather than
    // queued, so the stored state never races itself.
    if (this.busyRowKeys.has(rowKey)) return false;
    this.busyRowKeys.add(rowKey);
    this.errorMessage = "";
    this.redrawImpl();
    try {
      this.data = await this.enqueueWrite(() => this.post(path, body, baseUrl));
      this.status = "ready";
    } catch (error) {
      // The last good view stays on screen: a refused write changed nothing.
      this.errorMessage =
        errorPrefix + (error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      this.busyRowKeys.delete(rowKey);
      this.redrawImpl();
    }
    return true;
  }

  private async post(
    path: string,
    body: unknown,
    baseUrl: string = this.apiBase(),
  ): Promise<UiWorkspacePermissions> {
    const result = await this.fetchJsonImpl(`${baseUrl}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!result.ok)
      throw new Error(
        errorMessageFromBody(result.body, `HTTP ${result.status}`),
      );
    return result.body as UiWorkspacePermissions;
  }

  /** Serialize writes: overlapping flips reach the server in click order and
   * the last response is the one left on screen.
   *
   * Every write also drops the module's warm, which is a read the user's own
   * change has just invalidated. Twice, because both ends matter: a warm taken
   * before the click would otherwise be replayed by a reopen while the write
   * is still running (the browser sign-in blocks for as long as the sign-in
   * takes), and one started *during* the write answers from before it landed.
   * Either would put the pre-change state back on the next open, and the warm
   * is deduplicated for its whole lifetime, so it would also suppress the
   * fresh read that open would otherwise make.
   */
  private enqueueWrite<T>(makeRequest: () => Promise<T>): Promise<T> {
    forgetWarmedPermissionsOverview();
    const request = (): Promise<T> => {
      const pending = makeRequest();
      // Settled on a side branch rather than awaited, so dropping the warm adds
      // no hop to the chain the responses come back through.
      void pending.then(forgetWarmedPermissionsOverview, forgetWarmedPermissionsOverview);
      return pending;
    };
    const result = this.writeChain.then(request, request);
    this.writeChain = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private apiBase(): string {
    return `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/permissions`;
  }

  /** Where the sync routes live. A separate resource from the grants above
   * them, even though one pane draws both: a sync is not a permission, and
   * none of these three touch latchkey. They still answer with the whole
   * permissions payload, which is why they share this model. */
  private folderSyncBase(): string {
    return `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/folder-syncs`;
  }
}
