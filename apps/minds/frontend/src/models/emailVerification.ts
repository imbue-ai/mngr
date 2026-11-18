// The email-verification gate shared by the views that need a verified email:
// whether the signed-in account's email is verified (the connector refuses a
// cloud create until it is), and the re-send of the verification email. Both
// go through the local app, which answers only for an account this install
// has signed in.

/** How often a gate asks whether the email is verified while it waits on the link. */
export const VERIFICATION_POLL_MS = 3000;

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export const RESEND_EMAIL_LABEL = "Send the email again";

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
 * per-user cooldown); "failed" means nothing went out; "already_verified"
 * means nothing went out because the address needs no link. Keeping them
 * apart is what lets the views avoid claiming a delivery that never happened.
 */
export type ResendOutcome = "sent" | "suppressed" | "failed" | "already_verified";

/** What became of a send, read from the local app's answer to it. */
export async function resendOutcomeOf(send: () => Promise<Response>): Promise<ResendOutcome> {
  try {
    const response = await send();
    if (!response.ok) return "failed";
    const body = (await response.json()) as { sent?: unknown; already_verified?: unknown };
    if (body.already_verified === true) return "already_verified";
    if (body.sent === true) return "sent";
    return body.sent === false ? "suppressed" : "failed";
  } catch (error: unknown) {
    console.debug(`[verification] Could not re-send the verification email: ${String(error)}`);
    return "failed";
  }
}

/** Re-send the verification email and report what became of it. */
export function resendVerificationEmail(email: string, fetcher: FetchLike = defaultFetch): Promise<ResendOutcome> {
  return resendOutcomeOf(() => fetcher(verificationUrl(email), { method: "POST", credentials: "same-origin" }));
}

/** Only an outcome the server confirmed may point the user at an inbox. */
export function verificationEmailSentNote(email: string, outcome: ResendOutcome): string {
  switch (outcome) {
    case "sent":
      return `Sent another email to ${email}.`;
    case "suppressed":
      return `An email went out to ${email} moments ago. Check your inbox and spam folder.`;
    case "failed":
      return `Could not send the email to ${email}. Please try again.`;
    case "already_verified":
      return `${email} is already verified.`;
  }
}
