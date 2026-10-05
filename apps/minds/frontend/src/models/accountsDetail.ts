// Accounts page model: each account's asynchronously-loaded plan/usage
// section (the connector round trip that must never block first paint) and
// the account actions. The account list itself is the accounts store's,
// kept current by the channel. While a backup trim runs the plan section
// re-polls so progress stays visible.
//
// The last plan each account loaded is kept for the life of the window, so
// reopening the page shows it at once while a fresh read replaces it. It is
// held in memory rather than localStorage: the backend takes a new port each
// launch, so the page's origin (and its storage) changes with it.
//
// The mutating account actions reuse the legacy form-POST routes unchanged
// (set-default, logout, plan switch, trim); their 303-redirect success
// responses are followed by fetch and land on the SPA index, so response.ok
// is the success signal. The server republishes the accounts frame after
// set-default and logout, which is what updates the list.

import m from "mithril";
import type { UiAccountEntry } from "../channel/messages";

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
  /** The plan shown is a previous visit's, and a fresh read is in flight. */
  isRefreshing: boolean;
  /** The last read failed, so the plan shown is an older one. */
  isRefreshFailed: boolean;
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

interface CachedAccountPlan {
  planView: AccountPlanView;
  privacyPolicyUrl: string;
}

const TRIM_POLL_MS = 4000;

// Shared by every mount of the page. Trim status is not kept: it is the
// backend's live state, and a stale "running" would be wrong.
const cachedPlanByUserId = new Map<string, CachedAccountPlan>();

export function clearAccountPlanCacheForTests(): void {
  cachedPlanByUserId.clear();
}

type FetchLike = typeof fetch;
type ScheduleLike = (callback: () => void, delayMs: number) => void;

export class AccountsDetailModel {
  actionError = "";
  planByUserId = new Map<string, AccountPlanState>();
  verifyEmailPromptByUserId = new Map<string, VerifyEmailPrompt>();
  loggingOutUserIds = new Set<string>();
  switchingPlanUserIds = new Set<string>();

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;
  private readonly schedule: ScheduleLike;
  private isDisposed = false;
  // is_enabled by user_id, as of the last syncPlans.
  private listedIsEnabledByUserId = new Map<string, boolean>();

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

  /** Load the plan section of each account that is newly listed or was just
   * signed back in, dropping its old answer: a plan loaded while signed out
   * reads as unavailable, and a sign-in while the page is open must replace
   * it. The cached plan, when there is one, is shown until the read lands. */
  syncPlans(accounts: readonly UiAccountEntry[]): void {
    const previous = this.listedIsEnabledByUserId;
    this.listedIsEnabledByUserId = new Map(
      accounts.map((account) => [account.user_id, account.is_enabled]),
    );
    for (const userId of cachedPlanByUserId.keys()) {
      if (!this.listedIsEnabledByUserId.has(userId)) cachedPlanByUserId.delete(userId);
    }
    for (const account of accounts) {
      const wasEnabled = previous.get(account.user_id);
      if (wasEnabled === undefined || (!wasEnabled && account.is_enabled)) {
        this.planByUserId.delete(account.user_id);
        // A signed-out account's read cannot succeed, so its old figures would only mislead.
        const cached = account.is_enabled ? cachedPlanByUserId.get(account.user_id) : undefined;
        if (cached !== undefined) {
          const state = this.planStateFor(account.user_id);
          state.isLoaded = true;
          state.isRefreshing = true;
          state.planView = cached.planView;
          state.privacyPolicyUrl = cached.privacyPolicyUrl;
        }
        void this.loadPlan(account.user_id);
      }
    }
  }

  planStateFor(userId: string): AccountPlanState {
    const existing = this.planByUserId.get(userId);
    if (existing !== undefined) return existing;
    const fresh: AccountPlanState = {
      isLoaded: false,
      isUnavailable: false,
      isRefreshing: false,
      isRefreshFailed: false,
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
      state.trimStatus = payload.trim_status;
      state.privacyPolicyUrl = payload.privacy_policy_url ?? "";
      if (payload.plan_view === null) {
        markReadFailed(state);
      } else {
        state.isLoaded = true;
        state.isUnavailable = false;
        state.isRefreshing = false;
        state.isRefreshFailed = false;
        state.planView = payload.plan_view;
        cachedPlanByUserId.set(userId, {
          planView: payload.plan_view,
          privacyPolicyUrl: state.privacyPolicyUrl,
        });
      }
      if (payload.trim_status?.is_running && !this.isDisposed) {
        this.schedule(() => void this.loadPlan(userId), TRIM_POLL_MS);
      }
    } catch {
      markReadFailed(state);
    }
    this.redraw();
  }

  /** POST one of the legacy form routes, reporting whether it succeeded.
   * `onFailure` may consume a non-OK response (returning true suppresses the
   * generic actionError). */
  async submitAccountForm(
    url: string,
    fields: Record<string, string>,
    onFailure?: (status: number, bodyText: string) => boolean,
  ): Promise<boolean> {
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
        return false;
      }
      return true;
    } catch {
      this.actionError = "The action failed (network error).";
      this.redraw();
      return false;
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
    // Switching runs a connector round trip plus a plan reload (several
    // seconds); the card's button reads this set to show a busy state and
    // swallow double-clicks, like the log-out button.
    if (this.switchingPlanUserIds.has(userId)) return;
    this.switchingPlanUserIds.add(userId);
    this.redraw();
    try {
      this.verifyEmailPromptByUserId.delete(userId);
      const isSwitched = await this.submitAccountForm(
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
      if (isSwitched) await this.loadPlan(userId);
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

/** A plan already on screen stays there, marked as not current; with none
 * the section reads as unavailable. */
function markReadFailed(state: AccountPlanState): void {
  state.isLoaded = true;
  state.isRefreshing = false;
  if (state.planView === null) {
    state.isUnavailable = true;
  } else {
    state.isRefreshFailed = true;
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
