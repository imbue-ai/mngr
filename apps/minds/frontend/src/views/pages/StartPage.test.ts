import type m from "mithril";
import { describe, expect, it } from "vitest";
import {
  EXISTING_LOGIN_LABEL,
  REPORTING_ACCEPT_LABEL,
  RESEND_EMAIL_LABEL,
  answerStep,
  initialStartFlowState,
  observeEmailVerified,
  requireEmailVerification,
  startQuestions,
  undoAnswer,
} from "../../models/startFlow";
import { allText, attrsOf, collectVnodes, createFormDefaults, withAttr } from "../../testing";
import { areDefaultsStale, cloudCreateBody, transcriptTurns } from "./StartPage";

const SIGNED_OUT = { signedInAccount: null, knownAccountIds: [] };
const SIGNED_IN = { signedInAccount: { userId: "user-1", email: "a@b.com" }, knownAccountIds: ["user-1"] };

describe("cloudCreateBody", () => {
  it("is the create form's remote preset with the default account and its region", () => {
    const body = cloudCreateBody(createFormDefaults(), "user-1");
    expect(body).toMatchObject({
      launch_mode: "IMBUE_CLOUD",
      backup_provider: "IMBUE_CLOUD",
      account_id: "user-1",
      region: "US-WEST-OR",
      git_url: "https://github.com/imbue-ai/default-workspace-template.git",
      branch: "minds-v9.9.9",
      color: "#0b292b",
      host_name: "",
    });
  });

  it("runs under the flow's account rather than the default", () => {
    const withBob = createFormDefaults({
      accounts: [
        { user_id: "user-1", email: "alice@example.com" },
        { user_id: "user-2", email: "bob@example.com" },
      ],
    });
    expect(cloudCreateBody(withBob, "user-2")).toMatchObject({ account_id: "user-2" });
  });
});

describe("areDefaultsStale", () => {
  it("re-reads defaults that do not list the flow's account", () => {
    // The route reads the defaults when it mounts, which on a first run is
    // before the account step; a create built from those could not name the
    // account that signed in since.
    expect(areDefaultsStale(createFormDefaults({ accounts: [], default_account_id: "" }), "user-1")).toBe(true);
    expect(areDefaultsStale(createFormDefaults(), "user-2")).toBe(true);
  });

  it("keeps defaults that already list the flow's account", () => {
    expect(areDefaultsStale(createFormDefaults(), "user-1")).toBe(false);
  });

  it("has nothing to keep when none were read", () => {
    expect(areDefaultsStale(null, "user-1")).toBe(true);
  });
});

describe("transcriptTurns", () => {
  const started = startQuestions(initialStartFlowState(), REPORTING_ACCEPT_LABEL);

  it("renders the open question with its table, prompt, both answers, and the existing-account way out", () => {
    const turns = transcriptTurns(started.entries, { isInstant: true, isPressable: true });
    const text = allText(turns);
    expect(text).toContain(REPORTING_ACCEPT_LABEL);
    expect(text).toContain("Let's create your first workspace.");
    expect(text).toContain("How do you want to run it?");
    expect(text).toContain("Recommended");
    const answers = withAttr(turns, "data-answer").map((node) => node.attrs?.["data-answer"]);
    expect(answers).toEqual(["custom", "cloud"]);
    expect(allText(withAttr(turns, "data-aside"))).toContain(EXISTING_LOGIN_LABEL);
  });

  it("renders an answered question as the user's words with an undo, and no buttons", () => {
    const answered = answerStep(started, 1, "cloud", SIGNED_OUT);
    const turns = transcriptTurns(answered.entries, { isInstant: true, isPressable: true, onUndo: () => undefined });
    const text = allText(turns);
    expect(text).toContain("On Imbue Cloud");
    expect(text).toContain("Imbue Cloud it is. A cloud workspace runs on our machines");
    // The where-to-run buttons are gone; the account question's are up, with
    // sign-in as the emphasized answer at the row's right end.
    const answers = withAttr(turns, "data-answer").map((node) => node.attrs?.["data-answer"]);
    expect(answers).toEqual(["signup", "signin"]);
    expect(text).toContain("Sign in with the account you downloaded Imbue Studio with.");
    const undo = collectVnodes(turns).find((node) => node.attrs?.["aria-label"] === "Change answer");
    expect(undo).toBeDefined();
  });

  it("brings a re-opened question's buttons back without the arrival delay", () => {
    const reopened = undoAnswer(answerStep(started, 1, "cloud", SIGNED_OUT), 1);
    const delayOf = (turns: m.Children[]): string =>
      String(
        collectVnodes(turns)
          .filter((node) => withAttr(node, "data-answer").length > 0)
          .map((node) => attrsOf(node).style)
          .find((style) => typeof style === "string"),
      );
    const fresh = transcriptTurns(reopened.entries, { isInstant: true, isPressable: true });
    const undone = transcriptTurns(reopened.entries, { isInstant: true, isPressable: true, reopenedStepIndex: 1 });
    expect(delayOf(fresh)).toMatch(/--start-chat-delay: [1-9]\d+ms/);
    expect(delayOf(undone)).toContain("--start-chat-delay: 0ms");
  });

  it("renders nothing pressable when asked for a read-only transcript", () => {
    const turns = transcriptTurns(started.entries, { isInstant: true, isPressable: false });
    expect(withAttr(turns, "data-answer")).toHaveLength(0);
    expect(withAttr(turns, "data-aside")).toHaveLength(0);
    expect(allText(turns)).toContain("How do you want to run it?");
  });
});

describe("transcriptTurns on the account question with an account signed in", () => {
  const asked = answerStep(startQuestions(initialStartFlowState(), REPORTING_ACCEPT_LABEL), 1, "cloud", SIGNED_IN);

  it("bolds the signed-in email and offers to continue with it or use a different account", () => {
    const turns = transcriptTurns(asked.entries, { isInstant: true, isPressable: true });
    const strongTexts = collectVnodes(turns)
      .filter((node) => node.tag === "strong")
      .map((node) => allText(node));
    expect(strongTexts).toEqual(["a@b.com"]);
    const labels = withAttr(turns, "data-answer").map((node) => allText(node));
    expect(labels).toEqual(["Use a different account", "Continue"]);
  });
});

describe("transcriptTurns on the verification question", () => {
  const waiting = requireEmailVerification(
    answerStep(
      answerStep(startQuestions(initialStartFlowState(), REPORTING_ACCEPT_LABEL), 1, "cloud", SIGNED_IN),
      2,
      "continue",
      SIGNED_IN,
    ),
  );

  it("leads with the requirement in bold, names the address, and offers only the verified button plus a resend", () => {
    const turns = transcriptTurns(waiting.entries, { isInstant: true, isPressable: true });
    const strongTexts = collectVnodes(turns)
      .filter((node) => node.tag === "strong")
      .map((node) => allText(node));
    expect(strongTexts).toContain("You must verify your email");
    expect(allText(turns)).toContain("(click the link emailed to a@b.com when you created your account)");
    expect(withAttr(turns, "data-answer").map((node) => node.attrs?.["data-answer"])).toEqual(["verified"]);
    expect(allText(withAttr(turns, "data-aside"))).toContain(RESEND_EMAIL_LABEL);
  });

  it("puts the resend on the same row as the button", () => {
    const turns = transcriptTurns(waiting.entries, { isInstant: true, isPressable: true });
    const row = collectVnodes(turns).find((node) => withAttr(node, "data-aside").length > 0 && withAttr(node, "data-answer").length > 0);
    expect(row).toBeDefined();
  });

  it("renders the verified answer without an undo", () => {
    const turns = transcriptTurns(observeEmailVerified(waiting).entries, {
      isInstant: true,
      isPressable: true,
      onUndo: () => undefined,
    });
    expect(allText(turns)).toContain("I verified it");
    const undos = collectVnodes(turns).filter((node) => node.attrs?.["aria-label"] === "Change answer");
    // The where-to-run and account answers keep their undo; the verified one has none.
    expect(undos).toHaveLength(2);
  });
});
