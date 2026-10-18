import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes, createFormDefaults, jsonResponse, settle } from "../../testing";
import type { CreateFormDefaults } from "../../models/create";
import { CreateTemplatePage } from "./CreateTemplatePage";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ROUTE: Record<string, string> = { git_url: "https://github.com/example/template.git", start: "create" };

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
  it("opens the advanced panel on the first cloud account when your own cloud is picked", async () => {
    const render = await pageOnWhereToRun(
      createFormDefaults({
        byok_clouds_enabled: true,
        cloud_accounts: [{ name: "aws_acct", alias: "team aws", backend: "aws", region: "us-west-2" }],
      }),
    );

    pick(render(), "own-cloud");
    const nodes = render();

    const select = computeSelect(nodes) as AnyVnode;
    expect(attrsOf(select).value).toBe("BYOK:aws_acct");
    expect(allText(select)).toContain("team aws (AWS account)");
    expect(allText(nodes)).toContain("Your own cloud");
  });

  it("keeps the advanced panel closed when Imbue Cloud is picked", async () => {
    const render = await pageOnWhereToRun(createFormDefaults());

    pick(render(), "remote");
    const nodes = render();

    expect(computeSelect(nodes)).toBeUndefined();
    expect(allText(nodes)).toContain("Imbue Cloud");
  });
});
