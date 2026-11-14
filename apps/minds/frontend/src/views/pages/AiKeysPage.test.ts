import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAppContextForTests, registerAppContext } from "../../app-context";
import { createEmptyStores } from "../../models/boot";
import type { AiKeysContext } from "../../models/aiKeys";
import {
  allText,
  attrsOf,
  collectVnodes,
  jsonResponse,
  renderDeep,
  withMindsNative,
  workspacesMessage,
} from "../../testing";
import { Button } from "../components/Button";
import { ShellState } from "../shell/shell-state";
import { AiKeysPage } from "./AiKeysPage";

/** Press the button reading `label` in a rendered tree. */
function press(tree: unknown, label: string): void {
  const button = collectVnodes(tree).find(
    (node) => node.tag === "button" && allText(node) === label,
  );
  if (button === undefined) throw new Error(`no "${label}" button`);
  (attrsOf(button).onclick as () => void)();
}

/** The page opened for `workspace`, once the server's context for it has
 * loaded: its rendered tree, and its view with the components left unrendered
 * (where a Button's variant can still be read). */
async function mountWithContext(
  workspace: string,
  context: AiKeysContext,
): Promise<{ render: () => unknown; view: () => m.Children }> {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m.route, "param").mockReturnValue(workspace);
  vi.stubGlobal("fetch", () => Promise.resolve(jsonResponse(context)));
  const page = AiKeysPage();
  (page.oninit as () => void).call(page);
  const view = (): m.Children => (page.view as () => m.Children).call(page);
  const render = (): unknown => renderDeep(view());
  await vi.waitFor(() => expect(allText(render())).not.toContain("Loading"));
  return { render, view };
}

describe("AiKeysPage on a workspace with no Imbue account", () => {
  afterEach(() => {
    clearAppContextForTests();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("opens the workspace's Account settings, where the account is linked", async () => {
    const shell = new ShellState(createEmptyStores());
    shell.stores.workspaces.applyWorkspacesMessage(workspacesMessage());
    registerAppContext({ stores: shell.stores, shell });
    const setRoute = vi
      .spyOn(m.route, "set")
      .mockImplementation(() => undefined);
    const { render } = await mountWithContext("host-bb22", {
      workspace_id: "host-bb22",
      workspace_display_name: "",
      account_email: "",
      error_message:
        "This workspace has no associated Imbue account. Associate an account on the workspace's settings page, then come back here.",
    });
    expect(allText(render())).not.toContain("Report a bug");
    press(render(), "Open workspace settings");
    expect(setRoute).toHaveBeenCalledWith("/workspace/agent-aa11/options", {
      tab: "settings",
      group: "account",
    });
  });

  it("offers no settings to open when it was opened without a workspace", async () => {
    const { render } = await mountWithContext("", {
      workspace_id: "",
      workspace_display_name: "",
      account_email: "",
      error_message:
        "This page needs to be opened from a workspace: use the Sign in with Imbue option in the workspace's Claude sign-in dialog.",
    });
    expect(allText(render())).toContain("opened from a workspace");
    expect(allText(render())).not.toContain("Open workspace settings");
  });
});

describe("AiKeysPage's Create key beside a failed mint", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const linkedContext: AiKeysContext = {
    workspace_id: "host-bb22",
    workspace_display_name: "my-workspace",
    account_email: "owner@example.com",
    error_message: "",
  };

  /** The Create key button's variant, read from the unrendered view. */
  function createKeyVariant(view: () => m.Children): unknown {
    const button = collectVnodes(view()).find(
      (node) => node.tag === Button && allText(node).startsWith("Create"),
    );
    if (button === undefined) throw new Error("no Create key button");
    return attrsOf(button).variant;
  }

  it("steps down while the failure leads with Restart Imbue Studio, and stays primary for one with no button", async () => {
    await withMindsNative({ retry: vi.fn() }, async () => {
      const { render, view } = await mountWithContext(
        "host-bb22",
        linkedContext,
      );
      expect(createKeyVariant(view)).toBe("primary");

      vi.stubGlobal("fetch", () =>
        Promise.resolve(jsonResponse({ error: "Not authenticated" }, 403)),
      );
      press(render(), "Create key & copy credentials");
      await vi.waitFor(() =>
        expect(allText(render())).toContain("Restart Imbue Studio"),
      );
      expect(createKeyVariant(view)).toBe("secondary");

      vi.stubGlobal("fetch", () =>
        Promise.resolve(
          jsonResponse(
            {
              error:
                "Couldn't create a key for this workspace. Try again. If this persists, report a bug.",
              detail: "LiteLLM answered 500",
            },
            502,
          ),
        ),
      );
      press(render(), "Create key & copy credentials");
      await vi.waitFor(() =>
        expect(allText(render())).toContain("Couldn't create a key"),
      );
      expect(allText(render())).not.toContain("Restart Imbue Studio");
      expect(createKeyVariant(view)).toBe("primary");
    });
  });
});
