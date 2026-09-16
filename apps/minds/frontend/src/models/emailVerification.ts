// The start flow's email-verification gate: whether the signed-in account's
// email is verified (the connector refuses a cloud create until it is), and
// the re-send of the verification email. Both go through the local app,
// which answers only for an account this install has signed in.

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

/** Re-send the verification email; false when the server's cooldown suppressed it or the request failed. */
export async function resendVerificationEmail(email: string, fetcher: FetchLike = defaultFetch): Promise<boolean> {
  try {
    const response = await fetcher(verificationUrl(email), { method: "POST", credentials: "same-origin" });
    if (!response.ok) return false;
    const body = (await response.json()) as { sent?: unknown };
    return body.sent === true;
  } catch (error: unknown) {
    // Answered as nothing sent, like the server's cooldown; the console line
    // is what separates a request that never landed from a suppressed send.
    console.debug(`[verification] Could not re-send the verification email: ${String(error)}`);
    return false;
  }
}
