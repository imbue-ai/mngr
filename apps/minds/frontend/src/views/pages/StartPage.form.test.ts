// The where-to-run links on the live page: each opens the create form as a modal on its own preset, and closing
// the form asks again. A file of its own because it drives the live page, whose sign-in module is replaced here.
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import type { FormChoiceId } from "../../models/startFlow";
import type { AnyVnode } from "../../testing";
import { attrsOf, collectVnodes, createFormDefaults, jsonResponse } from "../../testing";
import { ShellState } from "../shell/shell-state";
import { CreatePage } from "./CreatePage";
import { pressLast, renderStartPage } from "./StartPage.testing";
import type { PresetName } from "./create/form-model";

vi.mock("../../models/webLogin", () => ({ webLogin: { start: vi.fn(async () => {}), email: "" } }));

function openCreateFormModal(tree: unknown): AnyVnode | undefined {
  return collectVnodes(tree).find((node) => attrsOf(node).id === "start-create-form");
}

describe("the where-to-run links on the live page", () => {
  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const shell = new ShellState(createEmptyStores());
    registerAppContext({ stores: shell.stores, shell });
    vi.stubGlobal("fetch", async () => jsonResponse(createFormDefaults()));
  });

  afterEach(() => {
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each<[FormChoiceId, PresetName]>([
    ["local", "local"],
    ["own-cloud", "own-cloud"],
  ])("%s opens the create form on the %s preset, and closing it asks again", (answer, preset) => {
    const { view } = renderStartPage();
    pressLast(view(), "data-answer", "reporting-yes");
    expect(openCreateFormModal(view())).toBeUndefined();

    pressLast(view(), "data-answer", answer);
    const modal = openCreateFormModal(view()) as AnyVnode;
    const form = collectVnodes(modal).find((node) => node.tag === CreatePage) as AnyVnode;
    expect(attrsOf(form).initialPreset).toBe(preset);
    expect(attrsOf(form).isOnboardingDrivenByAgent).toBe(true);

    (attrsOf(modal).onClose as () => void)();
    const nodes = collectVnodes(view());
    expect(openCreateFormModal(nodes)).toBeUndefined();
    const offered = nodes.map((node) => attrsOf(node)["data-answer"]).filter((id) => id !== undefined);
    expect(offered.slice(-3)).toEqual(["cloud", "local", "own-cloud"]);
  });
});
