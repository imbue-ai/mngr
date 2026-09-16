import type m from "mithril";
import { describe, expect, it } from "vitest";
import type { CreateFormDefaults } from "../../models/create";
import {
  EXISTING_LOGIN_LABEL,
  RESEND_EMAIL_LABEL,
  answerStep,
  initialStartFlowState,
  observeEmailVerified,
  requireEmailVerification,
  startQuestions,
  undoAnswer,
} from "../../models/startFlow";
import { allText, attrsOf, collectVnodes, withAttr } from "../../testing";
import { areDefaultsStale, cloudCreateBody, transcriptTurns } from "./StartPage";

function defaults(): CreateFormDefaults {
  return {
    accounts: [{ user_id: "user-1", email: "alice@example.com" }],
    default_account_id: "user-1",
    launch_modes: ["IMBUE_CLOUD", "LIMA", "DOCKER"],
    selected_launch_mode: "IMBUE_CLOUD",
    docker_runtimes: ["RUNC", "RUNSC"],
    selected_docker_runtime: "RUNSC",
    backup_providers: ["IMBUE_CLOUD", "API_KEY", "CONFIGURE_LATER"],
    selected_backup_provider: "IMBUE_CLOUD",
    region_options_by_launch_mode: { IMBUE_CLOUD: ["US-EAST-VA", "US-WEST-OR"] },
    region_selected_by_launch_mode: { IMBUE_CLOUD: "US-WEST-OR" },
    instance_types_by_backend: {},
    default_instance_type_by_backend: {},
    cloud_accounts: [],
    byok_clouds_enabled: false,
    git_url: "https://github.com/imbue-ai/default-workspace-template.git",
    branch: "minds-v9.9.9",
    color: "#0b292b",
    prefill: null,
  };
}

describe("cloudCreateBody", () => {
  it("is the create form's remote preset with the default account and its region", () => {
    const body = cloudCreateBody(defaults());
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
});

describe("areDefaultsStale", () => {
  it("re-reads defaults that list no account once one exists", () => {
    // The route reads the defaults when it mounts, which on a first run is
    // before the account step; a create built from those would carry no
    // account and be refused.
    expect(areDefaultsStale({ ...defaults(), accounts: [], default_account_id: "" }, true)).toBe(true);
  });

  it("keeps defaults that already list the account, and anything read while signed out", () => {
    expect(areDefaultsStale(defaults(), true)).toBe(false);
    expect(areDefaultsStale({ ...defaults(), accounts: [], default_account_id: "" }, false)).toBe(false);
  });

  it("has nothing to keep when none were read", () => {
    expect(areDefaultsStale(null, false)).toBe(true);
  });
});

describe("transcriptTurns", () => {
  const started = startQuestions(initialStartFlowState());

  it("renders the open question with its table, prompt, both answers, and the existing-account way out", () => {
    const turns = transcriptTurns(started.entries, { isInstant: true, isPressable: true });
    const text = allText(turns);
    expect(text).toContain("Sounds great, let's continue");
    expect(text).toContain("Let's create your first workspace.");
    expect(text).toContain("How do you want to run it?");
    expect(text).toContain("Recommended");
    const answers = withAttr(turns, "data-answer").map((node) => node.attrs?.["data-answer"]);
    expect(answers).toEqual(["custom", "cloud"]);
    expect(allText(withAttr(turns, "data-aside"))).toContain(EXISTING_LOGIN_LABEL);
  });

  it("renders an answered question as the user's words with an undo, and no buttons", () => {
    const answered = answerStep(started, 1, "cloud", { isSignedIn: false, signedInEmail: "" });
    const turns = transcriptTurns(answered.entries, { isInstant: true, isPressable: true, onUndo: () => undefined });
    const text = allText(turns);
    expect(text).toContain("On Imbue Cloud");
    expect(text).toContain("Imbue Cloud it is. A cloud workspace runs on our machines");
    // The where-to-run buttons are gone; the account question's are up.
    const answers = withAttr(turns, "data-answer").map((node) => node.attrs?.["data-answer"]);
    expect(answers).toEqual(["signin", "signup"]);
    const undo = collectVnodes(turns).find((node) => node.attrs?.["aria-label"] === "Change answer");
    expect(undo).toBeDefined();
  });

  it("brings a re-opened question's buttons back without the arrival delay", () => {
    const reopened = undoAnswer(answerStep(started, 1, "cloud", { isSignedIn: false, signedInEmail: "" }), 1);
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

describe("transcriptTurns on the verification question", () => {
  const waiting = requireEmailVerification(
    answerStep(startQuestions(initialStartFlowState()), 1, "cloud", { isSignedIn: true, signedInEmail: "a@b.com" }),
    "a@b.com",
  );

  it("leads with the requirement in bold, names the address, and offers only the verified button plus a resend", () => {
    const turns = transcriptTurns(waiting.entries, { isInstant: true, isPressable: true });
    const strong = collectVnodes(turns).find((node) => node.tag === "strong");
    expect(allText(strong)).toBe("You must verify your email");
    expect(allText(turns)).toContain("(click the link sent to a@b.com)");
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
    // Only the where-to-run answer keeps its undo.
    expect(undos).toHaveLength(1);
  });
});
