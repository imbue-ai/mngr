// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { remedyFromBody } from "../../models/errors";
import { withMindsNative } from "../../testing";
import { remedyActions } from "./errorRemedies";

describe("remedyActions for a route's named remedy", () => {
  it("draws the restart button a refusal names, and restarts the app from it", async () => {
    const retry = vi.fn();
    await withMindsNative({ retry }, async () => {
      const actions = remedyActions(
        remedyFromBody({
          error: "Imbue Studio's permission service isn't running.",
          remedy: "RESTART_APP",
        }),
      );

      expect(actions.map((action) => action.label)).toEqual([
        "Restart Imbue Studio",
      ]);
      actions[0].onclick();
    });

    expect(retry).toHaveBeenCalledOnce();
  });

  it("offers no button for a remedy this build does not know", async () => {
    await withMindsNative({}, async () => {
      expect(
        remedyActions(remedyFromBody({ error: "x", remedy: "REINSTALL_APP" })),
      ).toEqual([]);
    });
  });

  it("asks the agent only where the surface says how", () => {
    const ask = { label: "Ask the agent to fix it", onclick: vi.fn() };

    expect(remedyActions("ASK_AGENT")).toEqual([]);
    expect(remedyActions("ASK_AGENT", { askAgent: () => [ask] })).toEqual([
      ask,
    ]);
  });
});
