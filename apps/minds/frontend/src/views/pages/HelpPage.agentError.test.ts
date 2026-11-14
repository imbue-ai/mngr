// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { HelpModel, setPendingHelpLaunch } from "../../models/help";
import {
  buttonLabelled,
  jsonResponse,
  memoryStorage,
  mountInBody,
} from "../../testing";
import { agentErrorPhase } from "./HelpPage";

afterEach(() => {
  document.body.replaceChildren();
});

async function failedModel(
  answers: Response[],
  onClose: () => void = () => undefined,
): Promise<HelpModel> {
  setPendingHelpLaunch({
    workspaceAgentId: "agent-1",
    isAssistAvailable: true,
  });
  const model = new HelpModel({
    storage: memoryStorage(),
    fetcher: () => Promise.resolve(answers.shift() as Response),
    onClose,
  });
  model.description = "help me";
  await model.submit();
  return model;
}

function mountPhase(
  model: HelpModel,
  onUpdateWorkspace: () => void,
): HTMLElement {
  return mountInBody(() => agentErrorPhase(model, onUpdateWorkspace));
}

describe("HelpPage agent error phase", () => {
  it("offers Try again beside Back to report, with the verdict collapsed, and Try again starts the agent", async () => {
    let closed = false;
    const model = await failedModel(
      [
        jsonResponse(
          {
            error: "Couldn't start an agent in this workspace.",
            reason: "SPAWN_FAILED",
            detail: "Error: no account is signed in",
          },
          502,
        ),
        jsonResponse({ ok: true }),
      ],
      () => {
        closed = true;
      },
    );
    const root = mountPhase(model, () => undefined);

    expect(root.textContent).toContain(
      "Couldn't start an agent in this workspace. Try again. If this persists, report a bug.",
    );
    expect(buttonLabelled(root, "Back to report")).toBeDefined();
    // The verdict is behind See details, not on the visible line.
    expect(root.querySelector("pre")).toBeNull();
    expect(root.textContent).toContain("See details");

    buttonLabelled(root, "Try again").click();
    await vi.waitFor(() => expect(closed).toBe(true));
  });

  it("offers Update workspace, not Try again, for a workspace too old to start a helper", async () => {
    const model = await failedModel([
      jsonResponse({ error: "too old", reason: "TOO_OLD" }, 409),
    ]);
    const updates: string[] = [];
    const root = mountPhase(model, () => updates.push("update"));

    expect(() => buttonLabelled(root, "Try again")).toThrow();
    buttonLabelled(root, "Update workspace").click();
    expect(updates).toEqual(["update"]);
  });
});
