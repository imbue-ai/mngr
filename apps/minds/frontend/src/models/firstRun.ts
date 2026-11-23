// The first run (specs/ftue-onboarding-v2): sign in, then the cloud workspace
// starts at once while the welcome greets the user. This module holds the
// sign-in page's stage as a pure reducer and the one window-wide record that
// carries what the sign-in page learned (who signed in, which create it sent)
// over to the creation page.

import type { ResendOutcome } from "./emailVerification";

/** The account the first run settled on: the one the browser sign-in reported. */
export interface FirstRunAccount {
  userId: string;
  email: string;
  displayName: string;
}

/**
 * Where the sign-in page stands.
 *
 * - `sign-in`: the page at rest, its Sign in button live.
 * - `signing-in`: the browser sign-in is open; an account landing moves on.
 * - `landing`: signed in, asking whether the account already has workspaces.
 * - `checking`: no workspaces; checking the email is verified before the create.
 * - `verify`: the email is not verified yet; waiting on the emailed link.
 * - `creating`: the create request is on its way.
 * - `refused`: the create was refused; `message` says why, Try again resends it.
 */
export type FirstRunStage =
  | { kind: "sign-in" }
  | { kind: "signing-in" }
  | { kind: "landing"; account: FirstRunAccount }
  | { kind: "checking"; account: FirstRunAccount }
  | { kind: "verify"; account: FirstRunAccount; lastResend: ResendOutcome | null }
  | { kind: "creating"; account: FirstRunAccount }
  | { kind: "refused"; account: FirstRunAccount; message: string };

export const SIGN_IN_STAGE: FirstRunStage = { kind: "sign-in" };

export function startSignIn(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "sign-in" ? { kind: "signing-in" } : stage;
}

/** The browser sign-in was closed without an account landing. */
export function cancelSignIn(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "signing-in" ? SIGN_IN_STAGE : stage;
}

/** An account landed while the page waited on one. */
export function landAccount(stage: FirstRunStage, account: FirstRunAccount): FirstRunStage {
  if (stage.kind !== "signing-in" && stage.kind !== "sign-in") return stage;
  return { kind: "landing", account };
}

/**
 * The landing verdict: an account with workspaces leaves for its home page
 * (the caller navigates; the stage is left as it was), one without goes on to
 * the email check that precedes its first create.
 */
export function settleLanding(stage: FirstRunStage, hasWorkspaces: boolean): FirstRunStage {
  if (stage.kind !== "landing" || hasWorkspaces) return stage;
  return { kind: "checking", account: stage.account };
}

/** The email check answered: verified goes on to the create, unverified waits on the link. */
export function settleVerification(stage: FirstRunStage, isVerified: boolean): FirstRunStage {
  if (stage.kind !== "checking" && stage.kind !== "verify") return stage;
  if (isVerified) return { kind: "creating", account: stage.account };
  if (stage.kind === "verify") return stage;
  return { kind: "verify", account: stage.account, lastResend: null };
}

export function noteResend(stage: FirstRunStage, outcome: ResendOutcome): FirstRunStage {
  return stage.kind === "verify" ? { ...stage, lastResend: outcome } : stage;
}

export function refuseCreate(stage: FirstRunStage, message: string): FirstRunStage {
  return stage.kind === "creating" ? { kind: "refused", account: stage.account, message } : stage;
}

export function retryCreate(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "refused" ? { kind: "checking", account: stage.account } : stage;
}

/** The account a stage is working under, or null before one has landed. */
export function accountOf(stage: FirstRunStage): FirstRunAccount | null {
  return "account" in stage ? stage.account : null;
}

/** The first word of the account's name, falling back to the part of the email before the @; empty if neither. */
export function firstNameOf(account: Pick<FirstRunAccount, "displayName" | "email"> | null): string {
  if (account === null) return "";
  const fromName = account.displayName.trim().split(/\s+/)[0] ?? "";
  if (fromName !== "") return fromName;
  const local = account.email.split("@")[0] ?? "";
  return local === "" ? "" : local.charAt(0).toUpperCase() + local.slice(1);
}

/** "Welcome, Gleb!", or "Welcome!" with no name to use. */
export function welcomeHeading(firstName: string): string {
  return firstName === "" ? "Welcome!" : `Welcome, ${firstName}!`;
}

/**
 * What the sign-in page hands the creation page: the create it sent and the
 * account it sent it under. One per window; a create submitted elsewhere
 * (the create form, a retry) leaves `createAttemptId` naming another attempt,
 * so the creation page shows its ordinary progress for it.
 */
export class FirstRunRecord {
  createAttemptId = "";
  account: FirstRunAccount | null = null;

  noteCreate(createAttemptId: string, account: FirstRunAccount | null): void {
    this.createAttemptId = createAttemptId;
    this.account = account;
  }

  isFirstRunCreate(createAttemptId: string): boolean {
    return createAttemptId !== "" && this.createAttemptId === createAttemptId;
  }
}

export const firstRun = new FirstRunRecord();
