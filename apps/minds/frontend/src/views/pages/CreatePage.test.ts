import { describe, expect, it } from "vitest";
import { attrsOf, collectVnodes } from "../../testing";
import { DialogCloseButton } from "../components/Modal";
import { createFormModal, signInOutcome } from "./CreatePage";

describe("createFormModal", () => {
  it("carries a close control wired to the host, so the backdrop is not the only way out", () => {
    let closes = 0;
    const modal = createFormModal({
      key: "the-modal",
      id: "the-modal",
      onClose: () => {
        closes += 1;
      },
    });
    const closeButton = collectVnodes(modal).find((vnode) => vnode.tag === DialogCloseButton);
    expect(closeButton).toBeDefined();
    if (closeButton === undefined) return;
    (attrsOf(closeButton).onClose as () => void)();
    expect(closes).toBe(1);
  });
});

describe("signInOutcome", () => {
  it("counts the sign-in as landed on the flow's verdict or on an account reaching the channel first", () => {
    expect(signInOutcome("done", false)).toBe("signed-in");
    expect(signInOutcome("waiting", true)).toBe("signed-in");
  });

  it("drops the create once the modal is closed with no account", () => {
    expect(signInOutcome("idle", false)).toBe("abandoned");
  });

  it("keeps waiting while the modal is up, including on an error it offers a retry for", () => {
    for (const state of ["starting", "waiting", "finishing", "error"] as const) {
      expect(signInOutcome(state, false)).toBe("pending");
    }
  });
});
