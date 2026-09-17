import { describe, expect, it } from "vitest";
import type { CreateAttemptRequestSummary } from "./create";
import { SETUP_LINE, readyTurnMarkdown } from "./creationTranscript";
import {
  CONTINUE_LABEL,
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
} from "./startFlow";
import type { TranscriptEntry } from "./startFlow";
import { jsonResponse } from "../testing";
import {
  disclosureMarkdown,
  flowTurns,
  postWelcomeChat,
  tableColumnMarkdown,
  welcomeChatBody,
} from "./welcomeChat";

const REQUEST: CreateAttemptRequestSummary = {
  display_name: "workspace-1",
  launch_mode: "IMBUE_CLOUD",
  cloud_account: "",
  backup_provider: "IMBUE_CLOUD",
  region: "US-EAST-VA",
  instance_type: "",
  repository: "https://github.com/imbue-ai/default-workspace-template.git",
  branch: "",
};

/** A cloud create by a returning user: the continue press, the where-to-run question, its answer, the receipt. */
const ENTRIES: TranscriptEntry[] = [
  { kind: "said", text: CONTINUE_LABEL },
  { kind: "step", id: "run", ack: "", answer: "cloud", said: "On Imbue Cloud" },
  {
    kind: "note",
    text: "Imbue Cloud it is. You've signed in as me@example.com.",
  },
];

describe("welcomeChatBody", () => {
  it("opens with the manifesto, follows the flow, and closes with the settings, the setup line and the ready list", () => {
    const body = welcomeChatBody(ENTRIES, REQUEST, false);

    expect(body.title).toBe("Welcome");
    expect(body.turns.map((turn) => turn.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
      "assistant",
    ]);
    expect(body.turns[0].text).toBe(MANIFESTO_QUESTION);
    for (const point of MANIFESTO_POINTS) {
      expect(body.turns[1].text).toContain(`<summary>${point.label}</summary>`);
      expect(body.turns[1].text).toContain(point.detail);
    }
    expect(body.turns[2].text).toBe(CONTINUE_LABEL);
    expect(body.turns[4].text).toBe("On Imbue Cloud");
    expect(body.turns[6].text).toContain("Name — workspace-1");
    expect(body.turns[6].text).toContain("Branch — latest");
    expect(body.turns[7].text.startsWith(SETUP_LINE)).toBe(true);
    expect(body.turns[7].text).toContain(
      "[How workspaces work](https://imbue.com/product/mind)",
    );
    expect(body.turns[8].text).toBe(readyTurnMarkdown());
  });
});

describe("flowTurns", () => {
  it("says a question with its comparison and prompt as one turn, and an unanswered question with no answer", () => {
    const [ask] = flowTurns([
      { kind: "step", id: "run", ack: "Welcome back.", answer: null, said: "" },
    ]);
    expect(ask.role).toBe("assistant");
    expect(
      ask.text.startsWith("Welcome back. Let's create your first workspace."),
    ).toBe(true);
    expect(ask.text).toContain(
      "**Imbue Cloud** (Recommended)\n- 30 second setup",
    );
    expect(ask.text).toContain("**Custom setup** (Advanced)\n- Runs on your computer");
    expect(ask.text.endsWith("How do you want to run it?")).toBe(true);
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

  it("fails on a refusal, carrying the body's message and the script's verdict, or on an answer without a chat id", async () => {
    await expect(
      postWelcomeChat("attempt-1", { title: "", turns: [] }, () =>
        Promise.resolve(
          jsonResponse(
            { error: "Couldn't open the welcome chat in the workspace.", detail: "the chat app answered HTTP 400" },
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
