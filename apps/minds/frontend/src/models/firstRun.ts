// The first run (specs/onboarding-tweaks): sign in, then the account's first workspace starts at once while the
// creation page welcomes the user. This module holds the sign-in page's stage as a pure reducer, and the words of
// the creation page's heading.

export const SETTING_UP_LINE = "Just a sec, we're setting up your workspace";
/** The setting-up line once the build is known to take minutes, when "just a sec" would not be true. */
export const SETTING_UP_LONG_LINE = "We're setting up your workspace";

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
 * - `landing`: signed in by a hand-off that did not say how many workspaces the account has, so the page asks.
 * - `creating`: the create request is on its way.
 * - `refused`: the create was refused; `message` says why, Try again resends it.
 */
export type FirstRunStage =
  | { kind: "sign-in" }
  | { kind: "signing-in" }
  | { kind: "landing"; account: FirstRunAccount }
  | { kind: "creating"; account: FirstRunAccount }
  | { kind: "refused"; account: FirstRunAccount; message: string };

export const SIGN_IN_STAGE: FirstRunStage = { kind: "sign-in" };

/** Where a landed account goes: its home page, its first workspace's create, or a question the page has to ask. */
export type LandingVerdict = "home" | "create" | "ask";

/** What the sign-in hand-off's workspace count means; null is a hand-off that carried none. */
export function landingVerdict(activeWorkspaceCount: number | null): LandingVerdict {
  if (activeWorkspaceCount === null) return "ask";
  return activeWorkspaceCount > 0 ? "home" : "create";
}

export function startSignIn(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "sign-in" ? { kind: "signing-in" } : stage;
}

/** The browser sign-in was closed without an account landing. */
export function cancelSignIn(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "signing-in" ? SIGN_IN_STAGE : stage;
}

/**
 * An account landed while the page waited on one. An account known to have no workspaces goes straight to its
 * first create; one whose count is unknown is asked about first. (One with workspaces leaves for its home page:
 * the caller navigates, and the stage is left as it was.)
 */
export function landAccount(stage: FirstRunStage, account: FirstRunAccount, verdict: LandingVerdict): FirstRunStage {
  if (stage.kind !== "signing-in" && stage.kind !== "sign-in") return stage;
  switch (verdict) {
    case "home":
      return stage;
    case "create":
      return { kind: "creating", account };
    case "ask":
      return { kind: "landing", account };
  }
}

/** The asked-about account has no workspaces: on to its first create. */
export function settleLanding(stage: FirstRunStage, hasWorkspaces: boolean): FirstRunStage {
  if (stage.kind !== "landing" || hasWorkspaces) return stage;
  return { kind: "creating", account: stage.account };
}

export function refuseCreate(stage: FirstRunStage, message: string): FirstRunStage {
  return stage.kind === "creating" ? { kind: "refused", account: stage.account, message } : stage;
}

export function retryCreate(stage: FirstRunStage): FirstRunStage {
  return stage.kind === "refused" ? { kind: "creating", account: stage.account } : stage;
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
 * The creation page's heading: the welcome for its owner's first workspace, the workspace's name for any later
 * one.
 */
export function creationHeading(isFirstWorkspace: boolean, firstName: string, workspaceName: string): string {
  if (isFirstWorkspace) return welcomeHeading(firstName);
  return workspaceName === "" ? "Setting up your workspace" : `Setting up ${workspaceName}`;
}
