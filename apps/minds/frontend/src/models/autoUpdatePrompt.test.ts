import { describe, expect, it } from "vitest";
import m from "mithril";
import { attrsOf, byId, jsonResponse, renderRoot, settle } from "../testing";
import { AutoUpdatePromptModal } from "../views/components/AutoUpdatePromptModal";
import { AutoUpdatePromptModel } from "./autoUpdatePrompt";
import type { AutoUpdatePromptDecision } from "./autoUpdatePrompt";

interface Backend {
  model: AutoUpdatePromptModel;
  /** Every request, as `<method> <url> <body>`. */
  requests: string[];
}

/** A model over a backend whose settings payload records `decision`, on a
 * launch that is (or is not) the first into a new app version. */
function backend(
  decision: AutoUpdatePromptDecision | null,
  isFirstLaunchAfterAppUpdate: boolean,
): Backend {
  const requests: string[] = [];
  const fetchImpl = ((url: string, init?: RequestInit) => {
    requests.push(
      `${init?.method ?? "GET"} ${url}${init?.body ? ` ${String(init.body)}` : ""}`,
    );
    if (url === "/ui/api/settings")
      return Promise.resolve(
        jsonResponse({ auto_update_prompt_decision: decision }),
      );
    return Promise.resolve(jsonResponse({}));
  }) as typeof fetch;
  const model = new AutoUpdatePromptModel(
    fetchImpl,
    () => Promise.resolve(isFirstLaunchAfterAppUpdate),
    () => undefined,
  );
  return { model, requests };
}

describe("the auto-update prompt's triggers", () => {
  it("comes up on the first launch into an app update, with nothing behind", async () => {
    const { model } = backend(null, true);
    await model.load();
    expect(model.triggerFor(false, false)).toBe("app-updated");
  });

  it("comes up on the first workspace read as behind", async () => {
    const { model } = backend(null, false);
    await model.load();
    expect(model.triggerFor(false, false)).toBeNull();
    expect(model.triggerFor(false, true)).toBe("out-of-date");
    // Kept up while the reader is on it, even if a sweep changes the reading.
    expect(model.triggerFor(false, false)).toBe("out-of-date");
  });

  it("stays down once an answer is recorded, or auto-update is already on", async () => {
    const answered = backend("declined", true);
    await answered.model.load();
    expect(answered.model.triggerFor(false, true)).toBeNull();

    const enabled = backend(null, true);
    await enabled.model.load();
    expect(enabled.model.triggerFor(true, true)).toBeNull();
  });

  it("stays down until the recorded answer has been read", () => {
    const { model } = backend(null, true);
    expect(model.triggerFor(false, true)).toBeNull();
  });
});

describe("answering the auto-update prompt", () => {
  it("records Not now as a decline, after which it never returns", async () => {
    const { model, requests } = backend(null, true);
    await model.load();
    const modal = renderRoot(AutoUpdatePromptModal, {
      model,
      trigger: "app-updated" as const,
      updateWindow: "2:00 AM-5:00 AM",
    }) as unknown as m.Children;

    const decline = byId(modal, "auto-update-prompt-decline");
    if (decline === undefined) throw new Error("no Not now to press");
    (attrsOf(decline).onclick as () => void)();
    await settle();

    expect(requests).toContain(
      'POST /ui/api/settings/auto-update-prompt {"decision":"declined"}',
    );
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("records turning it on as acceptance", async () => {
    const { model, requests } = backend(null, false);
    await model.load();
    await model.choose("accepted");
    expect(requests).toContain(
      'POST /ui/api/settings/auto-update-prompt {"decision":"accepted"}',
    );
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("keeps the prompt up with the reason when the answer could not be saved", async () => {
    const requests: string[] = [];
    const model = new AutoUpdatePromptModel(
      ((url: string) => {
        requests.push(url);
        return Promise.resolve(
          url === "/ui/api/settings"
            ? jsonResponse({ auto_update_prompt_decision: null })
            : jsonResponse({ error: "The settings file is read-only." }, 500),
        );
      }) as typeof fetch,
      () => Promise.resolve(true),
      () => undefined,
    );
    await model.load();
    await model.choose("declined");
    expect(model.error).toBe("The settings file is read-only.");
    expect(model.triggerFor(false, false)).toBe("app-updated");
  });

  it("puts a prompt closed without an answer away for the session only, recording nothing", async () => {
    const { model, requests } = backend(null, true);
    await model.load();
    model.putAway();
    expect(model.triggerFor(false, true)).toBeNull();
    expect(requests).toEqual(["GET /ui/api/settings"]);
  });
});
