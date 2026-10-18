import { describe, expect, it } from "vitest";
import { attrsOf, renderRoot } from "../../testing";
import { OverlayBackdrop } from "./OverlayBackdrop";

describe("OverlayBackdrop alignment", () => {
  it("hangs a full-window (app-level) modal from the top, not centered", () => {
    const root = renderRoot(
      OverlayBackdrop,
      { backdropId: "b", onDismiss: () => undefined, fullWindow: true },
    );
    const className = String(attrsOf(root).className);
    expect(className).toContain("items-start");
    expect(className).not.toContain("items-center");
  });

  it("keeps the docked workspace-options overlay vertically centered", () => {
    const root = renderRoot(OverlayBackdrop, {
      backdropId: "b",
      onDismiss: () => undefined,
    });
    const className = String(attrsOf(root).className);
    expect(className).toContain("items-center");
    expect(className).not.toContain("items-start");
  });
});
