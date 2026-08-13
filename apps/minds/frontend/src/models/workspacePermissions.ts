// Model for the options panel's Permissions tab: the machine's whole toggle
// tree plus the single-flip writes that replace it.
//
// A flip POSTs ONLY the flipped permission and its new state; the server
// recomputes the affected rule's complete permission set and answers with the
// refreshed FULL view, which is adopted verbatim. The pane therefore renders
// what was actually stored rather than an optimistic guess, at the cost of one
// round trip per flip.
//
// Loading is separate from WorkspaceOptionsModel's: an unreachable latchkey
// gateway must not take the Share or Settings tabs down with it.
//
// Add connection has two writes, because a service has two ways of being
// connected: the settings page's browser sign-in, and -- for the services
// latchkey cannot sign in to -- this pane's own credential form. Both end the
// same way, on the connection they produced.

import m from "mithril";
import type { UiPermissionConnection, UiServiceSignIn, UiWorkspacePermissions } from "../generated/ui";
import type { FetchJson } from "./workspaceOptions";
import { defaultFetchJson, errorMessageFromBody } from "./workspaceOptions";

export type PermissionsLoadStatus = "idle" | "loading" | "load_failed" | "ready";

export interface PermissionsModelOptions {
  fetchJson?: FetchJson;
  redraw?: () => void;
}

export const ADD_CONNECTION_SECTION = "add-connection";
export const LOCAL_FILES_SECTION = "local-files";
export const OTHER_MACHINES_SECTION = "other-machines";

const FIXED_SECTIONS: readonly string[] = [
  ADD_CONNECTION_SECTION,
  LOCAL_FILES_SECTION,
  OTHER_MACHINES_SECTION,
];

/** Left-nav key for a connection. Keyed by (service, account) rather than by
 * position so a ?section deep link survives a connection being added or
 * revoked out from under it. */
export function connectionSectionId(connection: UiPermissionConnection): string {
  return `conn:${connection.service_name}:${connection.account}`;
}

/** The section to render: the requested one when it still exists, else the
 * first connection (or Add connection when there are none). */
export function resolvePermissionsSection(
  data: UiWorkspacePermissions | null,
  requested: string | null,
): string {
  if (requested !== null && FIXED_SECTIONS.includes(requested)) return requested;
  const connectionIds = (data?.connections ?? []).map(connectionSectionId);
  if (requested !== null && connectionIds.includes(requested)) return requested;
  return connectionIds[0] ?? ADD_CONNECTION_SECTION;
}

/** Busy-set identity of one control: at most one write per row is in flight.
 * NUL joins the parts so no combination of names can alias another key. */
export function connectorToggleRowKey(scope: string, account: string, permission: string): string {
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

/** What connecting a service actually does. Latchkey signs most services in
 * through a browser; the rest (AWS, Coolify, ...) are connected by typing in
 * the credentials they ask for. A service with neither -- no browser sign-in
 * and no command Minds can turn into inputs -- cannot be connected from here,
 * so its row says so rather than opening a form nothing can submit. */
export type ConnectAction = "browser_sign_in" | "credential_form" | "unconnectable";

export function connectActionFor(signIn: UiServiceSignIn): ConnectAction {
  if (signIn.is_browser_supported) return "browser_sign_in";
  return signIn.credential_parameters.length > 0 ? "credential_form" : "unconnectable";
}

/** Whether an open credential form carries everything the server needs. Blank
 * values are refused server-side, so the submit waits for all of them. */
export function isCredentialFormComplete(
  signIn: UiServiceSignIn,
  valueByParameterName: Record<string, string>,
  accountName: string,
): boolean {
  if (signIn.credential_parameters.length === 0) return false;
  if (signIn.is_account_name_required && accountName.trim() === "") return false;
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
    const result = await this.fetchJsonImpl(this.apiBase());
    if (!result.ok) {
      this.status = "load_failed";
      this.errorMessage = errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.redrawImpl();
      return;
    }
    this.data = result.body as UiWorkspacePermissions;
    this.status = "ready";
    this.redrawImpl();
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
        "Could not refresh permissions: " + errorMessageFromBody(result.body, `HTTP ${result.status}`);
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

  async revokeAll(serviceName: string, account: string, serviceLabel: string): Promise<void> {
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
   * NOT scoped to this machine: the stored credential itself is cleared, so the
   * account is disconnected everywhere and the server strips its grants from
   * every workspace. The connection therefore leaves the refreshed view
   * entirely, and the section it occupied has to be given up.
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
    const sectionIdsBefore = new Set((this.data?.connections ?? []).map(connectionSectionId));
    const result = await this.enqueueWrite(() =>
      this.fetchJsonImpl("/settings/connectors/add-account", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ service_name: serviceName }),
      }),
    );
    this.busyRowKeys.delete(rowKey);
    if (!result.ok) {
      this.alertMessage = "Could not connect: " + errorMessageFromBody(result.body, `HTTP ${result.status}`);
      this.redrawImpl();
      return null;
    }
    // In the same queue as the flips: the sign-in blocks for seconds while the
    // rest of the pane stays live, so an un-chained re-read could land after a
    // toggle made meanwhile and put its old state back on screen.
    await this.enqueueWrite(() => this.reloadInPlace());
    const added = (this.data?.connections ?? []).find(
      (connection) =>
        connection.service_name === serviceName && !sectionIdsBefore.has(connectionSectionId(connection)),
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
    const sectionIdsBefore = new Set((this.data?.connections ?? []).map(connectionSectionId));
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
      this.credentialErrorMessage = errorMessageFromBody(result.body, `HTTP ${result.status}`);
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
        connection.service_name === serviceName && !sectionIdsBefore.has(connectionSectionId(connection)),
    );
    return added === undefined ? null : connectionSectionId(added);
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
  ): Promise<boolean> {
    // A second click while this row's write runs is dropped rather than
    // queued, so the stored state never races itself.
    if (this.busyRowKeys.has(rowKey)) return false;
    this.busyRowKeys.add(rowKey);
    this.errorMessage = "";
    this.redrawImpl();
    try {
      this.data = await this.enqueueWrite(() => this.post(path, body));
      this.status = "ready";
    } catch (error) {
      // The last good view stays on screen: a refused write changed nothing.
      this.errorMessage = errorPrefix + (error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      this.busyRowKeys.delete(rowKey);
      this.redrawImpl();
    }
    return true;
  }

  private async post(path: string, body: unknown): Promise<UiWorkspacePermissions> {
    const result = await this.fetchJsonImpl(`${this.apiBase()}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!result.ok) throw new Error(errorMessageFromBody(result.body, `HTTP ${result.status}`));
    return result.body as UiWorkspacePermissions;
  }

  /** Serialize writes: overlapping flips reach the server in click order and
   * the last response is the one left on screen. */
  private enqueueWrite<T>(makeRequest: () => Promise<T>): Promise<T> {
    const result = this.writeChain.then(makeRequest, makeRequest);
    this.writeChain = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private apiBase(): string {
    return `/ui/api/workspaces/${encodeURIComponent(this.agentId)}/permissions`;
  }
}
