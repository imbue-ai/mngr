import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import type { AppContext } from "../../app-context";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  createFormDefaults,
  jsonResponse,
  settle,
} from "../../testing";
import { DialogCloseButton } from "../components/Modal";
import type { CreatePageAttrs } from "./CreatePage";
import { CreatePage, createFormModal, signInOutcome } from "./CreatePage";
import { ENABLE_SHARING_LABEL, SHARING_SENTENCE } from "./workspace/ShareTab";

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function byId(root: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(root).find((node) => attrsOf(node).id === id);
}

/** The routed page with its defaults loaded. */
async function loadedPage(): Promise<{
  page: m.Component<CreatePageAttrs>;
  vnode: m.Vnode<CreatePageAttrs>;
}> {
  // The page reads the accounts store for the sign-in gate and the providers
  // store for the add-connection owner list; neither matters to these tests.
  registerAppContext({
    stores: {
      accounts: { hasAccounts: false },
      providers: { connectionOwnerOptions: () => [] },
    },
  } as unknown as AppContext);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m.route, "param").mockImplementation(
    (() => undefined) as typeof m.route.param,
  );
  vi.stubGlobal("fetch", () =>
    Promise.resolve(jsonResponse(createFormDefaults())),
  );
  const vnode = m(CreatePage, {}) as m.Vnode<CreatePageAttrs>;
  const page = CreatePage(vnode);
  page.oninit?.call(page, vnode);
  await settle();
  return { page, vnode };
}

describe("CreatePage", () => {
  it("offers the share panel's own switch in the advanced view, in the panel's own words", async () => {
    const { page, vnode } = await loadedPage();

    const simple = page.view.call(page, vnode);
    (attrsOf(byId(simple, "toggle-advanced") as AnyVnode).onclick as () => void)();
    const advanced = page.view.call(page, vnode);

    expect(classTokensOf(byId(advanced, "advanced-view") as AnyVnode)).not.toContain("hidden");
    const row = byId(advanced, "web-access-row");
    expect(row).toBeDefined();
    expect(allText(row)).toContain(ENABLE_SHARING_LABEL);
    expect(allText(row)).toContain(SHARING_SENTENCE);
    const control = byId(row, "enable_web_access") as AnyVnode;
    expect(attrsOf(control).role).toBe("switch");
    expect(attrsOf(control)["aria-checked"]).toBe("false");
    expect(attrsOf(control)["aria-label"]).toBe(ENABLE_SHARING_LABEL);
    expect(classTokensOf(control)).toContain("perm-switch");
    expect(allText(byId(row, "web-access-word"))).toBe("No");
    expect(byId(row, "web-access-account-error")).toBeUndefined();

    (attrsOf(control).onclick as () => void)();
    const thrown = byId(page.view.call(page, vnode), "web-access-row");

    expect(attrsOf(byId(thrown, "enable_web_access") as AnyVnode)["aria-checked"]).toBe("true");
    expect(allText(byId(thrown, "web-access-word"))).toBe("Yes");
  });
});

describe("createFormModal", () => {
  it("carries a close control wired to the host, so the backdrop is not the only way out", () => {
    let closes = 0;
    const modal = createFormModal({
      key: "the-modal",
      id: "the-modal",
      onClose: () => {
        closes += 1;
      },
    });
    const closeButton = collectVnodes(modal).find((vnode) => vnode.tag === DialogCloseButton);
    expect(closeButton).toBeDefined();
    if (closeButton === undefined) return;
    (attrsOf(closeButton).onClose as () => void)();
    expect(closes).toBe(1);
  });
});

describe("signInOutcome", () => {
  it("counts the sign-in as landed on the flow's verdict or on an account reaching the channel first", () => {
    expect(signInOutcome("done", false)).toBe("signed-in");
    expect(signInOutcome("waiting", true)).toBe("signed-in");
  });

  it("drops the create once the modal is closed with no account", () => {
    expect(signInOutcome("idle", false)).toBe("abandoned");
  });

  it("keeps waiting while the modal is up, including on an error it offers a retry for", () => {
    for (const state of ["starting", "waiting", "finishing", "error"] as const) {
      expect(signInOutcome(state, false)).toBe("pending");
    }
  });
});
