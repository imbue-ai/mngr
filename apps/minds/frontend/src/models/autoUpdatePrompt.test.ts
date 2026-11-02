import { afterEach, beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { AppContext } from "../app-context";
import { clearAppContextForTests, registerAppContext } from "../app-context";
import {
  attrsOf,
  byId,
  collectVnodes,
  jsonResponse,
  renderRoot,
  settingsOverview,
  settle,
} from "../testing";
import { AutoUpdatePromptModal } from "../views/components/AutoUpdatePromptModal";
import { DialogCloseButton } from "../views/components/Modal";
import { AutoUpdatePromptModel } from "./autoUpdatePrompt";
import type { AutoUpdatePromptDecision } from "./autoUpdatePrompt";

// The modal's answer buttons read the window's account off the shell.
beforeEach(() => {
  registerAppContext({ shell: { accountId: "user-7" } } as unknown as AppContext);
});

afterEach(() => {
  clearAppContextForTests();
});

interface Backend {
  model: AutoUpdatePromptModel;
  /** Every request, as `<method> <url> <body>`. */
  requests: string[];
}

/** A model over a backend whose settings payload records `decision`, on a
 * launch that is (or is not) the first into a new app version; every write
 * answers `writeAnswer`. */
function backend(
  decision: AutoUpdatePromptDecision | null,
  isFirstLaunchAfterAppUpdate: boolean,
  writeAnswer: () => Response = () => jsonResponse({}),
): Backend {
  const requests: string[] = [];
  const fetchImpl = ((url: string, init?: RequestInit) => {
    requests.push(
      `${init?.method ?? "GET"} ${url}${init?.body ? ` ${String(init.body)}` : ""}`,
    );
    if (url.split("?")[0] === "/ui/api/settings")
      return Promise.resolve(
        jsonResponse(
          settingsOverview({
            auto_update_prompt_decision: decision,
            update_window_start_hour: 2,
            update_window_end_hour: 5,
          }),
        ),
      );
    return Promise.resolve(writeAnswer());
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

  it("stays down until the app has said whether auto-update is already on", async () => {
    const { model } = backend(null, true);
    await model.load();
    expect(model.triggerFor(null, false)).toBeNull();
    expect(model.triggerFor(false, false)).toBe("app-updated");
  });

  it("stays down until the recorded answer has been read", () => {
    const { model } = backend(null, true);
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("reads the answer again on the next load after a read that failed", async () => {
    let isBackendUp = false;
    const fetchImpl = (() =>
      Promise.resolve(
        isBackendUp ? jsonResponse(settingsOverview()) : jsonResponse({}, 503),
      )) as typeof fetch;
    const model = new AutoUpdatePromptModel(
      fetchImpl,
      () => Promise.resolve(true),
      () => undefined,
    );
    await model.load();
    expect(model.triggerFor(false, false)).toBeNull();

    isBackendUp = true;
    await model.load();
    expect(model.triggerFor(false, false)).toBe("app-updated");
  });
});

describe("answering the auto-update prompt", () => {
  it("records Not now as a decline, after which it never returns", async () => {
    const { model, requests } = backend(null, true);
    await model.load();
    const modal = renderRoot(AutoUpdatePromptModal, {
      model,
    }) as unknown as m.Children;

    const decline = byId(modal, "auto-update-prompt-decline");
    if (decline === undefined) throw new Error("no Not now to press");
    (attrsOf(decline).onclick as () => void)();
    await settle();

    expect(requests).toContain(
      'PUT /ui/api/settings/user-7 {"auto_update_prompt_decision":"declined"}',
    );
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("records turning it on as acceptance, with the window picked in the prompt", async () => {
    const { model, requests } = backend(null, false);
    await model.load();
    const render = () =>
      renderRoot(AutoUpdatePromptModal, {
        model,
      }) as unknown as m.Children;

    const start = byId(render(), "update-window-start-prompt");
    if (start === undefined) throw new Error("no window start to pick");
    // Starts from the saved window.
    expect(attrsOf(start).value).toBe("2");
    (attrsOf(start).onchange as (event: Event) => void)({
      target: { value: "23" },
    } as unknown as Event);
    const accept = byId(render(), "auto-update-prompt-accept");
    if (accept === undefined) throw new Error("no Turn on to press");
    (attrsOf(accept).onclick as () => void)();
    await settle();

    expect(requests).toContain(
      'PUT /ui/api/settings/user-7 ' +
        '{"auto_update_prompt_decision":"accepted","auto_update_workspaces":true,' +
        '"update_window_start_hour":23,"update_window_end_hour":5}',
    );
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("keeps the prompt up with the reason when the answer could not be saved", async () => {
    const { model } = backend(null, true, () =>
      jsonResponse({ error: "The settings file is read-only." }, 500),
    );
    await model.load();
    await model.choose("declined", "user-7");
    expect(model.error).toBe("The settings file is read-only.");
    expect(model.triggerFor(false, false)).toBe("app-updated");
  });

  it("reads the recorded answer from the window's account's record", async () => {
    const { model, requests } = backend("declined", true);

    await model.load("user-7");

    expect(requests).toEqual(["GET /ui/api/settings?account=user-7"]);
    expect(model.triggerFor(false, true)).toBeNull();
  });

  it("puts a prompt closed without an answer away for the session only, recording nothing", async () => {
    const { model, requests } = backend(null, true);
    await model.load();
    const modal = renderRoot(AutoUpdatePromptModal, {
      model,
    }) as unknown as m.Children;

    const close = collectVnodes(modal).find(
      (vnode) => vnode.tag === DialogCloseButton,
    );
    if (close === undefined) throw new Error("no close button to press");
    (attrsOf(close).onClose as () => void)();

    expect(model.triggerFor(false, true)).toBeNull();
    expect(requests).toEqual(["GET /ui/api/settings?account=local"]);
  });
});
