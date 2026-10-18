// The email-verification gate shared by the start flow and the create form:
// whether the signed-in account's email is verified (the connector refuses a
// cloud create until it is), and the re-send of the verification email. Both
// go through the local app, which answers only for an account this install
// has signed in.

/** How often a gate asks whether the email is verified while it waits on the link. */
export const VERIFICATION_POLL_MS = 3000;

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

const defaultFetch: FetchLike = (input, init) => fetch(input, init);

function verificationUrl(email: string): string {
  return `/accounts/verification?email=${encodeURIComponent(email)}`;
}

/** Whether ``email`` is verified. Rejects when the app could not find out. */
export async function fetchIsEmailVerified(email: string, fetcher: FetchLike = defaultFetch): Promise<boolean> {
  const response = await fetcher(verificationUrl(email), { credentials: "same-origin" });
  if (!response.ok) throw new Error(`Verification check failed with status ${response.status}`);
  const body = (await response.json()) as { verified?: unknown };
  if (typeof body.verified !== "boolean") throw new Error("Verification check answered without a verdict");
  return body.verified;
}

/**
 * How a re-send ended.
 *
 * "suppressed" means a link really did go out moments ago (the server's
 * per-user cooldown); "failed" means nothing went out. Keeping them apart is
 * what lets the views avoid claiming a delivery that never happened.
 */
export type ResendOutcome = "sent" | "suppressed" | "failed";

/** Re-send the verification email and report what became of it. */
export async function resendVerificationEmail(
  email: string,
  fetcher: FetchLike = defaultFetch,
): Promise<ResendOutcome> {
  try {
    const response = await fetcher(verificationUrl(email), { method: "POST", credentials: "same-origin" });
    if (!response.ok) return "failed";
    const body = (await response.json()) as { sent?: unknown };
    return body.sent === true ? "sent" : "suppressed";
  } catch (error: unknown) {
    console.debug(`[verification] Could not re-send the verification email: ${String(error)}`);
    return "failed";
  }
}
