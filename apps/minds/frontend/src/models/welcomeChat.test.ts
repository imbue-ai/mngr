import { describe, expect, it } from "vitest";
import type { CreateAttemptRequestSummary } from "./create";
import type { BuildAnswerId } from "./creationTranscript";
import {
  BUILD_ASK,
  PATH_ASK,
  SETUP_LINE,
  SHOWCASE_APP,
  readyTurnMarkdown,
  showcaseMarkdown,
} from "./creationTranscript";
import {
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
  REPORTING_ACCEPT_LABEL,
  REPORTING_ASK,
  REPORTING_MORE_DETAIL,
} from "./startFlow";
import type { TranscriptEntry } from "./startFlow";
import { jsonResponse } from "../testing";
import {
  NO_ONBOARDING_ANSWERS,
  disclosureMarkdown,
  flowTurns,
  manifestoOpenerMarkdown,
  onboardingQuestionTurns,
  postWelcomeChat,
  tableColumnMarkdown,
  welcomeChatBody,
} from "./welcomeChat";

const REQUEST: CreateAttemptRequestSummary = {
  display_name: "workspace-1",
  launch_mode: "IMBUE_CLOUD",
  cloud_account: "",
  cloud_account_alias: "",
  cloud_account_backend_label: "",
  backup_provider: "IMBUE_CLOUD",
  region: "US-EAST-VA",
  instance_type: "",
  cpu_count: null,
  memory_gb: null,
  disk_gb: null,
  repository: "https://github.com/imbue-ai/default-workspace-template.git",
  branch: "",
  is_onboarding_driven_by_agent: false,
};

/** A cloud create by a returning user. */
const ENTRIES: TranscriptEntry[] = [
  { kind: "said", text: REPORTING_ACCEPT_LABEL },
  { kind: "step", id: "run", ack: "", answer: "cloud", said: "Use Imbue Cloud" },
  {
    kind: "note",
    text: "Imbue Cloud it is. You've signed in as me@example.com.",
  },
];

describe("welcomeChatBody", () => {
  it("opens with the manifesto, follows the flow, and closes with the settings, the setup line and the welcome", () => {
    const body = welcomeChatBody(ENTRIES, REQUEST, false, NO_ONBOARDING_ANSWERS);

    expect(body.title).toBe("Welcome");
    expect(body.turns.map((turn) => turn.role)).toEqual([
      "assistant",
      "user",
      "assistant",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
      "assistant",
    ]);
    expect(body.turns[0].text).toBe(manifestoOpenerMarkdown());
    expect(body.turns[1].text).toBe(MANIFESTO_QUESTION);
    for (const point of MANIFESTO_POINTS) {
      expect(body.turns[2].text).toContain(`<summary>${point.label}</summary>`);
      expect(body.turns[2].text).toContain(point.detail);
    }
    expect(body.turns[3].text).toContain(REPORTING_ASK);
    expect(body.turns[3].text).toContain(REPORTING_MORE_DETAIL);
    expect(body.turns[4].text).toBe(REPORTING_ACCEPT_LABEL);
    expect(body.turns[6].text).toBe("Use Imbue Cloud");
    expect(body.turns[8].text).toContain("Name — workspace-1");
    expect(body.turns[8].text).toContain("Branch — latest");
    expect(body.turns[9].text).toBe(SETUP_LINE);
    expect(body.turns[10].text).toBe(readyTurnMarkdown("problem"));
  });

  it("carries the onboarding questions with their answers, and closes on the welcome the answers chose", () => {
    const built = welcomeChatBody(ENTRIES, REQUEST, false, { isDriven: true, buildAnswer: "yes", pathAnswer: null });
    const tail = built.turns.slice(9);
    expect(tail.map((turn) => [turn.role, turn.text])).toEqual([
      ["assistant", SETUP_LINE],
      ["assistant", BUILD_ASK],
      ["user", "Yes"],
      ["assistant", readyTurnMarkdown("build")],
    ]);

    const examples = welcomeChatBody(ENTRIES, REQUEST, false, {
      isDriven: true,
      buildAnswer: "not-sure",
      pathAnswer: "examples",
    });
    expect(examples.turns.slice(10).map((turn) => [turn.role, turn.text])).toEqual([
      ["assistant", BUILD_ASK],
      ["user", "I’m not sure"],
      ["assistant", PATH_ASK],
      ["user", "See examples"],
      ["assistant", showcaseMarkdown(SHOWCASE_APP)],
      ["assistant", readyTurnMarkdown("examples")],
    ]);
  });
});

describe("onboardingQuestionTurns", () => {
  it("asks nothing for a page that asked nothing, and stops at the open question otherwise", () => {
    expect(onboardingQuestionTurns(NO_ONBOARDING_ANSWERS)).toEqual([]);
    expect(onboardingQuestionTurns({ isDriven: true, buildAnswer: null, pathAnswer: null })).toEqual([
      { role: "assistant", text: BUILD_ASK },
    ]);
    expect(onboardingQuestionTurns({ isDriven: true, buildAnswer: "no", pathAnswer: null })).toEqual([
      { role: "assistant", text: BUILD_ASK },
      { role: "user", text: "No" },
      { role: "assistant", text: PATH_ASK },
    ]);
  });

  it("refuses an answer no question offers", () => {
    const unlisted = "maybe" as unknown as BuildAnswerId;
    expect(() => onboardingQuestionTurns({ isDriven: true, buildAnswer: unlisted, pathAnswer: null })).toThrow(
      "No onboarding choice is called maybe",
    );
  });
});

describe("flowTurns", () => {
  it("says a question with its recommended answer's points as one turn, and an unanswered question with no answer", () => {
    const [ask] = flowTurns([
      { kind: "step", id: "run", ack: "Welcome back.", answer: null, said: "" },
    ]);
    expect(ask.role).toBe("assistant");
    expect(
      ask.text.startsWith("Welcome back. Let's set up your workspace."),
    ).toBe(true);
    expect(ask.text).toContain(
      "**Imbue Cloud** (Recommended)\n- 30 second setup",
    );
    expect(ask.text.endsWith("- Easy to share with others")).toBe(true);
    expect(
      flowTurns([{ kind: "step", id: "run", ack: "", answer: null, said: "" }]),
    ).toHaveLength(1);
  });

  it("bolds a question's lead and keeps the verified answer as the user's own words", () => {
    const turns = flowTurns([
      {
        kind: "step",
        id: "verify",
        ack: "(click the link sent to me@example.com)",
        answer: "verified",
        said: "I verified it",
      },
    ]);
    expect(
      turns[0].text.startsWith(
        "**You must verify your email**\n\n(click the link",
      ),
    ).toBe(true);
    expect(turns[1]).toEqual({ role: "user", text: "I verified it" });
  });

  it("bolds the signed-in email the account question names", () => {
    const [ask] = flowTurns([
      {
        kind: "step",
        id: "account",
        ack: "Imbue Cloud it is. You're signed in as me@example.com.",
        answer: null,
        said: "",
        email: "me@example.com",
      },
    ]);
    expect(ask.text).toBe(
      "Imbue Cloud it is. You're signed in as **me@example.com**. Continue with this account or use a different one?",
    );
  });
});

describe("the markdown pieces", () => {
  it("renders a point as a details toggle and a column as a titled list", () => {
    expect(
      disclosureMarkdown({ id: "a", label: "is loyal", detail: "Always." }),
    ).toBe("<details><summary>is loyal</summary>\n\nAlways.\n\n</details>");
    expect(
      tableColumnMarkdown({
        title: "Custom",
        isEmphasized: false,
        points: ["one", "two"],
      }),
    ).toBe("**Custom**\n- one\n- two");
  });
});

describe("postWelcomeChat", () => {
  it("posts the body to the attempt's welcome-chat route and answers the chat id", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const chatId = await postWelcomeChat(
      "attempt-1",
      { title: "Welcome", turns: [{ role: "user", text: "hi" }] },
      (url, init) => {
        calls.push({ url, init });
        return Promise.resolve(jsonResponse({ chat_id: "agent-seeded" }));
      },
    );
    expect(chatId).toBe("agent-seeded");
    expect(calls[0].url).toBe("/ui/api/create/attempts/attempt-1/welcome-chat");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      title: "Welcome",
      turns: [{ role: "user", text: "hi" }],
    });
  });

  it("answers an empty chat id while the attempt is still being built and the conversation is held", async () => {
    const chatId = await postWelcomeChat(
      "attempt-1",
      { title: "Welcome", turns: [{ role: "user", text: "hi" }] },
      () => Promise.resolve(jsonResponse({ chat_id: "" }, 202)),
    );
    expect(chatId).toBe("");
  });

  it("fails on a refusal, carrying the body's message and the script's verdict, or on an answer without a chat id", async () => {
    await expect(
      postWelcomeChat("attempt-1", { title: "", turns: [] }, () =>
        Promise.resolve(
          jsonResponse(
            {
              error: "Couldn't open the welcome chat in the workspace.",
              detail: "the chat app answered HTTP 400",
            },
            502,
          ),
        ),
      ),
    ).rejects.toThrow(
      "failed (502): Couldn't open the welcome chat in the workspace. -- the chat app answered HTTP 400",
    );
    await expect(
      postWelcomeChat("attempt-1", { title: "", turns: [] }, () =>
        Promise.resolve(new Response("not json", { status: 409 })),
      ),
    ).rejects.toThrow(/failed \(409\)$/);
    await expect(
      postWelcomeChat("attempt-1", { title: "", turns: [] }, () =>
        Promise.resolve(jsonResponse({})),
      ),
    ).rejects.toThrow("without a chat id");
  });
});
