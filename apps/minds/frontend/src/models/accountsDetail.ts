// Accounts page model: each account's asynchronously-loaded plan/usage
// section and notification preferences (the connector round trips that must
// never block first paint) and the account actions. The account list itself
// is the accounts store's, kept current by the channel. While a backup trim
// runs the plan section re-polls so progress stays visible.
//
// The last plan each account loaded is kept for the life of the window, so
// reopening the page shows it at once while a fresh read replaces it. It is
// held in memory rather than localStorage: the backend takes a new port each
// launch, so the page's origin (and its storage) changes with it.
//
// The mutating account actions reuse the legacy form-POST routes unchanged
// (logout, plan switch, trim); their 303-redirect success responses are
// followed by fetch and land on the SPA index, so response.ok is the success
// signal. The server republishes the accounts frame after logout, which is
// what updates the list.

import m from "mithril";
import type { UiAccountEntry } from "../channel/messages";
import type { ResponseBody, UserFacingError } from "./errors";
import {
  TextResponseBody,
  WINDOW_LOGIN_LAPSED_SENTENCE,
  errorCodeFromBody,
  errorFromSentenceResponse,
  fallbackErrorSentence,
  readResponseBody,
  reasonFromBody,
  responseDetails,
} from "./errors";
import { isReconnectNeeded } from "./reconnect";

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
  /** The account holds at least one share: the shared-workspaces page has
   * something to review (and a leaked share to remove, if any). */
  is_sharing_workspaces: boolean;
}

export interface TrimStatus {
  is_running: boolean;
  detail: string;
  /** What went wrong, for See details, when the cleanup could not finish. */
  error_detail: string;
}

/** Why an account's plan or notification settings could not be read. All
 * but WINDOW_LOGIN_LAPSED come from the route; that one is the route refusing
 * this window because its own login on this computer has lapsed. */
export type AccountReadFailureKind = RouteFailureKind | "WINDOW_LOGIN_LAPSED";

const ROUTE_FAILURE_KINDS = [
  "NOT_SIGNED_IN",
  "NOT_CONFIGURED",
  "SESSION_REJECTED",
  "EMAIL_NOT_VERIFIED",
  "CLIENT_TOO_OLD",
  "FAILED",
] as const;

type RouteFailureKind = (typeof ROUTE_FAILURE_KINDS)[number];

function isRouteFailureKind(kind: unknown): kind is RouteFailureKind {
  return (ROUTE_FAILURE_KINDS as readonly unknown[]).includes(kind);
}

export interface AccountReadFailure {
  kind: AccountReadFailureKind;
  /** The underlying error, for See details. */
  details: string;
}

export interface AccountPlanState {
  isLoaded: boolean;
  isUnavailable: boolean;
  /** The plan shown is a previous visit's, and a fresh read is in flight. */
  isRefreshing: boolean;
  /** The last read failed, so the plan shown is an older one. */
  isRefreshFailed: boolean;
  /** Why the last read failed; null after a successful one. */
  readFailure: AccountReadFailure | null;
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

/** The account's notification channels on Imbue Cloud. Only email is
 * switchable from here; in-app stays as it is until that channel exists. */
export interface NotificationChannels {
  email_enabled: boolean;
  in_app_enabled: boolean;
}

export interface NotificationPreferencesState {
  isLoaded: boolean;
  /** null once loaded means they could not be read; readFailure says why. */
  channels: NotificationChannels | null;
  readFailure: AccountReadFailure | null;
  isWriting: boolean;
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
  actionError: UserFacingError | null = null;
  /** Repeats the action behind `actionError`, for a failure that trying again can fix. */
  actionErrorRetry: (() => void) | null = null;
  planByUserId = new Map<string, AccountPlanState>();
  notificationPreferencesByUserId = new Map<
    string,
    NotificationPreferencesState
  >();
  verifyEmailPromptByUserId = new Map<string, VerifyEmailPrompt>();
  loggingOutUserIds = new Set<string>();
  switchingPlanUserIds = new Set<string>();

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;
  private readonly schedule: ScheduleLike;
  private isDisposed = false;
  // The user ids listed as of the last syncAccounts.
  private listedUserIds = new Set<string>();

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

  /** Load the plan section and the notification preferences of each account
   * that is newly listed (a sign-in while the page is open adds it), dropping
   * its old answers. The cached plan, when there is one, is shown until the
   * read lands. */
  syncAccounts(accounts: readonly UiAccountEntry[]): void {
    const previous = this.listedUserIds;
    this.listedUserIds = new Set(accounts.map((account) => account.user_id));
    for (const userId of cachedPlanByUserId.keys()) {
      if (!this.listedUserIds.has(userId)) cachedPlanByUserId.delete(userId);
    }
    for (const account of accounts) {
      if (!previous.has(account.user_id)) {
        this.planByUserId.delete(account.user_id);
        this.notificationPreferencesByUserId.delete(account.user_id);
        const cached = cachedPlanByUserId.get(account.user_id);
        if (cached !== undefined) {
          const state = this.planStateFor(account.user_id);
          state.isLoaded = true;
          state.isRefreshing = true;
          state.planView = cached.planView;
          state.privacyPolicyUrl = cached.privacyPolicyUrl;
        }
        void this.loadPlan(account.user_id);
        void this.loadNotificationPreferences(account.user_id);
      }
    }
  }

  notificationPreferencesFor(userId: string): NotificationPreferencesState {
    const existing = this.notificationPreferencesByUserId.get(userId);
    if (existing !== undefined) return existing;
    const fresh: NotificationPreferencesState = {
      isLoaded: false,
      channels: null,
      readFailure: null,
      isWriting: false,
    };
    this.notificationPreferencesByUserId.set(userId, fresh);
    return fresh;
  }

  async loadNotificationPreferences(userId: string): Promise<void> {
    if (this.isDisposed) return;
    const state = this.notificationPreferencesFor(userId);
    const read = await readAccountSection<{
      preferences?: NotificationChannels | null;
    }>(this.fetchImpl, notificationPreferencesUrl(userId));
    const channels = read.body?.preferences ?? null;
    state.channels = channels;
    state.readFailure =
      channels === null ? (read.failure ?? failureInBody(read.body)) : null;
    state.isLoaded = true;
    this.redraw();
  }

  /** Turn the account's notification email on or off. The email an account
   * needs, such as a password reset, is never governed by this switch. */
  async setEmailNotifications(
    userId: string,
    isEnabled: boolean,
  ): Promise<void> {
    const state = this.notificationPreferencesFor(userId);
    if (state.isWriting || state.channels === null) return;
    state.isWriting = true;
    this.actionError = null;
    this.actionErrorRetry = null;
    this.redraw();
    const read = await readAccountSection<{
      preferences?: NotificationChannels | null;
    }>(this.fetchImpl, notificationPreferencesUrl(userId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email_enabled: isEnabled }),
    });
    const channels = read.body?.preferences ?? null;
    const failure =
      channels === null ? (read.failure ?? failureInBody(read.body)) : null;
    if (failure === null) {
      state.channels = channels;
    } else if (failure.kind === "FAILED") {
      this.actionError = {
        message: fallbackErrorSentence("Couldn't save your notification setting"),
        details: failure.details,
      };
      this.actionErrorRetry = () => void this.setEmailNotifications(userId, isEnabled);
    } else {
      // The write passes the same sign-in checks as the read, so any other
      // cause leaves the section unreadable too, and the card's read notice
      // offers its remedy (Sign in, Restart Imbue Studio).
      state.channels = null;
      state.readFailure = failure;
    }
    state.isWriting = false;
    this.redraw();
  }

  planStateFor(userId: string): AccountPlanState {
    const existing = this.planByUserId.get(userId);
    if (existing !== undefined) return existing;
    const fresh: AccountPlanState = {
      isLoaded: false,
      isUnavailable: false,
      isRefreshing: false,
      isRefreshFailed: false,
      readFailure: null,
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
    const read = await readAccountSection<{
      plan_view: AccountPlanView | null;
      trim_status: TrimStatus | null;
      privacy_policy_url?: string;
    }>(this.fetchImpl, `/ui/api/accounts/${encodeURIComponent(userId)}/plan`);
    const payload = read.body;
    if (payload === null) {
      markReadFailed(state, read.failure ?? failureInBody(null));
      this.redraw();
      return;
    }
    state.trimStatus = payload.trim_status;
    state.privacyPolicyUrl = payload.privacy_policy_url ?? "";
    if (payload.plan_view === null) {
      markReadFailed(state, failureInBody(payload));
    } else {
      state.isLoaded = true;
      state.isUnavailable = false;
      state.isRefreshing = false;
      state.isRefreshFailed = false;
      state.readFailure = null;
      state.planView = payload.plan_view;
      cachedPlanByUserId.set(userId, {
        planView: payload.plan_view,
        privacyPolicyUrl: state.privacyPolicyUrl,
      });
    }
    if (payload.trim_status?.is_running && !this.isDisposed) {
      this.schedule(() => void this.loadPlan(userId), TRIM_POLL_MS);
    }
    this.redraw();
  }

  /** POST one of the legacy form routes, reporting whether it succeeded.
   * `whatFailed` names the action for the page error ("Couldn't log out
   * <email>"); a refusal the route words itself replaces it. `onFailure` may
   * consume a non-OK response (returning true suppresses the page error). */
  async submitAccountForm(
    url: string,
    fields: Record<string, string>,
    whatFailed: string,
    onFailure?: (status: number, body: ResponseBody) => boolean,
  ): Promise<boolean> {
    this.actionError = null;
    this.actionErrorRetry = null;
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(fields).toString(),
      });
    } catch (thrown) {
      this.actionError = {
        message: fallbackErrorSentence(whatFailed),
        details: `No response. ${thrown instanceof Error ? thrown.message : String(thrown)}`,
      };
      this.redraw();
      return false;
    }
    if (response.ok) return true;
    const body = await readResponseBody(response);
    if (onFailure === undefined || !onFailure(response.status, body)) {
      this.actionError = errorFromSentenceResponse(whatFailed, {
        status: response.status,
        body,
      });
    }
    this.redraw();
    return false;
  }

  async logOut(userId: string, email: string): Promise<void> {
    // Logging out runs a subprocess server-side (several seconds); the card's
    // button reads this set to show a busy state and swallow double-clicks.
    if (this.loggingOutUserIds.has(userId)) return;
    this.loggingOutUserIds.add(userId);
    this.redraw();
    try {
      await this.submitAccountForm(
        `/accounts/${encodeURIComponent(userId)}/logout`,
        {},
        `Couldn't log out ${email}`,
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

  /** `planLabel` is the plan as the page names it ("Limited" for free). */
  async switchPlan(
    userId: string,
    email: string,
    plan: string,
    planLabel: string,
  ): Promise<void> {
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
        `Couldn't switch ${email} to the ${planLabel} plan`,
        (status, body) => {
          const planRefusal = parsePlanRefused(status, body);
          if (planRefusal !== null) {
            // Imbue Cloud's stated reason, which no retry changes.
            this.actionError = {
              message: `Imbue Cloud won't switch ${email} to the ${planLabel} plan. It said: "${planRefusal}"`,
              details: `HTTP ${status}`,
            };
            return true;
          }
          const refusal = parseEmailNotVerified(status, body);
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
          typeof body === "object" &&
          body !== null &&
          (body as { sent?: unknown }).sent === true;
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
      "Couldn't start freeing up backup space",
    );
    await this.loadPlan(userId);
  }
}

/** A plan already on screen stays there, marked as not current; with none
 * the section reads as unavailable. */
function markReadFailed(
  state: AccountPlanState,
  failure: AccountReadFailure,
): void {
  state.isLoaded = true;
  state.isRefreshing = false;
  state.readFailure = failure;
  if (state.planView === null) {
    state.isUnavailable = true;
  } else {
    state.isRefreshFailed = true;
  }
}

interface AccountSectionRead<T> {
  /** The route's answer, or null when there was no usable one. */
  body: T | null;
  /** Why there was no usable answer; null when there was. */
  failure: AccountReadFailure | null;
}

/** Read (or, with `init`, write) one of an account card's sections. A
 * refusal of the window's own login is told apart, since only a restart
 * fixes it. */
async function readAccountSection<T>(
  fetchImpl: FetchLike,
  url: string,
  init: RequestInit = {},
): Promise<AccountSectionRead<T>> {
  let response: Response;
  try {
    response = await fetchImpl(url, { ...init, credentials: "same-origin" });
  } catch (thrown) {
    return {
      body: null,
      failure: {
        kind: "FAILED",
        details: `No response. ${thrown instanceof Error ? thrown.message : String(thrown)}`,
      },
    };
  }
  const body = await readResponseBody(response);
  if (!response.ok) {
    return {
      body: null,
      failure: {
        kind: isReconnectNeeded({ status: response.status, body }) ? "WINDOW_LOGIN_LAPSED" : "FAILED",
        details: responseDetails({ status: response.status, body }),
      },
    };
  }
  if (
    body === null ||
    typeof body !== "object" ||
    body instanceof TextResponseBody
  ) {
    return {
      body: null,
      failure: {
        kind: "FAILED",
        details: responseDetails({ status: response.status, body }),
      },
    };
  }
  return { body: body as T, failure: null };
}

/** The failure a section's answer carries alongside its empty value. */
function failureInBody(body: unknown): AccountReadFailure {
  const failure =
    body !== null && typeof body === "object"
      ? (body as { failure?: unknown }).failure
      : undefined;
  if (failure === null || typeof failure !== "object") {
    return { kind: "FAILED", details: "" };
  }
  const { kind, detail } = failure as { kind?: unknown; detail?: unknown };
  return {
    kind: isRouteFailureKind(kind) ? kind : "FAILED",
    details: typeof detail === "string" ? detail : "",
  };
}

/** What failed, for the sentences below: the plan area, the notification row,
 * or both when they failed for the same reason and share one notice. */
export type AccountSection = "plan" | "notifications" | "plan-and-notifications";

const SECTION_NOUN: Record<AccountSection, string> = {
  plan: "your plan and usage",
  notifications: "your notification settings",
  "plan-and-notifications": "your plan, usage and notification settings",
};

const SECTION_SHORT_NOUN: Record<AccountSection, string> = {
  plan: "your plan",
  notifications: "your notification settings",
  "plan-and-notifications": "your plan and notification settings",
};

/** The card's sentence for a section that could not be read. */
export function accountReadError(
  section: AccountSection,
  failure: AccountReadFailure,
): UserFacingError {
  const noun = SECTION_SHORT_NOUN[section];
  const details = failure.details;
  switch (failure.kind) {
    case "SESSION_REJECTED":
      return {
        message: `Your Imbue Cloud sign-in has expired. *Sign in again* to see ${noun}.`,
        details,
      };
    case "EMAIL_NOT_VERIFIED":
      return {
        message:
          `Imbue Cloud needs your email address verified before it shows ${noun}. ` +
          "*Click the link in the verification email, then try again.*",
        details,
      };
    case "CLIENT_TOO_OLD":
      return {
        message: `This version of Imbue Studio is too old to show ${noun}. *Update Imbue Studio.*`,
        details,
      };
    case "NOT_CONFIGURED":
      return {
        message: `This copy of Imbue Studio isn't set up for Imbue Cloud, so it can't show ${noun}.`,
        details,
      };
    case "WINDOW_LOGIN_LAPSED":
      return { message: WINDOW_LOGIN_LAPSED_SENTENCE, details };
    case "NOT_SIGNED_IN":
    case "FAILED":
      return {
        message: fallbackErrorSentence(`Couldn't load ${SECTION_NOUN[section]}`),
        details,
      };
  }
}

function notificationPreferencesUrl(userId: string): string {
  return `/ui/api/accounts/${encodeURIComponent(userId)}/notification-preferences`;
}

/** Imbue Cloud's reason from a structured plan_refused 403, or null when the
 * response is not that refusal. */
function parsePlanRefused(status: number, body: ResponseBody): string | null {
  if (status !== 403 || errorCodeFromBody(body) !== "plan_refused") return null;
  return reasonFromBody(body);
}

/** The email and auto-send outcome from a structured email_not_verified 403
 * body, or null when the response is not that refusal. A missing `sent` flag
 * counts as not sent, so the prompt never claims a link the server did not
 * confirm. */
function parseEmailNotVerified(
  status: number,
  body: ResponseBody,
): { email: string; wasAutoSent: boolean } | null {
  if (status !== 403 || errorCodeFromBody(body) !== "email_not_verified") return null;
  if (body === null || body instanceof TextResponseBody) return null;
  const email = body["email"];
  return typeof email === "string" ? { email, wasAutoSent: body["sent"] === true } : null;
}
