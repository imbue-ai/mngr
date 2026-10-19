// The shared-workspaces page (/workspaces/shared): every share each signed-in
// account holds, joined by the backend to the account's workspace records so
// a share whose workspace is destroyed or unknown reads as leaked. Removing a
// row frees its max_shared_workspaces slot: an unpublish when this device runs
// the workspace, else a plain delete of the share at the connector.

import type { UiAccountEntry } from "../channel/messages";
import { browserLifecycleDeps, type LifecycleDeps } from "./backups";

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

export interface SharedWorkspacesDeps extends Pick<LifecycleDeps, "getJson" | "redraw"> {
  /** DELETE returning {status, json} (json null when unparseable): unlike
   * LifecycleDeps.deleteResource, the body is kept, since the page shows the
   * route's refusal reason. */
  deleteJson(url: string): Promise<{ status: number; json: unknown | null }>;
}

export function browserSharedWorkspacesDeps(redraw: () => void): SharedWorkspacesDeps {
  const lifecycle = browserLifecycleDeps(redraw);
  return {
    getJson: lifecycle.getJson,
    async deleteJson(url) {
      try {
        const response = await fetch(url, { method: "DELETE", credentials: "same-origin" });
        let json: unknown | null = null;
        try {
          json = (await response.json()) as unknown;
        } catch {
          json = null;
        }
        return { status: response.status, json };
      } catch {
        return { status: 0, json: null };
      }
    },
    redraw,
  };
}

/** One account's section of the page. `rows` is null while its read is out
 * or after it failed; `isUnavailable` says which. */
export interface AccountSharesSection {
  accountId: string;
  email: string;
  rows: SharedWorkspaceRow[] | null;
  isUnavailable: boolean;
}

export function accountSharesUrl(accountId: string): string {
  return `/api/v1/account-shares/${encodeURIComponent(accountId)}`;
}

export class SharedWorkspacesModel {
  sections: AccountSharesSection[] = [];
  isLoaded = false;
  errorMessage: string | null = null;
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
      isUnavailable: false,
    }));
    this.isLoaded = accounts.length === 0;
    this.deps.redraw();
    await Promise.all(this.sections.map((section) => this.loadSection(section)));
    this.isLoaded = true;
    this.deps.redraw();
  }

  private async loadSection(section: AccountSharesSection): Promise<void> {
    const payload = (await this.deps.getJson(accountSharesUrl(section.accountId))) as AccountSharesPayload | null;
    if (payload === null) {
      section.rows = null;
      section.isUnavailable = true;
    } else {
      section.rows = payload.rows;
      section.isUnavailable = false;
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
    this.errorMessage = null;
    this.deps.redraw();
    const result = await this.deps.deleteJson(
      `${accountSharesUrl(section.accountId)}/${encodeURIComponent(hostId)}`,
    );
    this.removingHostIds.delete(hostId);
    if (result.status < 200 || result.status >= 300) {
      const detail = result.json as { error?: string } | null;
      this.errorMessage = detail?.error ?? "Could not remove the share; see the logs and try again.";
      this.deps.redraw();
      return;
    }
    await this.loadSection(section);
  }
}
