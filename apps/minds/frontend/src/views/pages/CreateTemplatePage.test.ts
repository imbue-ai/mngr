import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes, createFormDefaults, jsonResponse, settle } from "../../testing";
import type { CloudAccountOption, CreateFormDefaults } from "../../models/create";
import { CreateTemplatePage } from "./CreateTemplatePage";
import { SLOW_START_WARNING } from "./create/ComputeNotices";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";

afterEach(() => {
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
async function pageOnWhereToRun(defaults: CreateFormDefaults): Promise<() => AnyVnode[]> {
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

  it("drops an account's connection from the compute select when the account changes", async () => {
    const defaults = createFormDefaults({ cloud_accounts: [TEAM_AWS_CONNECTION] });
    const render = await pageOnWhereToRun(defaults);
    pick(render(), "own-cloud");
    expect(attrsOf(computeSelect(render()) as AnyVnode).value).toBe("CONNECTION:aws_acct");

    const accountSelect = render().find((node) => attrsOf(node).id === "account_id") as AnyVnode;
    (attrsOf(accountSelect).onchange as (event: Event) => void)({ target: { value: "" } } as unknown as Event);

    const select = computeSelect(render()) as AnyVnode;
    expect(attrsOf(select).value).toBe(defaults.local_launch_mode);
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
