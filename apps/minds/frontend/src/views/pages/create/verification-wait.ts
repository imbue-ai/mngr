// The create form's email-verification gate: the connector refuses a cloud
// create for an unverified email, so the create waits on the emailed link
// instead of being sent off to fail.

import { VERIFICATION_POLL_MS, fetchIsEmailVerified, resendVerificationEmail } from "../../../models/emailVerification";

export interface VerificationWaitDeps {
  isEmailVerified: (email: string) => Promise<boolean>;
  resendVerificationEmail: (email: string) => Promise<boolean>;
  redraw: () => void;
  bringAppToFront: () => void;
  pollMs: number;
}

export class VerificationWait {
  /** The email whose link is awaited; null while nothing is. */
  email: string | null = null;
  /** The last resend's outcome, for the notice; null before one is pressed. */
  isResendSent: boolean | null = null;
  private readonly deps: VerificationWaitDeps;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private isChecking = false;
  // Bumped by every require() and cancel(), so a check answering for a wait
  // that has since been replaced or cancelled is dropped.
  private generation = 0;

  constructor(deps: Partial<VerificationWaitDeps> & Pick<VerificationWaitDeps, "redraw" | "bringAppToFront">) {
    this.deps = {
      isEmailVerified: (email) => fetchIsEmailVerified(email),
      resendVerificationEmail: (email) => resendVerificationEmail(email),
      pollMs: VERIFICATION_POLL_MS,
      ...deps,
    };
  }

  /**
   * Run `onVerified` once `email` is verified: at once when it already is,
   * else when the link is clicked. `onUnchecked` runs instead when the first
   * check cannot be made.
   */
  require(email: string, onVerified: () => void, onUnchecked: () => void): void {
    this.cancel();
    const generation = this.generation;
    this.deps.isEmailVerified(email).then(
      (isVerified) => {
        if (generation !== this.generation) return;
        if (isVerified) {
          onVerified();
          return;
        }
        this.email = email;
        // Signing up sends no verification email (the connector sends the
        // first when it refuses a gated action, which this wait pre-empts), so
        // the wait sends the one its notice tells the user to look for.
        void this.deps.resendVerificationEmail(email);
        this.pollTimer = setInterval(() => void this.poll(generation, email, onVerified), this.deps.pollMs);
        this.deps.redraw();
      },
      (error: unknown) => {
        if (generation !== this.generation) return;
        console.error("Could not check whether the email is verified", error);
        onUnchecked();
      },
    );
  }

  resend(): void {
    const email = this.email;
    if (email === null) return;
    const generation = this.generation;
    void this.deps.resendVerificationEmail(email).then((isSent) => {
      if (generation !== this.generation) return;
      this.isResendSent = isSent;
      this.deps.redraw();
    });
  }

  cancel(): void {
    this.generation += 1;
    if (this.pollTimer !== null) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.isChecking = false;
    this.email = null;
    this.isResendSent = null;
  }

  /** One quiet check while the link is awaited; a failed check simply waits for the next. */
  private async poll(generation: number, email: string, onVerified: () => void): Promise<void> {
    if (this.isChecking) return;
    this.isChecking = true;
    let isVerified = false;
    try {
      isVerified = await this.deps.isEmailVerified(email);
    } catch (error: unknown) {
      console.debug(`[verification] Check failed; retrying on the next poll: ${String(error)}`);
    }
    if (generation !== this.generation) return;
    this.isChecking = false;
    if (!isVerified) return;
    this.cancel();
    // The link is clicked in a browser, which took OS focus with it.
    this.deps.bringAppToFront();
    onVerified();
    this.deps.redraw();
  }
}
