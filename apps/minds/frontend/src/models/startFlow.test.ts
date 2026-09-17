import { describe, expect, it } from "vitest";
import {
  CHAT_BUBBLE_MS,
  CHAT_CONTINUE_GAP_MS,
  CHAT_GAP_MS,
  CHAT_THINK_MS,
  CONTINUE_LABEL,
  MANIFESTO_ANSWER,
  answerStep,
  chooseExistingLogin,
  dismissPendingModal,
  finishPendingModal,
  initialStartFlowState,
  manifestoSchedule,
  noteVerificationEmailSent,
  observeEmailVerified,
  observeSignedIn,
  openStepIndex,
  reaskEmailVerification,
  requireEmailVerification,
  settleCloudCreate,
  signedInSaid,
  startQuestions,
  streamDurationMs,
  undoAnswer,
} from "./startFlow";
import type { StartFlowState } from "./startFlow";

const SIGNED_OUT = { isSignedIn: false, signedInEmail: "" };
const SIGNED_IN = { isSignedIn: true, signedInEmail: "alice@example.com" };

function started(): StartFlowState {
  return startQuestions(initialStartFlowState());
}

describe("the manifesto schedule", () => {
  it("sequences question, answer, and button with the read gaps in between", () => {
    const schedule = manifestoSchedule();
    expect(schedule.questionAt).toBe(CHAT_GAP_MS);
    expect(schedule.answerAt).toBe(CHAT_GAP_MS + CHAT_BUBBLE_MS + CHAT_THINK_MS);
    expect(schedule.continueAt).toBe(schedule.answerAt + streamDurationMs(MANIFESTO_ANSWER) + CHAT_CONTINUE_GAP_MS);
  });

  it("a one-character stream lands after just its fade", () => {
    expect(streamDurationMs("x")).toBe(70);
    expect(streamDurationMs("")).toBe(70);
  });
});

describe("starting the questions", () => {
  it("records the press as a user turn and opens the where-to-run question", () => {
    const state = started();
    expect(state.isStarted).toBe(true);
    expect(state.entries).toEqual([
      { kind: "said", text: CONTINUE_LABEL },
      { kind: "step", id: "run", ack: "", answer: null, said: "" },
    ]);
    expect(openStepIndex(state)).toBe(1);
  });

  it("is idempotent", () => {
    const state = started();
    expect(startQuestions(state)).toBe(state);
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

  it("cloud, already signed in: skips the account question and owes the create", () => {
    const state = answerStep(started(), 1, "cloud", SIGNED_IN);
    expect(state.entries[2]).toEqual({
      kind: "note",
      text: "Imbue Cloud it is. You've signed in as alice@example.com.",
    });
    expect(state.isCloudCreatePending).toBe(true);
    expect(openStepIndex(state)).toBeNull();
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
    const state = observeSignedIn(answerStep(atAccountStep(), 2, "signup", SIGNED_OUT), "bob@example.com");
    expect(state.pending).toBeNull();
    expect(state.entries[2]).toMatchObject({ answer: "signup", said: signedInSaid("bob@example.com") });
    expect(state.entries[3]).toEqual({ kind: "note", text: "You're in." });
    expect(state.isCloudCreatePending).toBe(true);
  });

  it("signing in through the other button is welcomed back instead", () => {
    const state = observeSignedIn(answerStep(atAccountStep(), 2, "signin", SIGNED_OUT), "bob@example.com");
    expect(state.entries[3]).toEqual({ kind: "note", text: "Welcome back." });
  });

  it("an account appearing while nothing waits on one is ignored", () => {
    const state = atAccountStep();
    expect(observeSignedIn(state, "bob@example.com")).toBe(state);
  });

  it("dismissing the sign-in modal leaves the question standing", () => {
    const state = dismissPendingModal(answerStep(atAccountStep(), 2, "signin", SIGNED_OUT));
    expect(state.pending).toBeNull();
    expect(openStepIndex(state)).toBe(2);
  });
});

describe("the cloud create", () => {
  it("a refusal is said as the ack of the where-to-run question, offered again", () => {
    const state = settleCloudCreate(answerStep(started(), 1, "cloud", SIGNED_IN), "That did not work: no capacity.");
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.entries[3]).toEqual({
      kind: "step",
      id: "again",
      ack: "That did not work: no capacity.",
      answer: null,
      said: "",
    });
    expect(openStepIndex(state)).toBe(3);
    // Custom is offered as the way out of a cloud that refused.
    expect(answerStep(state, 3, "custom", SIGNED_IN).pending).toBe("custom");
  });

  it("a success just clears the debt", () => {
    const state = settleCloudCreate(answerStep(started(), 1, "cloud", SIGNED_IN), null);
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.entries).toHaveLength(3);
  });
});

describe("the existing-account way out", () => {
  it("waits on the sign-in and records nothing in the transcript", () => {
    const state = chooseExistingLogin(started());
    expect(state.pending).toBe("existing-login");
    expect(state.entries).toHaveLength(2);
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
  function owingCreate(): StartFlowState {
    return answerStep(started(), 1, "cloud", SIGNED_IN);
  }

  it("parks the create behind a verification question that names the address", () => {
    const state = requireEmailVerification(owingCreate(), "alice@example.com");
    expect(state.isCloudCreatePending).toBe(false);
    expect(state.verificationEmail).toBe("alice@example.com");
    const last = state.entries[state.entries.length - 1];
    expect(last).toMatchObject({ kind: "step", id: "verify", answer: null });
    expect(last.kind === "step" && last.ack).toContain("alice@example.com");
    expect(openStepIndex(state)).toBe(state.entries.length - 1);
  });

  it("does nothing when no create is owed", () => {
    const state = started();
    expect(requireEmailVerification(state, "alice@example.com")).toBe(state);
  });

  it("a verified email answers the question, is acknowledged, and owes the create again", () => {
    const state = observeEmailVerified(requireEmailVerification(owingCreate(), "alice@example.com"));
    expect(state.verificationEmail).toBeNull();
    expect(state.isCloudCreatePending).toBe(true);
    const [answered, note] = state.entries.slice(-2);
    expect(answered).toMatchObject({ kind: "step", id: "verify", answer: "verified", said: "I verified it" });
    expect(note).toMatchObject({ kind: "note", text: "Your email is verified." });
  });

  it("a press that finds the email still unverified re-asks, and a later verification answers the re-ask", () => {
    const reasked = reaskEmailVerification(requireEmailVerification(owingCreate(), "alice@example.com"));
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

  it("says whether the email went out again, and only while the flow waits on one", () => {
    const waiting = requireEmailVerification(owingCreate(), "alice@example.com");
    expect(noteVerificationEmailSent(waiting, true).entries.at(-1)).toMatchObject({
      kind: "note",
      text: "Sent another email to alice@example.com.",
    });
    expect(noteVerificationEmailSent(waiting, false).entries.at(-1)?.kind === "note").toBe(true);
    const notWaiting = started();
    expect(noteVerificationEmailSent(notWaiting, true)).toBe(notWaiting);
  });

  it("the verified press is not itself an answer", () => {
    const waiting = requireEmailVerification(owingCreate(), "alice@example.com");
    expect(answerStep(waiting, waiting.entries.length - 1, "verified", SIGNED_IN)).toBe(waiting);
  });

  it("undo drops the wait along with the answer", () => {
    const waiting = requireEmailVerification(owingCreate(), "alice@example.com");
    expect(undoAnswer(waiting, 1).verificationEmail).toBeNull();
  });
});
