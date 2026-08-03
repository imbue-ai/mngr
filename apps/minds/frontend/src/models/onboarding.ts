// First-run onboarding transitions: acknowledge the error-reporting notice,
// and choose to continue without an account. Thin wrappers over the two
// /ui/api/onboarding POSTs so the pages stay declarative and the transitions
// are unit-testable.

interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

function defaultFetcher(url: string, init?: RequestInit): Promise<Response> {
  return fetch(url, { credentials: "same-origin", ...init });
}

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

/** POST the continue-without-an-account choice; resolves true when recorded. */
export async function skipAccountSetup(fetcher: FetchLike = defaultFetcher): Promise<boolean> {
  try {
    const response = await fetcher("/ui/api/onboarding/skip-account-setup", { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}
