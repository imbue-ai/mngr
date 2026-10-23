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
import type { CreateFormDefaults } from "../../models/create";
import { DialogCloseButton } from "../components/Modal";
import type { CreatePageAttrs } from "./CreatePage";
import {
  CreatePage,
  ONBOARDING_QUESTIONS_LABEL,
  ONBOARDING_QUESTIONS_SENTENCE,
  createFormModal,
  signInOutcome,
} from "./CreatePage";
import { ENABLE_SHARING_LABEL, SHARING_SENTENCE } from "./workspace/ShareTab";
import { SLOW_START_WARNING } from "./create/ComputeNotices";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";

/** Every page `loadedPage` built, torn down as the router would after each
 * test: a host-name check the page debounced would otherwise fire after the
 * real `m.redraw` is restored, which throws here, where there is no
 * `requestAnimationFrame` for it to schedule on. */
const loadedPages: m.Component<CreatePageAttrs>[] = [];

afterEach(() => {
  for (const page of loadedPages.splice(0)) (page.onremove as () => void).call(page);
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function byId(root: unknown, id: string): AnyVnode | undefined {
  return collectVnodes(root).find((node) => attrsOf(node).id === id);
}

/** The page (routed unless `attrs` say otherwise) with its defaults loaded. */
async function loadedPage(
  attrs: CreatePageAttrs = {},
  defaults: CreateFormDefaults = createFormDefaults(),
): Promise<{
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
  vi.stubGlobal("fetch", () => Promise.resolve(jsonResponse(defaults)));
  const vnode = m(CreatePage, attrs) as m.Vnode<CreatePageAttrs>;
  const page = CreatePage(vnode);
  page.oninit?.call(page, vnode);
  loadedPages.push(page);
  await settle();
  return { page, vnode };
}

describe("CreatePage", () => {
  it("opens on Imbue Cloud, and picking your own cloud opens the provider settings it needs", async () => {
    const { page, vnode } = await loadedPage();

    const simple = page.view.call(page, vnode);
    expect(classTokensOf(byId(simple, "advanced-view") as AnyVnode)).toContain("hidden");
    const cards = collectVnodes(simple).find((node) => node.tag === PresetCards) as AnyVnode;
    expect(attrsOf(cards).selectedPreset).toBe("remote");

    (attrsOf(cards).onSelect as (name: PresetName) => void)("own-cloud");
    const advanced = page.view.call(page, vnode);

    expect(classTokensOf(byId(advanced, "advanced-view") as AnyVnode)).not.toContain("hidden");
    expect(attrsOf(byId(advanced, "launch_mode") as AnyVnode).value).toBe("VULTR");
  });

  it("opens embedded on the caller's preset, with your own cloud's provider settings showing", async () => {
    const opened = async (initialPreset: PresetName, defaults?: CreateFormDefaults): Promise<AnyVnode[]> => {
      const { page, vnode } = await loadedPage({ mode: "embedded", initialPreset }, defaults);
      return collectVnodes(page.view.call(page, vnode));
    };
    const selectedPreset = (nodes: AnyVnode[]): unknown =>
      attrsOf(nodes.find((node) => node.tag === PresetCards) as AnyVnode).selectedPreset;

    const local = await opened("local");
    expect(selectedPreset(local)).toBe("local");
    expect(classTokensOf(byId(local, "advanced-view") as AnyVnode)).toContain("hidden");
    expect(attrsOf(byId(local, "launch_mode") as AnyVnode).value).toBe(createFormDefaults().local_launch_mode);

    const ownCloud = await opened("own-cloud");
    expect(selectedPreset(ownCloud)).toBe("own-cloud");
    expect(classTokensOf(byId(ownCloud, "advanced-view") as AnyVnode)).not.toContain("hidden");
    expect(attrsOf(byId(ownCloud, "launch_mode") as AnyVnode).value).toBe("VULTR");

    const withAccount = await opened(
      "own-cloud",
      createFormDefaults({
        cloud_accounts: [
          {
            name: "aws_acct",
            alias: "team aws",
            backend: "aws",
            region: "us-west-2",
            owner_user_id: "user-1",
            is_region_pinned: true,
          },
        ],
      }),
    );
    expect(attrsOf(byId(withAccount, "launch_mode") as AnyVnode).value).toBe("CONNECTION:aws_acct");
  });

  it("offers the share panel's own switch in the advanced view, on by default under an account", async () => {
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
    expect(attrsOf(control)["aria-checked"]).toBe("true");
    expect(attrsOf(control)["aria-label"]).toBe(ENABLE_SHARING_LABEL);
    expect(classTokensOf(control)).toContain("perm-switch");
    expect(allText(byId(row, "web-access-word"))).toBe("Yes");
    expect(byId(row, "web-access-account-error")).toBeUndefined();

    (attrsOf(control).onclick as () => void)();
    const thrown = byId(page.view.call(page, vnode), "web-access-row");

    expect(attrsOf(byId(thrown, "enable_web_access") as AnyVnode)["aria-checked"]).toBe("false");
    expect(allText(byId(thrown, "web-access-word"))).toBe("No");
  });

  it("offers the onboarding questions in the advanced view, off from the form and on for the start flow's form", async () => {
    const { page, vnode } = await loadedPage();
    (attrsOf(byId(page.view.call(page, vnode), "toggle-advanced") as AnyVnode).onclick as () => void)();
    const row = byId(page.view.call(page, vnode), "onboarding-questions-row");

    expect(allText(row)).toContain(ONBOARDING_QUESTIONS_LABEL);
    expect(allText(row)).toContain(ONBOARDING_QUESTIONS_SENTENCE);
    const control = byId(row, "is_onboarding_driven_by_agent") as AnyVnode;
    expect(attrsOf(control).role).toBe("switch");
    expect(attrsOf(control)["aria-checked"]).toBe("false");
    expect(allText(byId(row, "onboarding-questions-word"))).toBe("No");

    (attrsOf(control).onclick as () => void)();
    const thrown = byId(page.view.call(page, vnode), "onboarding-questions-row");
    expect(attrsOf(byId(thrown, "is_onboarding_driven_by_agent") as AnyVnode)["aria-checked"]).toBe("true");

    // The start flow's form answers open with it on: theirs is the first workspace.
    const { page: flowPage, vnode: flowVnode } = await loadedPage({
      mode: "embedded",
      initialPreset: "local",
      isOnboardingDrivenByAgent: true,
    });
    const flowRow = byId(flowPage.view.call(flowPage, flowVnode), "onboarding-questions-row");
    expect(attrsOf(byId(flowRow, "is_onboarding_driven_by_agent") as AnyVnode)["aria-checked"]).toBe("true");
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

  it("warns that the workspace will be slow to start on any compute but Imbue Cloud", async () => {
    const { page, vnode } = await loadedPage();

    const remote = page.view.call(page, vnode);
    expect(byId(remote, "slow-start-notice")).toBeUndefined();

    const cards = collectVnodes(remote).find((node) => node.tag === PresetCards) as AnyVnode;
    (attrsOf(cards).onSelect as (name: PresetName) => void)("local");
    const local = page.view.call(page, vnode);

    const notice = byId(local, "slow-start-notice");
    expect(notice).toBeDefined();
    expect(allText(notice)).toBe(SLOW_START_WARNING);
    // Shown beside the Create button, so the simple view carries it too.
    expect(classTokensOf(byId(local, "advanced-view") as AnyVnode)).toContain("hidden");
  });
});
