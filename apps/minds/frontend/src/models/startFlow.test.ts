import { describe, expect, it } from "vitest";
import {
  CHAT_BUBBLE_MS,
  CHAT_GAP_MS,
  CHAT_READ_MS,
  CHAT_REACT_MS,
  CHAT_THINK_MS,
  FLOW_OPTIONS_GAP_MS,
  MANIFESTO_ANSWER,
  MANIFESTO_OPENER,
  REPORTING_ACCEPT_LABEL,
  REPORTING_ASK,
  REPORTING_DECLINE_LABEL,
  answerStep,
  chooseExistingLogin,
  continueAsSaid,
  dismissPendingModal,
  finishPendingModal,
  initialStartFlowState,
  landedAccount,
  manifestoSchedule,
  noteVerificationEmailSent,
  observeEmailVerified,
  observeSignedIn,
  openStepIndex,
  reaskEmailVerification,
  refuseCreateForSignedOutAccount,
  requireEmailVerification,
  settleCloudCreate,
  signedInSaid,
  startQuestions,
  streamDurationMs,
  undoAnswer,
  undoReporting,
} from "./startFlow";
import type { StartFlowState } from "./startFlow";

const ALICE = { userId: "user-alice", email: "alice@example.com" };
const BOB = { userId: "user-bob", email: "bob@example.com" };
const SIGNED_OUT = { signedInAccount: null, knownAccountIds: [] };
const SIGNED_IN = { signedInAccount: ALICE, knownAccountIds: [ALICE.userId] };

function started(): StartFlowState {
  return startQuestions(initialStartFlowState(), REPORTING_ACCEPT_LABEL);
}

/** Cloud with Alice already signed in, kept at the account step: the create is owed under her. */
function owingCreate(): StartFlowState {
  return answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "continue", SIGNED_IN);
}

describe("the manifesto schedule", () => {
  it("sequences opener, question, answer, the reporting question and its answers with the read gaps between", () => {
    const schedule = manifestoSchedule();
    expect(schedule.openerAt).toBe(CHAT_GAP_MS);
    expect(schedule.questionAt).toBe(CHAT_GAP_MS + streamDurationMs(MANIFESTO_OPENER) + CHAT_REACT_MS);
    expect(schedule.answerAt).toBe(schedule.questionAt + CHAT_BUBBLE_MS + CHAT_THINK_MS);
    expect(schedule.reportingAt).toBe(schedule.answerAt + streamDurationMs(MANIFESTO_ANSWER) + CHAT_READ_MS);
    expect(schedule.answersAt).toBe(schedule.reportingAt + streamDurationMs(REPORTING_ASK) + FLOW_OPTIONS_GAP_MS);
  });

  it("a one-character stream lands after just its fade", () => {
    expect(streamDurationMs("x")).toBe(70);
    expect(streamDurationMs("")).toBe(70);
  });
});

describe("starting the questions", () => {
  it("records the reporting answer as a user turn and opens the where-to-run question", () => {
    const state = started();
    expect(state.isStarted).toBe(true);
    expect(state.entries).toEqual([
      { kind: "said", text: REPORTING_ACCEPT_LABEL },
      { kind: "step", id: "run", ack: "", answer: null, said: "" },
    ]);
    expect(openStepIndex(state)).toBe(1);
  });

  it("records a refusal the same way, so the transcript carries which answer was given", () => {
    const state = startQuestions(initialStartFlowState(), REPORTING_DECLINE_LABEL);
    expect(state.entries[0]).toEqual({ kind: "said", text: REPORTING_DECLINE_LABEL });
  });

  it("is idempotent", () => {
    const state = started();
    expect(startQuestions(state, REPORTING_DECLINE_LABEL)).toBe(state);
  });
});

describe("answering where to run", () => {
  it("cloud, signed out: the answer lands and the account question follows with the ack", () => {
    const state = answerStep(started(), 1, "cloud", SIGNED_OUT);
    expect(state.entries[1]).toMatchObject({ kind: "step", id: "run", answer: "cloud", said: "On Imbue Cloud" });
    expect(state.entries[2]).toEqual({ kind: "step", id: "auth", ack: "Imbue Cloud it is.", answer: null, said: "" });
    expect(state.isCloudCreatePending).toBe(false);
    expect(openStepIndex(state)).toBe(2);
  });

  it("cloud, already signed in: asks the account question, naming the signed-in account", () => {
    const state = answerStep(started(), 1, "cloud", SIGNED_IN);
    expect(state.entries[2]).toEqual({
      kind: "step",
      id: "account",
      ack: "Imbue Cloud it is. You're signed in as alice@example.com.",
      answer: null,
      said: "",
      email: "alice@example.com",
    });
    expect(state.isCloudCreatePending).toBe(false);
    expect(openStepIndex(state)).toBe(2);
  });

  it("continuing as the signed-in account answers the question and owes the create under it", () => {
    const state = owingCreate();
    expect(state.entries[2]).toMatchObject({ id: "account", answer: "continue", said: continueAsSaid(ALICE.email) });
    expect(state.entries[3]).toEqual({ kind: "note", text: "Welcome back." });
    expect(state.account).toEqual(ALICE);
    expect(state.isCloudCreatePending).toBe(true);
  });

  it("custom: the answer lands, the agent acknowledges, and the form is pending", () => {
    const state = answerStep(started(), 1, "custom", SIGNED_OUT);
    expect(state.entries[1]).toMatchObject({ answer: "custom", said: "Custom setup" });
    expect(state.entries[2]).toEqual({ kind: "note", text: "Your own platform it is." });
    expect(state.pending).toBe("custom");
  });

  it("ignores a choice the question does not offer, and an index that is not a question", () => {
    const state = started();
    expect(answerStep(state, 1, "signup", SIGNED_OUT)).toBe(state);
    expect(answerStep(state, 0, "cloud", SIGNED_OUT)).toBe(state);
    expect(answerStep(state, 9, "cloud", SIGNED_OUT)).toBe(state);
  });
});

describe("the custom form", () => {
  it("closing it without finishing asks the retry question, which offers the same two answers", () => {
    const state = dismissPendingModal(answerStep(started(), 1, "custom", SIGNED_OUT));
    expect(state.pending).toBeNull();
    const last = state.entries[state.entries.length - 1];
    expect(last).toEqual({ kind: "step", id: "retry", ack: "", answer: null, said: "" });
    // Custom is still among the answers: backing out is not a door closing on it.
    const again = answerStep(state, state.entries.length - 1, "custom", SIGNED_OUT);
    expect(again.pending).toBe("custom");
  });

  it("finishing it clears the wait without a retry question", () => {
    const state = finishPendingModal(answerStep(started(), 1, "custom", SIGNED_OUT));
    expect(state.pending).toBeNull();
    expect(state.entries.some((entry) => entry.kind === "step" && entry.id === "retry")).toBe(false);
  });

  it("dismissing with nothing pending changes nothing", () => {
    const state = started();
    expect(dismissPendingModal(state)).toBe(state);
  });
});

describe("the account step", () => {
  function atAccountStep(): StartFlowState {
    return answerStep(started(), 1, "cloud", SIGNED_OUT);
  }

  it("a sign-in button records nothing until the modal reports an account", () => {
    const state = answerStep(atAccountStep(), 2, "signup", SIGNED_OUT);
    expect(state.pending).toBe("signup");
    expect(state.entries[2]).toMatchObject({ answer: null });
  });

  it("an account appearing turns the buttons into the receipt, acknowledges, and owes the create", () => {
    const state = observeSignedIn(answerStep(atAccountStep(), 2, "signup", SIGNED_OUT), BOB);
    expect(state.pending).toBeNull();
    expect(state.account).toEqual(BOB);
    expect(state.entries[2]).toMatchObject({ answer: "signup", said: signedInSaid("bob@example.com") });
    expect(state.entries[3]).toEqual({ kind: "note", text: "You're in." });
    expect(state.isCloudCreatePending).toBe(true);
  });

  it("switching accounts from the signed-in question is acknowledged without guessing sign-in or sign-up", () => {
    const switching = answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "signin", SIGNED_IN);
    const state = observeSignedIn(switching, BOB);
    expect(state.entries[3]).toEqual({ kind: "note", text: "You're in." });
    expect(state.account).toEqual(BOB);
  });

  it("signing in through the other button is welcomed back instead", () => {
    const state = observeSignedIn(answerStep(atAccountStep(), 2, "signin", SIGNED_OUT), BOB);
    expect(state.entries[3]).toEqual({ kind: "note", text: "Welcome back." });
  });

  it("an account appearing while nothing waits on one is ignored", () => {
    const state = atAccountStep();
    expect(observeSignedIn(state, BOB)).toBe(state);
  });

  it("dismissing the sign-in modal leaves the question standing", () => {
    const state = dismissPendingModal(answerStep(atAccountStep(), 2, "signin", SIGNED_OUT));
    expect(state.pending).toBeNull();
    expect(openStepIndex(state)).toBe(2);
  });
});

describe("which account answers a sign-in", () => {
  it("is an account that was not signed in when the button was pressed", () => {
    const waiting = answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "signin", SIGNED_IN);
    expect(landedAccount(waiting, [ALICE], "")).toBeNull();
    expect(landedAccount(waiting, [ALICE, BOB], "")).toEqual(BOB);
  });

  it("is the account the sign-in reports, even one that was already signed in", () => {
    const waiting = answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "signin", SIGNED_IN);
    expect(landedAccount(waiting, [ALICE], ALICE.email)).toEqual(ALICE);
  });

  it("is nothing while no sign-in is awaited", () => {
    expect(landedAccount(started(), [ALICE, BOB], BOB.email)).toBeNull();
  });

  it("an unverified first account does not answer a second sign-up after an undo", () => {
    // Sign up as Alice, who never verifies: the create waits on her email.
    const atAuth = answerStep(started(), 1, "cloud", SIGNED_OUT);
    const asAlice = observeSignedIn(answerStep(atAuth, 2, "signup", SIGNED_OUT), ALICE);
    const parked = requireEmailVerification(asAlice);
    expect(parked.verificationEmail).toBe(ALICE.email);
    // Take the account answer back and create another account.
    const reopened = undoAnswer(parked, 2);
    expect(reopened.account).toBeNull();
    const waiting = answerStep(reopened, 2, "signup", SIGNED_IN);
    // Alice being signed in already settles nothing; Bob landing does.
    expect(landedAccount(waiting, [ALICE], "")).toBeNull();
    const landed = landedAccount(waiting, [ALICE, BOB], "");
    expect(landed).toEqual(BOB);
    const asBob = requireEmailVerification(observeSignedIn(waiting, BOB));
    expect(asBob.account).toEqual(BOB);
    expect(asBob.verificationEmail).toBe(BOB.email);
  });

  it("continuing as an account signed out since it was offered asks again about the account signed in now", () => {
    const offered = answerStep(started(), 1, "cloud", SIGNED_IN);
    const bobOnly = { signedInAccount: BOB, knownAccountIds: [BOB.userId] };
    const reasked = answerStep(offered, 2, "continue", bobOnly);
    expect(reasked.entries[2]).toMatchObject({ id: "account", answer: null, ack: "Imbue Cloud it is. You're signed in as bob@example.com." });
    expect(reasked.isCloudCreatePending).toBe(false);
    expect(answerStep(reasked, 2, "continue", bobOnly).account).toEqual(BOB);
    // With nobody signed in any more, it asks for an account instead.
    expect(answerStep(offered, 2, "continue", SIGNED_OUT).entries[2]).toMatchObject({ id: "auth", answer: null });
  });

  it("continuing after a sign-in was started and closed replaces that sign-in", () => {
    const abandoned = answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "signin", SIGNED_IN);
    const continued = answerStep(abandoned, 2, "continue", SIGNED_IN);
    expect(continued.pending).toBeNull();
    // The abandoned sign-in landing later answers nothing, so no second create is owed.
    expect(landedAccount(continued, [ALICE, BOB], BOB.email)).toBeNull();
    expect(observeSignedIn(continued, BOB)).toBe(continued);
  });

  it("continuing as a signed-out account after a sign-in was started and closed replaces that sign-in", () => {
    const abandoned = answerStep(answerStep(started(), 1, "cloud", SIGNED_IN), 2, "signin", SIGNED_IN);
    const reasked = answerStep(abandoned, 2, "continue", SIGNED_OUT);
    expect(reasked.pending).toBeNull();
    expect(observeSignedIn(reasked, BOB)).toBe(reasked);
  });

  it("asking again after an undo of the cloud answer offers a different account", () => {
    const parked = requireEmailVerification(owingCreate());
    const again = answerStep(undoAnswer(parked, 1), 1, "cloud", SIGNED_IN);
    expect(again.entries[2]).toMatchObject({ id: "account", answer: null });
    const waiting = answerStep(again, 2, "signin", SIGNED_IN);
    expect(waiting.pending).toBe("signin");
    expect(observeSignedIn(waiting, BOB).account).toEqual(BOB);
  });
});

describe("the cloud create", () => {
  it("a refusal is said as the ack of the where-to-run question, offered again", () => {
    const state = settleCloudCreate(owingCreate(), "That did not work: no capacity.");
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.entries[4]).toEqual({
      kind: "step",
      id: "again",
      ack: "That did not work: no capacity.",
      answer: null,
      said: "",
    });
    expect(openStepIndex(state)).toBe(4);
    // Custom is offered as the way out of a cloud that refused.
    expect(answerStep(state, 4, "custom", SIGNED_IN).pending).toBe("custom");
  });

  it("an account signed out since the account step settled on it is refused before the create goes out", () => {
    const owing = owingCreate();
    expect(refuseCreateForSignedOutAccount(owing, [ALICE.userId])).toBe(owing);
    const refused = refuseCreateForSignedOutAccount(owing, [BOB.userId]);
    expect(refused.isCloudCreatePending).toBe(false);
    expect(refused.entries.at(-1)).toMatchObject({
      id: "again",
      ack: "That did not work: alice@example.com is no longer signed in.",
      answer: null,
    });
    // Asked again, the cloud answer offers the account signed in now.
    const bobOnly = { signedInAccount: BOB, knownAccountIds: [BOB.userId] };
    const again = answerStep(refused, refused.entries.length - 1, "cloud", bobOnly);
    expect(again.entries.at(-1)).toMatchObject({ id: "account", ack: "Imbue Cloud it is. You're signed in as bob@example.com." });
  });

  it("a success just clears the debt", () => {
    const state = settleCloudCreate(owingCreate(), null);
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.entries).toHaveLength(4);
  });
});

describe("the existing-account way out", () => {
  it("waits on the sign-in and records nothing in the transcript", () => {
    const state = chooseExistingLogin(started());
    expect(state.pending).toBe("existing-login");
    expect(state.entries).toHaveLength(2);
  });
});

describe("undoing the reporting answer", () => {
  it("drops the whole conversation and asks the reporting question again", () => {
    const deep = requireEmailVerification(owingCreate());
    expect(undoReporting(deep)).toEqual(initialStartFlowState());
  });

  it("changes nothing before the reporting question is answered", () => {
    const fresh = initialStartFlowState();
    expect(undoReporting(fresh)).toBe(fresh);
  });
});

describe("undo", () => {
  it("takes the answer back and drops everything said after it", () => {
    const answered = answerStep(started(), 1, "cloud", SIGNED_OUT);
    const state = undoAnswer(answered, 1);
    expect(state.entries).toHaveLength(2);
    expect(state.entries[1]).toEqual({ kind: "step", id: "run", ack: "", answer: null, said: "" });
    expect(state.pending).toBeNull();
    expect(state.isCloudCreatePending).toBe(false);
    expect(openStepIndex(state)).toBe(1);
  });

  it("ignores an index that is not a question", () => {
    const state = started();
    expect(undoAnswer(state, 0)).toBe(state);
  });
});

describe("the email verification gate", () => {
  it("parks the create behind a verification question that names the address", () => {
    const state = requireEmailVerification(owingCreate());
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.verificationEmail).toBe("alice@example.com");
    const last = state.entries[state.entries.length - 1];
    expect(last).toMatchObject({ kind: "step", id: "verify", answer: null });
    expect(last.kind === "step" && last.ack).toContain("alice@example.com");
    expect(openStepIndex(state)).toBe(state.entries.length - 1);
  });

  it("does nothing when no create is owed", () => {
    const state = started();
    expect(requireEmailVerification(state)).toBe(state);
  });

  it("a verified email answers the question, is acknowledged, and owes the create again", () => {
    const state = observeEmailVerified(requireEmailVerification(owingCreate()));
    expect(state.verificationEmail).toBeNull();
    expect(state.isCloudCreatePending).toBe(true);
    const [answered, note] = state.entries.slice(-2);
    expect(answered).toMatchObject({ kind: "step", id: "verify", answer: "verified", said: "I verified it" });
    expect(note).toMatchObject({ kind: "note", text: "Your email is verified." });
  });

  it("a press that finds the email still unverified re-asks, and a later verification answers the re-ask", () => {
    const reasked = reaskEmailVerification(requireEmailVerification(owingCreate()));
    expect(reasked.verificationEmail).toBe("alice@example.com");
    expect(reasked.isCloudCreatePending).toBe(false);
    expect(reasked.entries.slice(-2)).toMatchObject([
      { kind: "step", id: "verify", answer: "verified" },
      { kind: "step", id: "verify-again", answer: null },
    ]);
    const verified = observeEmailVerified(reasked);
    expect(verified.entries[verified.entries.length - 2]).toMatchObject({ id: "verify-again", answer: "verified" });
    expect(verified.isCloudCreatePending).toBe(true);
  });

  it("says what became of the resend, and only while the flow waits on one", () => {
    const waiting = requireEmailVerification(owingCreate());
    expect(noteVerificationEmailSent(waiting, "sent").entries.at(-1)).toMatchObject({
      kind: "note",
      text: "Sent another email to alice@example.com.",
    });
    expect(noteVerificationEmailSent(waiting, "suppressed").entries.at(-1)).toMatchObject({
      text: "An email went out to alice@example.com moments ago. Check your inbox and spam folder.",
    });
    // A failure must not point the user at an inbox holding nothing.
    expect(noteVerificationEmailSent(waiting, "failed").entries.at(-1)).toMatchObject({
      text: "Could not send the email to alice@example.com. Please try again.",
    });
    const notWaiting = started();
    expect(noteVerificationEmailSent(notWaiting, "sent")).toBe(notWaiting);
  });

  it("the verified press is not itself an answer", () => {
    const waiting = requireEmailVerification(owingCreate());
    expect(answerStep(waiting, waiting.entries.length - 1, "verified", SIGNED_IN)).toBe(waiting);
  });

  it("undo drops the wait along with the answer", () => {
    const waiting = requireEmailVerification(owingCreate());
    expect(undoAnswer(waiting, 1).verificationEmail).toBeNull();
  });
});
