// The downloaded-update offer: what it says, and which control does what.

import { describe, expect, it } from "vitest";
import { allText, attrsOf, collectVnodes, renderRoot, renderedText } from "../../testing";
import { UpdateReadyCard } from "./UpdateReadyCard";

function card(onRestart: () => void, onDismiss: () => void) {
  return renderRoot(UpdateReadyCard, { version: "0.4.2", onRestart, onDismiss });
}

/** The card's clickable controls, which is every vnode carrying an onclick. */
function controls(root: unknown) {
  return collectVnodes(root).filter((vnode) => typeof attrsOf(vnode).onclick === "function");
}

describe("the update-ready card", () => {
  it("names the version and says what restarting costs", () => {
    // The second line is what makes dismissing feel like a choice rather than
    // refusing the update: it installs on the next restart either way.
    const text = renderedText(
      card(
        () => {},
        () => {},
      ),
    );

    expect(text).toContain("Minds 0.4.2 is ready");
    expect(text).toContain("Installs when you restart");
  });

  it("restarts from the control that offers to, and dismisses from the other", () => {
    // Each control is found by what it says, never by its position, so handlers
    // wired to the wrong one fail here instead of shipping a prominent button
    // that quietly dismisses and a glyph that restarts the app mid-edit.
    const clicked: string[] = [];
    const root = card(
      () => clicked.push("restart"),
      () => clicked.push("dismiss"),
    );

    const clickable = controls(root);
    expect(clickable).toHaveLength(2);
    const restart = clickable.find((vnode) => allText(vnode.children).includes("Restart now"));
    const dismiss = clickable.find((vnode) => attrsOf(vnode)["aria-label"] === "Dismiss");
    expect(restart).toBeDefined();
    expect(dismiss).toBeDefined();

    (attrsOf(restart!).onclick as () => void)();
    expect(clicked).toEqual(["restart"]);

    (attrsOf(dismiss!).onclick as () => void)();
    expect(clicked).toEqual(["restart", "dismiss"]);
  });
});
