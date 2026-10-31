import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import { ShellState } from "../shell/shell-state";
import type { AnyVnode } from "../../testing";
import {
  accountsMessage,
  allText,
  attrsOf,
  collectVnodes,
  createFormDefaults,
  jsonResponse,
  secondAccountEntry,
  settle,
} from "../../testing";
import type {
  CloudAccountOption,
  CreateFormDefaults,
} from "../../models/create";
import { CreateTemplatePage } from "./CreateTemplatePage";
import { SLOW_START_WARNING } from "./create/ComputeNotices";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";

afterEach(() => {
  clearAppContextForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ROUTE: Record<string, string> = { git_url: "https://github.com/example/template.git", start: "create" };

/** A connection the default test account (user-1) owns. */
const TEAM_AWS_CONNECTION: CloudAccountOption = {
  name: "aws_acct",
  alias: "team aws",
  backend: "aws",
  region: "us-west-2",
  owner_user_id: "user-1",
  is_region_pinned: true,
};

/** The full page, already on "Where should it run?", rendered afresh as vnodes on each call. */
async function pageOnWhereToRun(
  defaults: CreateFormDefaults,
): Promise<() => AnyVnode[]> {
  // The page reads the window's account off the shell for the defaults it fetches.
  const stores = createEmptyStores();
  const shell = new ShellState(stores);
  shell.accountId = defaults.account?.user_id ?? "local";
  registerAppContext({ stores, shell });
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
    key === undefined ? undefined : ROUTE[key]) as typeof m.route.param);
  vi.stubGlobal("fetch", () => Promise.resolve(jsonResponse(defaults)));
  // Opening the advanced panel pins the page's centered column, which is not mounted here.
  vi.stubGlobal("document", { getElementById: () => null });
  const vnode = m(CreateTemplatePage) as m.Vnode;
  const page = CreateTemplatePage(vnode);
  page.oninit?.call(page, vnode);
  await settle();
  return () => collectVnodes(page.view.call(page, vnode));
}

function pick(nodes: AnyVnode[], name: PresetName): void {
  const cards = nodes.find((node) => node.tag === PresetCards) as AnyVnode;
  (attrsOf(cards).onSelect as (preset: PresetName) => void)(name);
}

function computeSelect(nodes: AnyVnode[]): AnyVnode | undefined {
  return nodes.find((node) => attrsOf(node).id === "insp-launch-mode");
}

describe("CreateTemplatePage", () => {
  it("opens the advanced panel on the first connection on offer when your own cloud is picked", async () => {
    const render = await pageOnWhereToRun(createFormDefaults({ cloud_accounts: [TEAM_AWS_CONNECTION] }));

    pick(render(), "own-cloud");
    const nodes = render();

    const select = computeSelect(nodes) as AnyVnode;
    expect(attrsOf(select).value).toBe("CONNECTION:aws_acct");
    expect(allText(select)).toContain("team aws (AWS)");
    expect(allText(nodes)).toContain("Your own cloud");
  });

  it("offers only the device's compute when the window has no account", async () => {
    const render = await pageOnWhereToRun(
      createFormDefaults({
        account: null,
        cloud_accounts: [TEAM_AWS_CONNECTION],
      }),
    );
    pick(render(), "own-cloud");

    const select = computeSelect(render()) as AnyVnode;
    // An account's connection is not on offer on the device: the own-cloud preset falls to Vultr.
    expect(attrsOf(select).value).toBe("VULTR");
    expect(allText(select)).not.toContain("team aws");
  });

  it("keeps the advanced panel closed when Imbue Cloud is picked", async () => {
    const render = await pageOnWhereToRun(createFormDefaults());

    pick(render(), "remote");
    const nodes = render();

    expect(computeSelect(nodes)).toBeUndefined();
    expect(allText(nodes)).toContain("Imbue Cloud");
  });

  it("warns that the workspace will be slow to start on any compute but Imbue Cloud", async () => {
    const remote = await pageOnWhereToRun(createFormDefaults());
    pick(remote(), "remote");
    expect(remote().find((node) => attrsOf(node).id === "insp-slow-start-notice")).toBeUndefined();

    const local = await pageOnWhereToRun(createFormDefaults());
    pick(local(), "local");
    const notice = local().find((node) => attrsOf(node).id === "insp-slow-start-notice");
    expect(notice).toBeDefined();
    expect(allText(notice)).toBe(SLOW_START_WARNING);
  });
});

describe("CreateTemplatePage's sign-in for Imbue Cloud", () => {
  const BOB = secondAccountEntry();
  const BOB_OPTION = {
    user_id: BOB.user_id,
    email: BOB.email,
    state: "SIGNED_IN" as const,
    is_connection_owner_eligible: true,
  };

  it("takes up the account signed in here when a device window creates on Imbue Cloud, then posts the create under it", async () => {
    // The window is the device's, with Bob signed in on the install: the
    // stepper's Create must not send the user to the accounts page they are
    // already listed on, but take Bob up into the window and create.
    const stores = createEmptyStores();
    const shell = new ShellState(stores);
    shell.accountId = "local";
    stores.accounts.applyAccountsMessage(accountsMessage([BOB]));
    registerAppContext({ stores, shell });
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.spyOn(m.route, "param").mockImplementation(((key?: string) =>
      key === undefined ? undefined : ROUTE[key]) as typeof m.route.param);
    vi.spyOn(m.route, "get").mockReturnValue("/account/local/create/template");
    const routeSet = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    vi.stubGlobal("document", { getElementById: () => null });
    vi.stubGlobal("window", { history: { replaceState: () => undefined } });
    const requests: [string, RequestInit | undefined][] = [];
    vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
      requests.push([url, init]);
      if (url === "/api/v1/workspaces")
        return Promise.resolve(jsonResponse({ operation_id: "op-1" }, 202));
      const account = url.endsWith(`account=${BOB.user_id}`)
        ? BOB_OPTION
        : null;
      return Promise.resolve(jsonResponse(createFormDefaults({ account })));
    });
    const vnode = m(CreateTemplatePage) as m.Vnode;
    const page = CreateTemplatePage(vnode);
    page.oninit?.call(page, vnode);
    await settle();
    const render = (): AnyVnode[] => collectVnodes(page.view.call(page, vnode));

    pick(render(), "remote");
    const trust = render().find(
      (node) => attrsOf(node).type === "checkbox",
    ) as AnyVnode;
    (attrsOf(trust).onchange as (event: unknown) => void)({
      target: { checked: true },
    });
    const form = render().find((node) => node.tag === "form") as AnyVnode;
    (attrsOf(form).onsubmit as (event: unknown) => void)({
      preventDefault: () => undefined,
    });
    await settle();

    expect(shell.accountId).toBe(BOB.user_id);
    expect(requests.map(([url]) => url)).toEqual([
      "/ui/api/create/form-defaults?account=local",
      `/ui/api/create/form-defaults?account=${BOB.user_id}`,
      "/api/v1/workspaces",
    ]);
    const createInit = requests[2]?.[1] as RequestInit;
    expect(JSON.parse(String(createInit.body))).toMatchObject({
      account_id: BOB.user_id,
      launch_mode: "IMBUE_CLOUD",
    });
    expect(routeSet).toHaveBeenCalledWith("/creating/op-1");
  });
});
