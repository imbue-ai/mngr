// @vitest-environment jsdom
//
// Under the real router: what is under test is how Mithril parses the
// recovery deep-link's ?return_to once m.route.set has put it in the URL.
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { recoveryRoute } from "../../models/create";
import { settle } from "../../testing";

let root: HTMLElement | null = null;

afterEach(async () => {
  await settle();
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
});

/** Route to the recovery deep-link and read back the params the page reads. */
async function paramsAfterRouting(returnTo: string): Promise<{ returnTo: string; intent: string }> {
  const seen: { returnTo: string; intent: string }[] = [];
  const recoveryProbe: m.Component = {
    oninit: () => {
      seen.push({ returnTo: m.route.param("return_to") ?? "", intent: m.route.param("intent") ?? "" });
    },
    view: () => m("div"),
  };
  window.history.replaceState(null, "", "/");
  root = document.createElement("div");
  document.body.appendChild(root);
  m.route.prefix = "";
  m.route(root, "/", { "/": { view: () => m("div") }, "/agents/:agentId/recovery": recoveryProbe });
  await settle();
  m.route.set(recoveryRoute("agent-aa11", returnTo, "start"));
  return vi.waitFor(() => {
    const [params] = seen;
    if (params === undefined) throw new Error("the recovery route never rendered");
    return params;
  });
}

describe("the recovery deep-link's return path", () => {
  it("survives a return path that carries its own query, such as the Share tab's", async () => {
    expect(await paramsAfterRouting("/workspace/agent-aa11/options?tab=share&group=x")).toEqual({
      returnTo: "/workspace/agent-aa11/options?tab=share&group=x",
      intent: "start",
    });
  });

  it("still carries a plain return path", async () => {
    expect((await paramsAfterRouting("/goto/agent-aa11/")).returnTo).toBe("/goto/agent-aa11/");
  });
});
