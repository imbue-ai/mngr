// First-run onboarding transitions and the install's onboarding progress:
// answer the error-reporting consent question, mark onboarding complete, and the
// in-memory copy of "is this install past the start flow?" the home page's
// redirect reads. Thin wrappers over the /ui/api/onboarding POSTs so the
// pages stay declarative and the transitions are unit-testable.

interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

function defaultFetcher(url: string, init?: RequestInit): Promise<Response> {
  return fetch(url, { credentials: "same-origin", ...init });
}

/**
 * Whether this install is past the first-run start flow, as this window knows
 * it. Seeded from the bootstrap document and flipped locally the moment the
 * SPA completes onboarding itself (a start-flow sign-in landing on an account
 * that has workspaces, or a create submission), so the home page's redirect
 * never fires against a seed that the server has since moved past.
 */
export class OnboardingProgress {
  isComplete = true;

  seed(isComplete: boolean): void {
    this.isComplete = isComplete;
  }

  markComplete(): void {
    this.isComplete = true;
  }
}

/** One copy for the window: every page reads and flips the same value. */
export const onboardingProgress = new OnboardingProgress();

export const REPORTING_CONSENT_QUESTION = "Is it ok if we report data to help improve Studio?";

/** POST the answer to the error-reporting question (whether error data may be reported); resolves true when
 *  it was recorded. */
export async function recordErrorReportingConsent(
  isReportingAllowed: boolean,
  fetcher: FetchLike = defaultFetcher,
): Promise<boolean> {
  try {
    const response = await fetcher("/ui/api/onboarding/consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_unexpected_errors: isReportingAllowed }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Where the start flow's existing-login sign-in should land, as the backend
 * knows it: "done" with the account's synced-workspace count once the first
 * record fetch has finished (or the records were already local), "pending"
 * while it is still in flight, "failed" when the last fetch could not reach
 * the sync service.
 */
export interface SigninLanding {
  state: "pending" | "done" | "failed";
  workspace_count: number;
}

/** GET the landing answer for the account the existing-login sign-in reported. */
export async function fetchSigninLanding(
  accountId: string,
  fetcher: FetchLike = defaultFetcher,
): Promise<SigninLanding> {
  const response = await fetcher(
    `/ui/api/onboarding/signin-landing?account=${encodeURIComponent(accountId)}`,
  );
  if (!response.ok) throw new Error(`The signin-landing route answered ${response.status}`);
  const body = (await response.json()) as Partial<SigninLanding>;
  const state = body.state === "pending" || body.state === "failed" ? body.state : "done";
  return {
    state,
    workspace_count: typeof body.workspace_count === "number" ? body.workspace_count : 0,
  };
}

/**
 * POST the onboarding-complete fact; resolves true when recorded. The local
 * copy flips regardless of the outcome: the user did complete the flow, and a
 * failed write only means the next launch may ask again.
 */
export async function markOnboardingComplete(
  fetcher: FetchLike = defaultFetcher,
  progress: OnboardingProgress = onboardingProgress,
): Promise<boolean> {
  progress.markComplete();
  try {
    const response = await fetcher("/ui/api/onboarding/complete", { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}
