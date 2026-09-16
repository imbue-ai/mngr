// First-run onboarding transitions and the install's onboarding progress:
// acknowledge the error-reporting notice, mark onboarding complete, and the
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
 * SPA completes onboarding itself (a sign-in from the start flow, or a create
 * submission), so the home page's redirect never fires against a seed that the
 * server has since moved past.
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

/** POST the consent acknowledgement; resolves true when it was recorded. */
export async function acknowledgeErrorReportingConsent(fetcher: FetchLike = defaultFetcher): Promise<boolean> {
  try {
    const response = await fetcher("/ui/api/onboarding/consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return response.ok;
  } catch {
    return false;
  }
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
