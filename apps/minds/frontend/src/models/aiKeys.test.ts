import { describe, expect, it } from "vitest";
import { jsonResponse } from "../testing";
import { AiKeysMintModel } from "./aiKeys";

const CONTEXT = {
  workspace_id: "agent-1",
  workspace_display_name: "Research",
  account_email: "person@example.com",
  error_message: "",
};

describe("AiKeysMintModel.load", () => {
  it("offers a re-read after a failed load, and the re-read can succeed", async () => {
    let isFailing = true;
    const urls: string[] = [];
    const model = new AiKeysMintModel(async (input) => {
      urls.push(String(input));
      return isFailing
        ? new Response("Internal Server Error", { status: 500 })
        : jsonResponse(CONTEXT);
    }, () => {});

    await model.load("agent-1");
    const failed = model.loadError;
    isFailing = false;
    await model.load("agent-1");

    expect(failed).toEqual({
      message:
        "Couldn't load this page. Try again. If this persists, report a bug.",
      details: "HTTP 500\nInternal Server Error",
    });
    expect(model.isWindowLoginLapsed).toBe(false);
    expect(model.loadError).toBeNull();
    expect(model.context).toEqual(CONTEXT);
    expect(urls).toEqual([
      "/ui/api/ai-keys?workspace=agent-1",
      "/ui/api/ai-keys?workspace=agent-1",
    ]);
  });

  it("asks for a restart when the read is refused for this window's lapsed login", async () => {
    const model = new AiKeysMintModel(
      async () => jsonResponse({ error: "Not authenticated" }, 401),
      () => {},
    );

    await model.load("agent-1");

    expect(model.isWindowLoginLapsed).toBe(true);
    expect(model.loadError?.message).toBe(
      "Imbue Studio needs to restart to reconnect this window.",
    );
  });
});

describe("AiKeysMintModel.mint", () => {
  async function loadedModel(
    mintAnswer: () => Promise<Response>,
  ): Promise<AiKeysMintModel> {
    const model = new AiKeysMintModel(async (input) =>
      String(input).startsWith("/ui/api/ai-keys")
        ? jsonResponse(CONTEXT)
        : mintAnswer(),
    () => {});
    await model.load("agent-1");
    return model;
  }

  it("shows the route's sentence for a failed mint, with the command's text behind See details", async () => {
    const model = await loadedModel(async () =>
      jsonResponse(
        {
          error:
            "Couldn't create a key for this workspace. Try again. If this persists, report a bug.",
          detail:
            "Failed to create the key: keys litellm create failed (exit 1); see the desktop client logs",
        },
        502,
      ),
    );

    await model.mint();

    expect(model.mintError?.message).toBe(
      "Couldn't create a key for this workspace. Try again. If this persists, report a bug.",
    );
    expect(model.mintError?.details).toContain("HTTP 502");
    expect(model.mintError?.details).toContain("exit 1");
    expect(model.statusMessage).toBe("");
    expect(model.isMinting).toBe(false);
  });

  it.each([
    {
      name: "the app-wide 500's plain text",
      answer: () =>
        new Response("Internal Server Error: KeyError('litellm')", {
          status: 500,
        }),
      details: "HTTP 500\nInternal Server Error: KeyError('litellm')",
    },
    {
      name: "a JSON answer with no sentence of ours",
      answer: () => jsonResponse({ detail: "upstream proxy timeout" }, 504),
      details: "HTTP 504\nupstream proxy timeout",
    },
  ])(
    "names the failed mint, never the answer's own text, for $name",
    async ({ answer, details }) => {
      const model = await loadedModel(async () => answer());

      await model.mint();

      expect(model.mintError).toEqual({
        message:
          "Couldn't create a key for this workspace. Try again. If this persists, report a bug.",
        details,
      });
    },
  );

  it("asks for a restart when the mint is refused for this window's lapsed login", async () => {
    const model = await loadedModel(async () =>
      jsonResponse({ error: "Not authenticated" }, 403),
    );

    await model.mint();

    expect(model.isWindowLoginLapsed).toBe(true);
    expect(model.mintError).toEqual({
      message: "Imbue Studio needs to restart to reconnect this window.",
      details: "HTTP 403\nNot authenticated",
    });
  });

  it("says Imbue Studio didn't respond, with no server named, when the mint gets no answer", async () => {
    const model = await loadedModel(async () => {
      throw new TypeError("Failed to fetch");
    });

    await model.mint();

    expect(model.mintError?.message).toBe(
      "Imbue Studio didn't respond. Select Create key again in a moment.",
    );
    expect(model.isMinting).toBe(false);
  });
});
