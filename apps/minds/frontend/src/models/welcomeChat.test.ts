import { describe, expect, it } from "vitest";
import type { CreateAttemptRequestSummary } from "./create";
import {
  SETUP_LINE,
  SHOWCASE_APP,
  readyTurnMarkdown,
  showcaseMarkdown,
} from "./creationTranscript";
import { MANIFESTO_POINTS, MANIFESTO_QUESTION, REPORTING_ASK, REPORTING_MORE_DETAIL } from "./manifesto";
import { jsonResponse } from "../testing";
import {
  disclosureMarkdown,
  firstRunWelcomeChatBody,
  manifestoOpenerMarkdown,
  postWelcomeChat,
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

describe("welcomeChatBody", () => {
  it("opens with the manifesto and closes with the settings, the setup line and the welcome", () => {
    const body = welcomeChatBody(REQUEST, false);

    expect(body.title).toBe("Welcome");
    expect(body.turns.map((turn) => turn.role)).toEqual([
      "assistant",
      "user",
      "assistant",
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
    expect(body.turns[4].text).toContain("Name — workspace-1");
    expect(body.turns[4].text).toContain("Branch — latest");
    expect(body.turns[5].text).toBe(SETUP_LINE);
    expect(body.turns[6].text).toBe(readyTurnMarkdown());
    expect(body.is_first_run).toBeUndefined();
  });

  it("carries the recording between the setup line and the welcome for an onboarding create", () => {
    const built = welcomeChatBody(REQUEST, true);
    expect(built.turns.slice(5).map((turn) => [turn.role, turn.text])).toEqual([
      ["assistant", SETUP_LINE],
      ["assistant", showcaseMarkdown(SHOWCASE_APP)],
      ["assistant", readyTurnMarkdown()],
    ]);
  });
});

describe("firstRunWelcomeChatBody", () => {
  it("sends no turns and flags the arrival", () => {
    expect(firstRunWelcomeChatBody()).toEqual({ title: "Welcome", turns: [], is_first_run: true });
  });
});

describe("the markdown pieces", () => {
  it("renders a point as a details toggle", () => {
    expect(disclosureMarkdown({ id: "a", label: "is loyal", detail: "Always." })).toBe(
      "<details><summary>is loyal</summary>\n\nAlways.\n\n</details>",
    );
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
