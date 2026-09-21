// The unmet-prerequisite notice: what it says, and which control does what.

import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LocalBackendPrerequisite } from "../../../models/create";
import { allText, attrsOf, collectVnodes, renderRoot, renderedText, settle } from "../../../testing";
import { PrerequisiteNotice } from "./PrerequisiteNotice";

const DOCKER_MISSING: LocalBackendPrerequisite = {
  key: "DOCKER",
  is_available: false,
  summary: "Docker is not installed.",
  install_command: 'curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$USER"',
  docs_url: "https://docs.docker.com/engine/install/",
};

/** Every vnode carrying an onclick, keyed by the text it shows. */
function controlsByText(root: unknown): Map<string, () => void> {
  const byText = new Map<string, () => void>();
  for (const vnode of collectVnodes(root)) {
    const onclick = attrsOf(vnode).onclick;
    if (typeof onclick === "function") byText.set(allText(vnode.children).trim(), onclick as () => void);
  }
  return byText;
}

describe("the prerequisite notice", () => {
  // The copied label redraws the page it lives on; there is none here.
  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    // restoreAllMocks does not undo vi.stubGlobal; only this does.
    vi.unstubAllGlobals();
  });

  it("shows the summary, the command with a Copy control, the guide, and Check again", () => {
    const root = renderRoot(PrerequisiteNotice, { prerequisite: DOCKER_MISSING, onCheckAgain: () => {} });

    const text = renderedText(root);
    expect(text).toContain("Docker is not installed.");
    expect(text).toContain(DOCKER_MISSING.install_command);
    expect(text).toContain("Installation guide");
    expect([...controlsByText(root).keys()]).toEqual(["Copy", "Check again"]);
    const guide = collectVnodes(root).find((vnode) => attrsOf(vnode).href === DOCKER_MISSING.docs_url);
    expect(guide).toBeDefined();
  });

  it("leaves out the command block when only the docs can help", () => {
    // macOS, or a Linux without apt: there is nothing to paste, so a Copy
    // control would copy an empty string.
    const root = renderRoot(PrerequisiteNotice, {
      prerequisite: { ...DOCKER_MISSING, install_command: "" },
      onCheckAgain: () => {},
    });

    expect([...controlsByText(root).keys()]).toEqual(["Check again"]);
    expect(renderedText(root)).toContain("Docker is not installed.");
  });

  it("forwards Check again to the caller", () => {
    let checkCount = 0;
    const root = renderRoot(PrerequisiteNotice, { prerequisite: DOCKER_MISSING, onCheckAgain: () => checkCount++ });

    controlsByText(root).get("Check again")!();

    expect(checkCount).toBe(1);
  });

  it("reads Copied only after the clipboard accepted the command", async () => {
    // The label follows the write: a rejected write (an insecure context, a
    // denied permission) must not claim the command is on the clipboard.
    const written: string[] = [];
    let isAccepting = false;
    const clipboard = {
      writeText: (text: string) => {
        written.push(text);
        return isAccepting ? Promise.resolve() : Promise.reject(new Error("denied"));
      },
    };
    vi.stubGlobal("navigator", { clipboard });
    const notice = PrerequisiteNotice();
    const attrs = { prerequisite: DOCKER_MISSING, onCheckAgain: () => {} };
    const render = () => notice.view!({ attrs } as never);

    controlsByText(render()).get("Copy")!();
    await settle();
    expect(written).toEqual([DOCKER_MISSING.install_command]);
    expect(controlsByText(render()).has("Copy")).toBe(true);

    isAccepting = true;
    controlsByText(render()).get("Copy")!();
    await settle();
    expect(controlsByText(render()).has("Copied")).toBe(true);
  });

  it("keeps reading Copy where there is no clipboard at all", async () => {
    vi.stubGlobal("navigator", {});
    const notice = PrerequisiteNotice();
    const attrs = { prerequisite: DOCKER_MISSING, onCheckAgain: () => {} };
    const render = () => notice.view!({ attrs } as never);

    controlsByText(render()).get("Copy")!();
    await settle();

    expect(controlsByText(render()).has("Copy")).toBe(true);
  });
});
