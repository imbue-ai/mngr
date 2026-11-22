// @vitest-environment jsdom
//
// Rendered into a real document: a Modal counts as on screen from the moment
// its element is created until it is removed, which only mithril's own
// lifecycle says.
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { Modal, dismissTopmostModal, isAnyModalOpen } from "./Modal";

let root: HTMLElement | null = null;

/** Draw `children` as the whole page, the way a redraw would. */
function draw(children: m.Children): void {
  if (root === null) {
    root = document.createElement("div");
    document.body.appendChild(root);
  }
  m.render(root, children);
}

afterEach(() => {
  if (root === null) return;
  m.render(root, null);
  root.remove();
  root = null;
});

/** A page of two sibling Modals, each recording its own dismissal under its name. */
function twoModals(
  closed: string[],
  open: { first: boolean; second: boolean },
): m.Children {
  return [
    m(Modal, { isOpen: open.first, onClose: () => closed.push("first") }, "first"),
    m(Modal, { isOpen: open.second, onClose: () => closed.push("second") }, "second"),
  ];
}

describe("isAnyModalOpen", () => {
  it("is false with no Modal drawn, and for a Modal that is drawn shut", () => {
    expect(isAnyModalOpen()).toBe(false);

    draw(m(Modal, { isOpen: false }, "body"));

    expect(isAnyModalOpen()).toBe(false);
  });

  it("is true from the draw that opens a Modal until the draw that shuts it", () => {
    draw(m(Modal, { isOpen: false }, "body"));
    draw(m(Modal, { isOpen: true }, "body"));
    expect(isAnyModalOpen()).toBe(true);

    // A redraw of the open Modal is still the one Modal.
    draw(m(Modal, { isOpen: true }, "body"));
    expect(isAnyModalOpen()).toBe(true);

    draw(m(Modal, { isOpen: false }, "body"));
    expect(isAnyModalOpen()).toBe(false);
  });

  it("is false once the page holding an open Modal is gone", () => {
    draw(m("div", m(Modal, { isOpen: true }, "body")));
    expect(isAnyModalOpen()).toBe(true);

    draw(null);

    expect(isAnyModalOpen()).toBe(false);
  });

  it("stays true while any one of several Modals is still open", () => {
    const closed: string[] = [];
    draw(twoModals(closed, { first: true, second: true }));

    draw(twoModals(closed, { first: false, second: true }));
    expect(isAnyModalOpen()).toBe(true);

    draw(twoModals(closed, { first: false, second: false }));
    expect(isAnyModalOpen()).toBe(false);
  });
});

describe("dismissTopmostModal", () => {
  it("reports no Modal, and closes nothing, when none is open", () => {
    const closed: string[] = [];
    draw(twoModals(closed, { first: false, second: false }));

    expect(dismissTopmostModal()).toBe(false);

    expect(closed).toEqual([]);
  });

  it("asks the open Modal to close, as a click on its backdrop would, and leaves the closing to its owner", () => {
    let closes = 0;
    const onClose = (): void => {
      closes += 1;
    };
    draw(m(Modal, { isOpen: true, onClose }, "body"));

    expect(dismissTopmostModal()).toBe(true);

    expect(closes).toBe(1);
    // Still on screen until the owner draws it shut.
    expect(isAnyModalOpen()).toBe(true);
    draw(m(Modal, { isOpen: false, onClose }, "body"));
    expect(dismissTopmostModal()).toBe(false);
    expect(closes).toBe(1);
  });

  it("reports a Modal that cannot be dismissed as taking the key, without closing it", () => {
    draw(m(Modal, { isOpen: true }, "body"));

    expect(dismissTopmostModal()).toBe(true);
    expect(dismissTopmostModal()).toBe(true);

    expect(isAnyModalOpen()).toBe(true);
  });

  it("calls the onClose of the latest draw, not the one the Modal opened with", () => {
    const closed: string[] = [];
    draw(m(Modal, { isOpen: true, onClose: () => closed.push("stale") }, "body"));
    draw(m(Modal, { isOpen: true, onClose: () => closed.push("current") }, "body"));

    dismissTopmostModal();

    expect(closed).toEqual(["current"]);
  });

  it("closes the Modal that opened last, whichever comes first on the page", () => {
    const closed: string[] = [];
    // The second on the page opens first; the first opens over it.
    draw(twoModals(closed, { first: false, second: true }));
    draw(twoModals(closed, { first: true, second: true }));

    expect(dismissTopmostModal()).toBe(true);

    expect(closed).toEqual(["first"]);
  });

  it("works down the stack as each topmost Modal is drawn shut", () => {
    const closed: string[] = [];
    draw(twoModals(closed, { first: true, second: false }));
    draw(twoModals(closed, { first: true, second: true }));

    dismissTopmostModal();
    draw(twoModals(closed, { first: true, second: false }));
    dismissTopmostModal();
    draw(twoModals(closed, { first: false, second: false }));

    expect(closed).toEqual(["second", "first"]);
    expect(dismissTopmostModal()).toBe(false);
  });

  it("keeps the Modal on top there when one beneath it is removed", () => {
    const closed: string[] = [];
    draw(twoModals(closed, { first: true, second: false }));
    draw(twoModals(closed, { first: true, second: true }));

    draw(twoModals(closed, { first: false, second: true }));
    dismissTopmostModal();

    expect(closed).toEqual(["second"]);
  });

  it("puts a Modal that was shut and opened again back on top", () => {
    const closed: string[] = [];
    draw(twoModals(closed, { first: true, second: false }));
    draw(twoModals(closed, { first: true, second: true }));
    draw(twoModals(closed, { first: false, second: true }));

    draw(twoModals(closed, { first: true, second: true }));
    dismissTopmostModal();

    expect(closed).toEqual(["first"]);
  });
});

describe("a Modal raised from inside another", () => {
  /** A dialog whose body can raise a confirmation of its own. */
  function nested(closed: string[], isConfirmationOpen: boolean): m.Children {
    return m(
      Modal,
      { isOpen: true, onClose: () => closed.push("dialog") },
      m("div", [
        "dialog body",
        m(Modal, { isOpen: isConfirmationOpen, onClose: () => closed.push("confirmation") }, "confirm?"),
      ]),
    );
  }

  it("closes the confirmation first when it opens later, then the dialog under it", () => {
    const closed: string[] = [];
    draw(nested(closed, false));
    draw(nested(closed, true));

    dismissTopmostModal();
    draw(nested(closed, false));
    dismissTopmostModal();

    expect(closed).toEqual(["confirmation", "dialog"]);
  });

  it("closes the confirmation first when both arrive in one draw", () => {
    const closed: string[] = [];
    draw(nested(closed, true));

    dismissTopmostModal();

    expect(closed).toEqual(["confirmation"]);
  });

  it("forgets both when the dialog is removed with its confirmation still open", () => {
    const closed: string[] = [];
    draw(nested(closed, true));

    draw(null);

    expect(isAnyModalOpen()).toBe(false);
    expect(dismissTopmostModal()).toBe(false);
    expect(closed).toEqual([]);
  });
});
