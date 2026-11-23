import { describe, expect, it } from "vitest";
import type { RequestResult } from "./errors";
import type { SuggestionSettings } from "./suggestionSettings";
import { SuggestionSettingsModel, suggestionSettingsFromBody } from "./suggestionSettings";

const AGENT = "agent-" + "c".repeat(8);
const URL = `/ui/api/workspaces/${AGENT}/suggestion-settings`;
const ALL_ON: SuggestionSettings = {
  is_next_step_suggestions_enabled: true,
  is_new_chat_suggestions_enabled: true,
};

interface Sent {
  url: string;
  method: string;
  body: unknown;
}

/** A model whose requests are answered by hand, in the order they were sent. */
function harness(): {
  model: SuggestionSettingsModel;
  sent: Sent[];
  answer: (result: RequestResult) => Promise<void>;
  redraws: () => number;
} {
  const sent: Sent[] = [];
  const resolvers: ((result: RequestResult) => void)[] = [];
  let redrawCount = 0;
  const model = new SuggestionSettingsModel(
    AGENT,
    (url, init) => {
      sent.push({
        url,
        method: init?.method ?? "GET",
        body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
      });
      return new Promise<RequestResult>((resolve) => {
        resolvers.push(resolve);
      });
    },
    () => {
      redrawCount += 1;
    },
  );
  return {
    model,
    sent,
    answer: async (result) => {
      const resolve = resolvers.shift();
      if (resolve === undefined) throw new Error("no request is waiting for an answer");
      resolve(result);
      await Promise.resolve();
      await Promise.resolve();
    },
    redraws: () => redrawCount,
  };
}

async function readyHarness(settings: SuggestionSettings = ALL_ON): Promise<ReturnType<typeof harness>> {
  const built = harness();
  const loading = built.model.load();
  await built.answer({ ok: true, status: 200, body: settings });
  await loading;
  return built;
}

describe("suggestionSettingsFromBody", () => {
  it("reads the two switches and nothing else", () => {
    expect(suggestionSettingsFromBody({ ...ALL_ON, is_new_chat_suggestions_enabled: false, extra: 1 })).toEqual({
      ...ALL_ON,
      is_new_chat_suggestions_enabled: false,
    });
  });

  it("answers null for a body missing a switch, carrying a non-boolean, or not an object", () => {
    expect(suggestionSettingsFromBody({ is_next_step_suggestions_enabled: true })).toBeNull();
    expect(suggestionSettingsFromBody({ ...ALL_ON, is_new_chat_suggestions_enabled: "true" })).toBeNull();
    expect(suggestionSettingsFromBody(null)).toBeNull();
    expect(suggestionSettingsFromBody("on")).toBeNull();
  });
});

describe("SuggestionSettingsModel", () => {
  it("is loading until the workspace answers, then ready with its values", async () => {
    const { model, sent, answer, redraws } = harness();
    const loading = model.load();
    expect(model.status).toBe("loading");
    expect(model.settings).toBeNull();
    expect(sent).toEqual([{ url: URL, method: "GET", body: null }]);

    await answer({ ok: true, status: 200, body: { ...ALL_ON, is_new_chat_suggestions_enabled: false } });
    await loading;

    expect(model.status).toBe("ready");
    expect(model.settings).toEqual({ ...ALL_ON, is_new_chat_suggestions_enabled: false });
    expect(redraws()).toBe(1);
  });

  it("is unavailable for a workspace that cannot be reached, one from before the switches, or no answer at all", async () => {
    const refusals: RequestResult[] = [
      { ok: false, status: 502, body: { error: "Couldn't reach it.", code: "UNREACHABLE" } },
      { ok: false, status: 502, body: { error: "Not yet.", code: "UNSUPPORTED" } },
      { ok: false, status: 0, body: { error: "Could not reach the app server." } },
      { ok: true, status: 200, body: { is_next_step_suggestions_enabled: true } },
    ];
    for (const refusal of refusals) {
      const { model, answer } = harness();
      const loading = model.load();
      await answer(refusal);
      await loading;
      expect(model.status).toBe("unavailable");
      expect(model.settings).toBeNull();
    }
  });

  it("shows a flip at once, writes that one switch, and takes the workspace's answer", async () => {
    const { model, sent, answer } = await readyHarness();

    const flipping = model.flip("is_new_chat_suggestions_enabled", false);
    expect(model.settings?.is_new_chat_suggestions_enabled).toBe(false);
    expect(model.isWriting("is_new_chat_suggestions_enabled")).toBe(true);
    expect(model.isWriting("is_next_step_suggestions_enabled")).toBe(false);
    expect(sent.at(-1)).toEqual({ url: URL, method: "PUT", body: { is_new_chat_suggestions_enabled: false } });

    await answer({ ok: true, status: 200, body: { ...ALL_ON, is_new_chat_suggestions_enabled: false } });
    await flipping;

    expect(model.settings).toEqual({ ...ALL_ON, is_new_chat_suggestions_enabled: false });
    expect(model.isWriting("is_new_chat_suggestions_enabled")).toBe(false);
    expect(model.writeError).toBeNull();
  });

  it("puts a switch back and says why when its write fails", async () => {
    const { model, answer } = await readyHarness();

    const flipping = model.flip("is_next_step_suggestions_enabled", false);
    expect(model.settings?.is_next_step_suggestions_enabled).toBe(false);
    await answer({
      ok: false,
      status: 502,
      body: { error: "Couldn't reach this workspace's suggestion settings.", code: "UNREACHABLE", detail: "HTTP 503" },
    });
    await flipping;

    expect(model.settings).toEqual(ALL_ON);
    expect(model.isWriting("is_next_step_suggestions_enabled")).toBe(false);
    expect(model.writeError?.message).toBe("Couldn't reach this workspace's suggestion settings.");
    expect(model.writeError?.details).toContain("HTTP 503");

    // The next flip clears the reason while its own write is out.
    void model.flip("is_next_step_suggestions_enabled", false);
    expect(model.writeError).toBeNull();
  });

  it("puts a switch back when the write is answered without the switches", async () => {
    const { model, answer } = await readyHarness();

    const flipping = model.flip("is_new_chat_suggestions_enabled", false);
    await answer({ ok: true, status: 200, body: {} });
    await flipping;

    expect(model.settings).toEqual(ALL_ON);
    expect(model.writeError?.message).toContain("Couldn't save this workspace's suggestion settings");
  });

  it("drops a second flip of a switch whose write is still out", async () => {
    const { model, sent } = await readyHarness();

    void model.flip("is_new_chat_suggestions_enabled", false);
    void model.flip("is_new_chat_suggestions_enabled", true);

    expect(sent.filter((request) => request.method === "PUT")).toHaveLength(1);
    expect(model.settings?.is_new_chat_suggestions_enabled).toBe(false);
  });

  it("keeps a flip still being written when another switch's answer lands with the older value", async () => {
    const { model, answer } = await readyHarness();

    const first = model.flip("is_next_step_suggestions_enabled", false);
    const second = model.flip("is_new_chat_suggestions_enabled", false);
    // The first write's answer was read before the second write landed in the workspace.
    await answer({ ok: true, status: 200, body: { ...ALL_ON, is_next_step_suggestions_enabled: false } });
    await first;
    expect(model.settings).toEqual({
      ...ALL_ON,
      is_next_step_suggestions_enabled: false,
      is_new_chat_suggestions_enabled: false,
    });

    await answer({
      ok: true,
      status: 200,
      body: { ...ALL_ON, is_next_step_suggestions_enabled: false, is_new_chat_suggestions_enabled: false },
    });
    await second;
    expect(model.settings).toEqual({
      ...ALL_ON,
      is_next_step_suggestions_enabled: false,
      is_new_chat_suggestions_enabled: false,
    });
  });

  it("drops an answer that lands after the group was left", async () => {
    const { model, answer, redraws } = harness();
    const loading = model.load();
    model.stop();

    await answer({ ok: true, status: 200, body: ALL_ON });
    await loading;

    expect(model.status).toBe("loading");
    expect(redraws()).toBe(0);
  });

  it("ignores a flip before the values are known", async () => {
    const { model, sent } = harness();

    await model.flip("is_new_chat_suggestions_enabled", false);

    expect(sent).toEqual([]);
  });
});
