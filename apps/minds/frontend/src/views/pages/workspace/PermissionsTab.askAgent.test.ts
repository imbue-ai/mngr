// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PermissionsModel } from "../../../models/workspacePermissions";
import { permissionsView } from "../../../models/workspacePermissions.testing";
import { buttonLabelled, mountInBody, primaryButtonLabels, settle } from "../../../testing";
import { PermissionsTab } from "./PermissionsTab";

const AGENT_ID = "agent-" + "d".repeat(8);

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

/** The tab after a change the gateway refused, with that refusal's "Ask the
 * agent to request it again" pressed and refused in turn. */
async function refuseAChangeThenTheAsk(): Promise<{
  root: HTMLElement;
  assistBodies: { description: string; workspace_agent_id: string }[];
}> {
  const model = new PermissionsModel(AGENT_ID, {
    fetchJson: async (url: string) =>
      url.endsWith("/connector-toggle")
        ? {
            ok: false,
            status: 409,
            body: {
              error: "Imbue Studio no longer knows what this permission covers.",
              detail: "Permission 'slack-chat-read' cannot be re-enabled because its definition is gone.",
              remedy: "ASK_AGENT",
            },
          }
        : { ok: true, status: 200, body: permissionsView() },
    redraw: () => m.redraw(),
  });
  const assistBodies: { description: string; workspace_agent_id: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/help/assist") assistBodies.push(JSON.parse(String(init?.body)));
      // A refusal keeps the tab where it is, so nothing navigates.
      return new Response(JSON.stringify({ error: "No assist skill here." }), { status: 409 });
    }),
  );
  const root = mountInBody(() =>
    m(PermissionsTab, {
      model,
      workspaceName: "alpha",
      requestedSection: null,
      onSelectSection: () => undefined,
      onReviewRequest: () => undefined,
    }),
  );
  await settle();
  m.redraw.sync();

  root.querySelector<HTMLElement>(".perm-switch")!.click();
  await settle();
  m.redraw.sync();
  expect(primaryButtonLabels(root)).toEqual(["Ask the agent to request it again"]);
  buttonLabelled(root, "Ask the agent to request it again").click();
  await settle();
  m.redraw.sync();

  return { root, assistBodies };
}

describe("PermissionsTab's Ask the agent to request it again", () => {
  it("tells the agent what was refused and why, without the status line, and withdraws the ask once the workspace refuses it", async () => {
    const { root, assistBodies } = await refuseAChangeThenTheAsk();

    // The refused start shows under the refused change's row, and a second
    // press would be refused the same way, so the ask is gone and the refusal
    // offers no report.
    const refusal = root.querySelector("#ws-perm-error #ws-perm-ask-agent-error");
    expect(refusal?.textContent).toContain("No assist skill here.");
    expect(refusal?.textContent).not.toContain("Report a bug");
    expect(root.querySelector("#ws-perm-ask-agent")).toBeNull();

    expect(assistBodies).toHaveLength(1);
    expect(assistBodies[0].workspace_agent_id).toBe(AGENT_ID);
    const { description } = assistBodies[0];
    expect(description).toMatch(/^Imbue Studio refused a permission change the user made/);
    expect(description).not.toMatch(/\bI\b/);
    expect(description).toContain('"Imbue Studio no longer knows what this permission covers."');
    expect(description).toContain("Request that permission again so the user can approve it.");
    expect(description).toContain(
      "Permission 'slack-chat-read' cannot be re-enabled because its definition is gone.",
    );
    expect(description).not.toContain("HTTP");
    expect(description).not.toContain("409");
  });

  it("draws the refused ask on the refused change's own box rather than in a second one", async () => {
    const { root } = await refuseAChangeThenTheAsk();

    const tint = (noticeId: string): string =>
      root.querySelector(`#${noticeId} > div`)?.className ?? "";
    expect(tint("ws-perm-error")).toContain("c-important-surface");
    expect(tint("ws-perm-ask-agent-error")).not.toContain("c-important-surface");
    // Only the box went: the refusal still says what failed.
    expect(root.querySelector("#ws-perm-ask-agent-error")?.textContent).toContain("No assist skill here.");
  });
});
