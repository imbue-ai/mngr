// Accounts page model: the account list from /ui/api/accounts plus each
// account's asynchronously-loaded plan/usage section (the connector round
// trip that must never block first paint). While a backup trim runs the
// plan section re-polls so progress stays visible.
//
// The mutating account actions reuse the legacy form-POST routes unchanged
// (set-default, logout, plan switch, trim); their 303-redirect success
// responses are followed by fetch and land on the SPA index, so response.ok
// is the success signal and a reload refreshes the list.

import m from "mithril";

export interface AccountEntry {
  user_id: string;
  email: string;
  workspace_count: number;
  is_default: boolean;
  is_enabled: boolean;
}

export interface PlanUsageRow {
  label: string;
  used: string;
  limit: string;
  note: string;
}

export interface AccountPlanView {
  plan_name: string;
  plan_display_name: string;
  available_plans: string[];
  usage_rows: PlanUsageRow[];
  is_over_storage_quota: boolean;
  is_at_bucket_quota: boolean;
}

export interface TrimStatus {
  is_running: boolean;
  detail: string;
}

export interface AccountPlanState {
  isLoaded: boolean;
  isUnavailable: boolean;
  planView: AccountPlanView | null;
  trimStatus: TrimStatus | null;
}

const TRIM_POLL_MS = 4000;

type FetchLike = typeof fetch;
type ScheduleLike = (callback: () => void, delayMs: number) => void;

export class AccountsDetailModel {
  accounts: AccountEntry[] = [];
  isListLoaded = false;
  isLoadFailed = false;
  actionError = "";
  planByUserId = new Map<string, AccountPlanState>();

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;
  private readonly schedule: ScheduleLike;
  private isDisposed = false;

  constructor(
    // A plain-call wrapper: passing the global `fetch` itself would invoke it
    // with the model as its receiver ("Illegal invocation" in browsers).
    fetchImpl: FetchLike = (input, init) => fetch(input, init),
    redraw: () => void = m.redraw,
    schedule: ScheduleLike = (callback, delayMs) => {
      setTimeout(callback, delayMs);
    },
  ) {
    this.fetchImpl = fetchImpl;
    this.redraw = redraw;
    this.schedule = schedule;
  }

  dispose(): void {
    this.isDisposed = true;
  }

  async load(): Promise<void> {
    this.isLoadFailed = false;
    try {
      const response = await this.fetchImpl("/ui/api/accounts", {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { accounts: AccountEntry[] };
      this.accounts = payload.accounts;
      this.isListLoaded = true;
    } catch {
      this.isLoadFailed = true;
    }
    this.redraw();
    for (const account of this.accounts) {
      void this.loadPlan(account.user_id);
    }
  }

  planStateFor(userId: string): AccountPlanState {
    const existing = this.planByUserId.get(userId);
    if (existing !== undefined) return existing;
    const fresh: AccountPlanState = {
      isLoaded: false,
      isUnavailable: false,
      planView: null,
      trimStatus: null,
    };
    this.planByUserId.set(userId, fresh);
    return fresh;
  }

  async loadPlan(userId: string): Promise<void> {
    if (this.isDisposed) return;
    const state = this.planStateFor(userId);
    try {
      const response = await this.fetchImpl(
        `/ui/api/accounts/${encodeURIComponent(userId)}/plan`,
        {
          credentials: "same-origin",
        },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as {
        plan_view: AccountPlanView | null;
        trim_status: TrimStatus | null;
      };
      state.isLoaded = true;
      state.isUnavailable = payload.plan_view === null;
      state.planView = payload.plan_view;
      state.trimStatus = payload.trim_status;
      if (payload.trim_status?.is_running && !this.isDisposed) {
        this.schedule(() => void this.loadPlan(userId), TRIM_POLL_MS);
      }
    } catch {
      state.isLoaded = true;
      state.isUnavailable = true;
    }
    this.redraw();
  }

  /** POST one of the legacy form routes; on success reload the whole list. */
  async submitAccountForm(
    url: string,
    fields: Record<string, string>,
  ): Promise<void> {
    this.actionError = "";
    try {
      const response = await this.fetchImpl(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(fields).toString(),
      });
      if (!response.ok) {
        this.actionError =
          (await response.text()) ||
          `The action failed (HTTP ${response.status}).`;
        this.redraw();
        return;
      }
      await this.load();
    } catch {
      this.actionError = "The action failed (network error).";
      this.redraw();
    }
  }

  async setDefault(userId: string): Promise<void> {
    await this.submitAccountForm("/accounts/set-default", { user_id: userId });
  }

  async logOut(userId: string): Promise<void> {
    await this.submitAccountForm(
      `/accounts/${encodeURIComponent(userId)}/logout`,
      {},
    );
  }

  async switchPlan(userId: string, plan: string): Promise<void> {
    await this.submitAccountForm(
      `/accounts/${encodeURIComponent(userId)}/plan`,
      { plan },
    );
  }

  async trimBackups(userId: string): Promise<void> {
    await this.submitAccountForm(
      `/accounts/${encodeURIComponent(userId)}/trim-backups`,
      {},
    );
    await this.loadPlan(userId);
  }
}
