// Randomized walks through the start flow: a simulated user presses whatever the flow offers in any order (answers,
// undos, closed modals, relaunches) while a simulated world signs accounts in, verifies emails and refuses creates.
// Every walk drives the same transitions the page calls, and after every step checks that the user cannot be
// stranded and that a cloud create only ever runs under the account the user last chose.
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  FLOW,
  REPORTING_ACCEPT_LABEL,
  answerStep,
  chooseExistingLogin,
  dismissPendingModal,
  finishPendingModal,
  initialStartFlowState,
  noteVerificationEmailSent,
  observeEmailVerified,
  openStepIndex,
  reaskEmailVerification,
  refuseCreateForSignedOutAccount,
  requireEmailVerification,
  settleAwaitedSignIn,
  settleCloudCreate,
  startQuestions,
  undoAnswer,
  undoReporting,
} from "./startFlow";
import type { AnswerContext, ChoiceId, FlowAccount, StartFlowState } from "./startFlow";

/** How the walk ended, once it has: a create went out, or the user left for the home page. */
type Outcome = { kind: "cloud"; accountId: string; isSignedIn: boolean } | { kind: "custom" } | { kind: "home" };

interface World {
  flow: StartFlowState;
  /** Signed-in accounts, oldest first; the oldest is the default, as the backend keeps it while it stays signed in. */
  accounts: FlowAccount[];
  verifiedEmails: ReadonlySet<string>;
  /** The email the browser sign-in reported, until the page consumes it or a new sign-in starts. */
  completedSignInEmail: string;
  willRefuseNextCreate: boolean;
  /** The account the user last chose at the account step, by what they did rather than by what the flow recorded. */
  chosenAccount: FlowAccount | null;
  /** The default account when the cloud answer offered it. */
  offeredAccount: FlowAccount | null;
  outcome: Outcome | null;
  nextAccountNumber: number;
}

type Action =
  | { kind: "report"; isAllowed: boolean }
  | { kind: "press"; pick: number }
  | { kind: "undo"; pick: number }
  | { kind: "undo-reporting" }
  | { kind: "aside" }
  | { kind: "dismiss" }
  | { kind: "sign-in"; isNewAccount: boolean; isVerified: boolean; pick: number }
  | { kind: "verify"; pick: number }
  | { kind: "sign-out"; pick: number }
  | { kind: "submit-custom" }
  | { kind: "refuse-next-create" }
  | { kind: "relaunch" };

function emptyWorld(): World {
  return {
    flow: initialStartFlowState(),
    accounts: [],
    verifiedEmails: new Set(),
    completedSignInEmail: "",
    willRefuseNextCreate: false,
    chosenAccount: null,
    offeredAccount: null,
    outcome: null,
    nextAccountNumber: 1,
  };
}

function pickFrom<T>(options: readonly T[], pick: number): T | undefined {
  return options.length === 0 ? undefined : options[pick % options.length];
}

function knownAccountIds(world: World): string[] {
  return world.accounts.map((account) => account.userId);
}

function answerContext(world: World): AnswerContext {
  return { signedInAccount: world.accounts[0] ?? null, knownAccountIds: knownAccountIds(world) };
}

/**
 * The create goes out, unless its account was signed out since; then it is refused once if the world says so, else
 * accepted, which ends the walk.
 */
function submitCloudCreate(world: World): World {
  const flow = refuseCreateForSignedOutAccount(world.flow, knownAccountIds(world));
  if (!flow.isCloudCreatePending) return { ...world, flow };
  const account = world.flow.account;
  if (account === null) throw new Error("A cloud create went out with no account settled");
  if (world.willRefuseNextCreate) {
    return { ...world, willRefuseNextCreate: false, flow: settleCloudCreate(world.flow, "That did not work: refused.") };
  }
  const isSignedIn = world.accounts.some((existing) => existing.userId === account.userId);
  return {
    ...world,
    flow: settleCloudCreate(world.flow, null),
    outcome: { kind: "cloud", accountId: account.userId, isSignedIn },
  };
}

/** What the page does when a create is owed: check the email, then submit or park it behind the verification question. */
function startCloudCreate(world: World): World {
  if (!world.flow.isCloudCreatePending) return world;
  const account = world.flow.account;
  if (account === null) throw new Error("A cloud create is owed with no account settled");
  if (world.verifiedEmails.has(account.email)) return submitCloudCreate(world);
  return { ...world, flow: requireEmailVerification(world.flow) };
}

/** What the page does on every redraw: let a landed sign-in answer whatever is waiting on one. */
function syncAccounts(world: World): World {
  const pending = world.flow.pending;
  if (pending === "existing-login") {
    return world.completedSignInEmail === "" ? world : { ...world, outcome: { kind: "home" } };
  }
  const settled = settleAwaitedSignIn(world.flow, world.accounts, world.completedSignInEmail);
  if (settled === world.flow) return world;
  return startCloudCreate({ ...world, completedSignInEmail: "", flow: settled });
}

function openChoices(world: World): ChoiceId[] {
  const at = openStepIndex(world.flow);
  if (at === null) return [];
  const entry = world.flow.entries[at];
  return entry.kind === "step" ? FLOW[entry.id].choices.map((choice) => choice.id) : [];
}

function undoableIndexes(world: World): number[] {
  return world.flow.entries.flatMap((entry, index) =>
    entry.kind === "step" && entry.answer !== null && FLOW[entry.id].isUndoable !== false ? [index] : [],
  );
}

function press(world: World, choiceId: ChoiceId): World {
  const at = openStepIndex(world.flow);
  if (at === null) return world;
  if (choiceId === "verified") {
    const email = world.flow.verificationEmail;
    if (email === null) return world;
    if (!world.verifiedEmails.has(email)) return { ...world, flow: reaskEmailVerification(world.flow) };
    return startCloudCreate({ ...world, flow: observeEmailVerified(world.flow) });
  }
  const flow = answerStep(world.flow, at, choiceId, answerContext(world));
  let next: World = { ...world, flow };
  if (choiceId === "cloud") next = { ...next, offeredAccount: world.accounts[0] ?? null };
  if (choiceId === "continue") {
    // Continuing as an account that has since signed out is not a choice: the question is asked again about the
    // account signed in now.
    const offered = world.offeredAccount;
    const isOfferedSignedIn = offered !== null && knownAccountIds(world).includes(offered.userId);
    next = isOfferedSignedIn
      ? { ...next, chosenAccount: offered }
      : { ...next, offeredAccount: world.accounts[0] ?? null };
  }
  // Starting a browser sign-in forgets what the last one reported.
  if (choiceId === "signup" || choiceId === "signin") next = { ...next, completedSignInEmail: "" };
  return startCloudCreate(next);
}

function signIn(world: World, account: FlowAccount, isVerified: boolean): World {
  const isNew = !world.accounts.some((existing) => existing.userId === account.userId);
  const isAwaited = world.flow.pending === "signup" || world.flow.pending === "signin";
  return {
    ...world,
    accounts: isNew ? [...world.accounts, account] : world.accounts,
    verifiedEmails: isVerified ? new Set([...world.verifiedEmails, account.email]) : world.verifiedEmails,
    completedSignInEmail: account.email,
    chosenAccount: isAwaited ? account : world.chosenAccount,
  };
}

function verifyEmail(world: World, email: string): World {
  const verified: World = { ...world, verifiedEmails: new Set([...world.verifiedEmails, email]) };
  // The page polls while it waits on the link, so a verification of the awaited address answers the question.
  if (world.flow.verificationEmail !== email) return verified;
  return startCloudCreate({ ...verified, flow: observeEmailVerified(verified.flow) });
}

function newAccount(world: World): { world: World; account: FlowAccount } {
  const number = world.nextAccountNumber;
  return {
    world: { ...world, nextAccountNumber: number + 1 },
    account: { userId: `user-${number}`, email: `user${number}@example.com` },
  };
}

function act(world: World, action: Action): World {
  if (world.outcome !== null) return world;
  switch (action.kind) {
    case "report": {
      if (world.flow.isStarted) return world;
      return { ...world, flow: startQuestions(world.flow, action.isAllowed ? REPORTING_ACCEPT_LABEL : "No") };
    }
    case "press": {
      const choiceId = pickFrom(openChoices(world), action.pick);
      return choiceId === undefined ? world : press(world, choiceId);
    }
    case "undo": {
      const at = pickFrom(undoableIndexes(world), action.pick);
      if (at === undefined) return world;
      return { ...world, flow: undoAnswer(world.flow, at), chosenAccount: null };
    }
    case "undo-reporting":
      return { ...world, flow: undoReporting(world.flow), chosenAccount: null };
    case "aside": {
      const at = openStepIndex(world.flow);
      const entry = at === null ? undefined : world.flow.entries[at];
      if (entry?.kind !== "step") return world;
      if (entry.id === "run") return { ...world, completedSignInEmail: "", flow: chooseExistingLogin(world.flow) };
      if (entry.id === "verify" || entry.id === "verify-again") {
        return { ...world, flow: noteVerificationEmailSent(world.flow, "sent") };
      }
      return world;
    }
    case "dismiss": {
      // Closing a sign-in modal leaves the flow waiting; closing the custom form or the existing-login one ends the wait.
      const pending = world.flow.pending;
      if (pending === "custom" || pending === "existing-login") return { ...world, flow: dismissPendingModal(world.flow) };
      return world;
    }
    case "sign-in": {
      if (action.isNewAccount || world.accounts.length === 0) {
        const created = newAccount(world);
        return signIn(created.world, created.account, action.isVerified);
      }
      const existing = pickFrom(world.accounts, action.pick);
      return existing === undefined ? world : signIn(world, existing, action.isVerified);
    }
    case "verify": {
      const account = pickFrom(world.accounts, action.pick);
      return account === undefined ? world : verifyEmail(world, account.email);
    }
    case "sign-out": {
      // Signed out from elsewhere: the oldest remaining account becomes the default.
      const account = pickFrom(world.accounts, action.pick);
      if (account === undefined) return world;
      return { ...world, accounts: world.accounts.filter((existing) => existing.userId !== account.userId) };
    }
    case "submit-custom": {
      if (world.flow.pending !== "custom") return world;
      return { ...world, flow: finishPendingModal(world.flow), outcome: { kind: "custom" } };
    }
    case "refuse-next-create":
      return { ...world, willRefuseNextCreate: true };
    case "relaunch":
      // Nothing of the conversation survives a relaunch; the signed-in accounts do.
      return { ...world, flow: initialStartFlowState(), completedSignInEmail: "", chosenAccount: null, offeredAccount: null };
  }
}

function step(world: World, action: Action): World {
  return syncAccounts(act(world, action));
}

/**
 * The moves a user always has: answer what is asked, take an answer back, close a modal, and in the browser sign up
 * as a brand-new, verified account or verify the awaited email.
 */
function rescueMoves(world: World): Action[] {
  const moves: Action[] = [{ kind: "report", isAllowed: true }, { kind: "dismiss" }, { kind: "submit-custom" }];
  openChoices(world).forEach((_, pick) => moves.push({ kind: "press", pick }));
  undoableIndexes(world).forEach((_, pick) => moves.push({ kind: "undo", pick }));
  moves.push({ kind: "sign-in", isNewAccount: true, isVerified: true, pick: 0 });
  world.accounts.forEach((account, pick) => {
    if (account.email === world.flow.verificationEmail) moves.push({ kind: "verify", pick });
  });
  return moves;
}

/** Whether some short sequence of rescue moves gets a cloud workspace created under a brand-new account. */
function canReachCloudCreateUnderNewAccount(world: World, depth: number): boolean {
  const known = new Set(knownAccountIds(world));
  const search = (current: World, remaining: number): boolean => {
    const outcome = current.outcome;
    if (outcome?.kind === "cloud") return !known.has(outcome.accountId);
    if (outcome !== null || remaining === 0) return false;
    return rescueMoves(current).some((move) => {
      const next = step(current, move);
      return next !== current && search(next, remaining - 1);
    });
  };
  // A refusal is the server's doing, not the user's; the rescue assumes the server accepts.
  return search({ ...world, willRefuseNextCreate: false }, depth);
}

// Weighted toward the moves that carry a walk forward, so a fair share of walks get as far as a create.
const actionArbitrary: fc.Arbitrary<Action> = fc.oneof(
  { weight: 2, arbitrary: fc.record({ kind: fc.constant("report" as const), isAllowed: fc.boolean() }) },
  { weight: 8, arbitrary: fc.record({ kind: fc.constant("press" as const), pick: fc.nat(5) }) },
  { weight: 2, arbitrary: fc.record({ kind: fc.constant("undo" as const), pick: fc.nat(5) }) },
  { weight: 1, arbitrary: fc.constant({ kind: "undo-reporting" as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: "aside" as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: "dismiss" as const }) },
  {
    weight: 4,
    arbitrary: fc.record({
      kind: fc.constant("sign-in" as const),
      isNewAccount: fc.boolean(),
      isVerified: fc.boolean(),
      pick: fc.nat(5),
    }),
  },
  { weight: 3, arbitrary: fc.record({ kind: fc.constant("verify" as const), pick: fc.nat(5) }) },
  { weight: 1, arbitrary: fc.record({ kind: fc.constant("sign-out" as const), pick: fc.nat(5) }) },
  { weight: 1, arbitrary: fc.constant({ kind: "submit-custom" as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: "refuse-next-create" as const }) },
  { weight: 1, arbitrary: fc.constant({ kind: "relaunch" as const }) },
);

const walkArbitrary = fc.array(actionArbitrary, { minLength: 10, maxLength: 60, size: "max" });

/** Every state a walk passes through, starting from an empty world, until its outcome. */
function statesOf(actions: Action[]): World[] {
  const states: World[] = [emptyWorld()];
  for (const action of actions) {
    const current = states[states.length - 1];
    if (current.outcome !== null) break;
    states.push(step(current, action));
  }
  return states;
}

describe("randomized walks through the start flow", () => {
  it("never strand the user: a cloud workspace under a fresh account is always a few moves away", () => {
    fc.assert(
      fc.property(walkArbitrary, (actions) => {
        for (const world of statesOf(actions)) {
          if (world.outcome !== null) continue;
          expect(canReachCloudCreateUnderNewAccount(world, 6)).toBe(true);
        }
      }),
      { numRuns: 300 },
    );
  });

  it("only create in the cloud under the account the user last chose, while it is signed in", () => {
    fc.assert(
      fc.property(walkArbitrary, (actions) => {
        const final = statesOf(actions).at(-1);
        if (final?.outcome?.kind !== "cloud") return;
        expect(final.outcome.accountId).toBe(final.chosenAccount?.userId);
        expect(final.outcome.isSignedIn).toBe(true);
      }),
      { numRuns: 1000 },
    );
  });

  it("only ask to verify the email of the account the create will run under", () => {
    fc.assert(
      fc.property(walkArbitrary, (actions) => {
        for (const world of statesOf(actions)) {
          if (world.flow.verificationEmail === null) continue;
          expect(world.flow.verificationEmail).toBe(world.flow.account?.email);
        }
      }),
      { numRuns: 1000 },
    );
  });

  it("finds a cloud create at all in a fair share of walks, so the properties above are not vacuous", () => {
    const outcomes = fc.sample(walkArbitrary, { numRuns: 500, seed: 1 }).map((actions) => statesOf(actions).at(-1)?.outcome);
    expect(outcomes.filter((outcome) => outcome?.kind === "cloud").length).toBeGreaterThan(50);
  });
});
