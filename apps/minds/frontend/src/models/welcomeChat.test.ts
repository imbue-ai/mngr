import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { postWelcomeChat } from "./welcomeChat";

describe("postWelcomeChat", () => {
  it("posts to the attempt's welcome-chat route with no body and answers the chat id", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const chatId = await postWelcomeChat("attempt-1", (url, init) => {
      calls.push({ url, init });
      return Promise.resolve(jsonResponse({ chat_id: "agent-seeded" }));
    });
    expect(chatId).toBe("agent-seeded");
    expect(calls[0].url).toBe("/ui/api/create/attempts/attempt-1/welcome-chat");
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBeUndefined();
  });

  it("answers an empty chat id while the attempt is still being built and the seed is owed", async () => {
    const chatId = await postWelcomeChat("attempt-1", () => Promise.resolve(jsonResponse({ chat_id: "" }, 202)));
    expect(chatId).toBe("");
  });

  it("fails on a refusal, carrying the body's message and the script's verdict, or on an answer without a chat id", async () => {
    await expect(
      postWelcomeChat("attempt-1", () =>
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
      postWelcomeChat("attempt-1", () => Promise.resolve(new Response("not json", { status: 409 }))),
    ).rejects.toThrow(/failed \(409\)$/);
    await expect(postWelcomeChat("attempt-1", () => Promise.resolve(jsonResponse({})))).rejects.toThrow(
      "without a chat id",
    );
  });
});
