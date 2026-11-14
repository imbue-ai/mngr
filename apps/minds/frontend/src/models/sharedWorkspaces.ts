// The shared-workspaces page (/workspaces/shared): every share each signed-in
// account holds, joined by the backend to the account's workspace records so
// a share whose workspace is destroyed or unknown reads as leaked. Removing a
// row frees its max_shared_workspaces slot: an unpublish when this device runs
// the workspace, else a plain delete of the share at the connector.

import type { UiAccountEntry } from "../channel/messages";
import type { RequestResult } from "./errors";
import { shareFailureFrom, type ShareFailure } from "./shareFailures";
import { defaultFetchJson } from "./workspaceOptions";

export type SharedWorkspaceRecordState = "active" | "destroyed" | "missing";

export interface SharedWorkspaceRow {
  host_id: string;
  workspace_id: string | null;
  account_email: string;
  display_name: string;
  workspace_domain: string;
  device_label: string;
  share_state: string;
  record_state: SharedWorkspaceRecordState;
  created_display: string;
  last_seen_display: string;
  is_likely_leaked: boolean;
  is_hosted_here: boolean;
}

interface AccountSharesPayload {
  account_id: string;
  email: string;
  rows: SharedWorkspaceRow[];
}

/** The page's requests, each answering its status (0 when no answer came)
 * and its body, since the page words a refusal from the route's reason. */
export interface SharedWorkspacesDeps {
  getJson(url: string): Promise<RequestResult>;
  deleteJson(url: string): Promise<RequestResult>;
  redraw(): void;
}

export function browserSharedWorkspacesDeps(redraw: () => void): SharedWorkspacesDeps {
  return {
    getJson: (url) => defaultFetchJson(url),
    deleteJson: (url) => defaultFetchJson(url, { method: "DELETE" }),
    redraw,
  };
}

/** The context the page's failures are worded in: it acts on no one workspace. */
const NO_WORKSPACE_CONTEXT = { isWorkspaceStopped: false };

/** One account's section of the page. `rows` is null while its read is out
 * or after it failed; `failure` says why it failed. */
export interface AccountSharesSection {
  accountId: string;
  email: string;
  rows: SharedWorkspaceRow[] | null;
  failure: ShareFailure | null;
}

export function accountSharesUrl(accountId: string): string {
  return `/api/v1/account-shares/${encodeURIComponent(accountId)}`;
}

export class SharedWorkspacesModel {
  sections: AccountSharesSection[] = [];
  isLoaded = false;
  /** Why the last removal failed; null when it did not. */
  removeFailure: ShareFailure | null = null;
  /** The row whose Remove has been armed (inline confirm), by host id. */
  armedHostId: string | null = null;
  removingHostIds = new Set<string>();

  private readonly deps: SharedWorkspacesDeps;
  /** The account ids of the last load; null before the first. */
  private loadedAccountIds: readonly string[] | null = null;

  constructor(deps: SharedWorkspacesDeps) {
    this.deps = deps;
  }

  /** Load the accounts' shares when the signed-in set differs from the last
   * load: the accounts store fills after boot and sign-ins land while the
   * page is open, so the page calls this on every redraw, and an unchanged
   * set must not re-read. */
  syncAccounts(accounts: readonly UiAccountEntry[]): void {
    const ids = accounts.map((account) => account.user_id);
    const loaded = this.loadedAccountIds;
    if (loaded !== null && loaded.length === ids.length && loaded.every((id, index) => id === ids[index])) return;
    void this.load(accounts);
  }

  /** Read every listed account's shares; a read that fails leaves that
   * account's section marked unavailable rather than failing the page. */
  async load(accounts: readonly UiAccountEntry[]): Promise<void> {
    this.loadedAccountIds = accounts.map((account) => account.user_id);
    this.sections = accounts.map((account) => ({
      accountId: account.user_id,
      email: account.email,
      rows: null,
      failure: null,
    }));
    this.isLoaded = accounts.length === 0;
    this.deps.redraw();
    await Promise.all(this.sections.map((section) => this.loadSection(section)));
    this.isLoaded = true;
    this.deps.redraw();
  }

  /** Read one account's section again (its Try again). */
  async reloadSection(section: AccountSharesSection): Promise<void> {
    section.rows = null;
    section.failure = null;
    this.deps.redraw();
    await this.loadSection(section);
  }

  private async loadSection(section: AccountSharesSection): Promise<void> {
    const outcome = await this.deps.getJson(accountSharesUrl(section.accountId));
    if (outcome.ok) {
      section.rows = (outcome.body as AccountSharesPayload).rows;
      section.failure = null;
    } else {
      section.rows = null;
      section.failure = shareFailureFrom("shares_load", outcome, NO_WORKSPACE_CONTEXT);
    }
    this.deps.redraw();
  }

  get rowCount(): number {
    return this.sections.reduce((count, section) => count + (section.rows?.length ?? 0), 0);
  }

  get leakedCount(): number {
    return this.sections.reduce(
      (count, section) => count + (section.rows?.filter((row) => row.is_likely_leaked).length ?? 0),
      0,
    );
  }

  /** Free the row's slot, then re-read its account's section so the page
   * shows the connector's view rather than an optimistic one. */
  async remove(section: AccountSharesSection, hostId: string): Promise<void> {
    this.armedHostId = null;
    this.removingHostIds.add(hostId);
    this.removeFailure = null;
    this.deps.redraw();
    const outcome = await this.deps.deleteJson(
      `${accountSharesUrl(section.accountId)}/${encodeURIComponent(hostId)}`,
    );
    this.removingHostIds.delete(hostId);
    if (!outcome.ok) {
      this.removeFailure = shareFailureFrom("share_remove", outcome, NO_WORKSPACE_CONTEXT);
      this.deps.redraw();
      return;
    }
    await this.loadSection(section);
  }
}
