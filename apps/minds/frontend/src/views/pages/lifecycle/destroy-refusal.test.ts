// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { destroyDispatchFailureFor } from "../../../models/workspaceOptions";
import { withMindsNative } from "../../../testing";
import { destroyRefusalActions } from "./destroy-refusal";

const surface = {
  retry: { label: "Try again", onclick: () => undefined },
  backToWorkspaces: () => undefined,
};

describe("destroyRefusalActions", () => {
  it("offers Update Imbue Studio for a workspace a newer version set up", async () => {
    const refusal = destroyDispatchFailureFor({
      status: 409,
      body: { error: "managed by a newer version", code: "RECORD_TOO_NEW" },
    });

    await withMindsNative({}, async () => {
      expect(
        destroyRefusalActions(refusal, surface).map((action) => action.label),
      ).toEqual(["Update Imbue Studio"]);
    });
  });

  it("restarts the app for a refusal whose fix is a restart", async () => {
    const refusal = destroyDispatchFailureFor({
      status: 501,
      body: {
        error: "Workspace management not configured",
        code: "WORKSPACE_MANAGEMENT_UNCONFIGURED",
      },
    });
    const retry = vi.fn();

    await withMindsNative({ retry }, async () => {
      const [restart] = destroyRefusalActions(refusal, surface);
      expect(restart.label).toBe("Restart Imbue Studio");
      restart.onclick();
    });

    expect(retry).toHaveBeenCalledOnce();
  });
});
