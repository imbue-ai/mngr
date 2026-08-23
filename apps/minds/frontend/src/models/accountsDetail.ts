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
  privacyPolicyUrl: string;
}

/** The contextual "verify your email" prompt shown after a plan switch was
 * refused with the connector's structured email_not_verified 403. The server
 * tries to auto-send the first verification email as part of that refusal;
 * `wasAutoSent` is false when a cooldown suppressed that send (or it failed),
 * so the prompt must not claim a link was just sent. */
export interface VerifyEmailPrompt {
  email: string;
  wasAutoSent: boolean;
  isResending: boolean;
  wasResent: boolean;
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
  verifyEmailPromptByUserId = new Map<string, VerifyEmailPrompt>();
  loggingOutUserIds = new Set<string>();
  switchingPlanUserIds = new Set<string>();

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
      privacyPolicyUrl: "",
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
        privacy_policy_url?: string;
      };
      state.isLoaded = true;
      state.isUnavailable = payload.plan_view === null;
      state.planView = payload.plan_view;
      state.trimStatus = payload.trim_status;
      state.privacyPolicyUrl = payload.privacy_policy_url ?? "";
      if (payload.trim_status?.is_running && !this.isDisposed) {
        this.schedule(() => void this.loadPlan(userId), TRIM_POLL_MS);
      }
    } catch {
      state.isLoaded = true;
      state.isUnavailable = true;
    }
    this.redraw();
  }

  /** POST one of the legacy form routes; on success reload the whole list.
   * `onFailure` may consume a non-OK response (returning true suppresses the
   * generic actionError). */
  async submitAccountForm(
    url: string,
    fields: Record<string, string>,
    onFailure?: (status: number, bodyText: string) => boolean,
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
        const text = await response.text();
        if (onFailure === undefined || !onFailure(response.status, text)) {
          this.actionError =
            text || `The action failed (HTTP ${response.status}).`;
        }
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
    // Logging out runs a subprocess server-side (several seconds); the card's
    // button reads this set to show a busy state and swallow double-clicks.
    if (this.loggingOutUserIds.has(userId)) return;
    this.loggingOutUserIds.add(userId);
    this.redraw();
    try {
      await this.submitAccountForm(
        `/accounts/${encodeURIComponent(userId)}/logout`,
        {},
      );
    } finally {
      this.loggingOutUserIds.delete(userId);
      this.redraw();
    }
  }

  isLoggingOut(userId: string): boolean {
    return this.loggingOutUserIds.has(userId);
  }

  verifyEmailPromptFor(userId: string): VerifyEmailPrompt | null {
    return this.verifyEmailPromptByUserId.get(userId) ?? null;
  }

  async switchPlan(userId: string, plan: string): Promise<void> {
    // Switching runs a connector round trip plus a full reload (several
    // seconds); the card's button reads this set to show a busy state and
    // swallow double-clicks, like the log-out button.
    if (this.switchingPlanUserIds.has(userId)) return;
    this.switchingPlanUserIds.add(userId);
    this.redraw();
    try {
      this.verifyEmailPromptByUserId.delete(userId);
      await this.submitAccountForm(
        `/accounts/${encodeURIComponent(userId)}/plan`,
        { plan },
        (status, bodyText) => {
          const refusal = parseEmailNotVerified(status, bodyText);
          if (refusal === null) return false;
          this.verifyEmailPromptByUserId.set(userId, {
            email: refusal.email,
            wasAutoSent: refusal.wasAutoSent,
            isResending: false,
            wasResent: false,
          });
          return true;
        },
      );
    } finally {
      this.switchingPlanUserIds.delete(userId);
      this.redraw();
    }
  }

  isSwitchingPlan(userId: string): boolean {
    return this.switchingPlanUserIds.has(userId);
  }

  /** The verify-email prompt's resend button (the server applies a cooldown). */
  async resendVerification(userId: string): Promise<void> {
    const prompt = this.verifyEmailPromptByUserId.get(userId);
    if (prompt === undefined || prompt.isResending) return;
    this.verifyEmailPromptByUserId.set(userId, {
      ...prompt,
      isResending: true,
      wasResent: false,
    });
    this.redraw();
    let wasResent = false;
    try {
      const response = await this.fetchImpl(
        `/accounts/${encodeURIComponent(userId)}/resend-verification`,
        { method: "POST", credentials: "same-origin" },
      );
      // The route answers 200 even when the connector's cooldown (or a CLI
      // failure) suppressed the send -- the body's `sent` flag is the truth,
      // and the prompt must not claim a link the server did not confirm.
      if (response.ok) {
        const body: unknown = await response.json();
        wasResent =
          typeof body === "object" && body !== null && (body as { sent?: unknown }).sent === true;
      }
    } catch {
      // A network or parse failure counts as not resent; wasResent stays false.
    }
    // The prompt may have been removed while the request was in flight (a
    // concurrent plan switch deletes it first); a deleted prompt must not be
    // resurrected here.
    const current = this.verifyEmailPromptByUserId.get(userId);
    if (current !== undefined) {
      this.verifyEmailPromptByUserId.set(userId, {
        ...current,
        isResending: false,
        wasResent,
      });
    }
    this.redraw();
  }

  async trimBackups(userId: string): Promise<void> {
    await this.submitAccountForm(
      `/accounts/${encodeURIComponent(userId)}/trim-backups`,
      {},
    );
    await this.loadPlan(userId);
  }
}

/** The email and auto-send outcome from a structured email_not_verified 403
 * body, or null when the response is not that refusal. A missing `sent` flag
 * counts as not sent, so the prompt never claims a link the server did not
 * confirm. */
function parseEmailNotVerified(status: number, bodyText: string): { email: string; wasAutoSent: boolean } | null {
  if (status !== 403) return null;
  let body: unknown;
  try {
    body = JSON.parse(bodyText);
  } catch {
    return null;
  }
  if (
    typeof body === "object" &&
    body !== null &&
    (body as { code?: unknown }).code === "email_not_verified" &&
    typeof (body as { email?: unknown }).email === "string"
  ) {
    return {
      email: (body as { email: string }).email,
      wasAutoSent: (body as { sent?: unknown }).sent === true,
    };
  }
  return null;
}
