// Reconnect backoff schedule for the /ui/ws channel: exponential with a cap
// and deterministic-testable jitter injection.

export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 15_000;
// How many consecutive failures before the shell surfaces the subtle
// "reconnecting" indicator (silent below this).
export const VISIBLE_AFTER_FAILURES = 3;

export function backoffDelayMs(consecutiveFailures: number, jitter01: number): number {
  const exponent = Math.max(0, consecutiveFailures - 1);
  const base = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** exponent);
  // +-25% jitter so a fleet of windows doesn't reconnect in lockstep.
  const spread = base * 0.25;
  return Math.round(base - spread + jitter01 * 2 * spread);
}
