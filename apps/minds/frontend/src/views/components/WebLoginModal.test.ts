// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { webLogin } from "../../models/webLogin";
import { clickModalBackdrop, mountInBody } from "../../testing";
import { type ModalBackdrop, dismissTopmostModal } from "./Modal";
import { WebLoginModal } from "./WebLoginModal";

afterEach(() => {
  webLogin.state = "idle";
  for (const root of [...document.body.children]) m.mount(root, null);
  document.body.replaceChildren();
});

/** The card of the sign-in modal as it is drawn while the browser sign-in is waited on. */
function cardOver(backdrop: ModalBackdrop): HTMLElement {
  webLogin.state = "waiting";
  const root = mountInBody(() => m(WebLoginModal, { backdrop }));
  const card = root.querySelector<HTMLElement>(".modal-viewport > div");
  if (card === null) throw new Error("no modal card");
  return card;
}

describe("WebLoginModal", () => {
  it("wears the first run's white card over a first-run page", () => {
    const card = cardOver("wash");

    expect(card.classList.contains("first-run-card")).toBe(true);
    expect(card.classList.contains("bg-surface-primary")).toBe(false);
  });

  it("keeps the app's white card everywhere else", () => {
    const card = cardOver("dim");

    expect(card.classList.contains("bg-surface-primary")).toBe(true);
    expect(card.classList.contains("first-run-card")).toBe(false);
  });

  it("keeps the sign-in going through a click on its backdrop", () => {
    cardOver("wash");

    clickModalBackdrop(document);

    expect(webLogin.state).toBe("waiting");
  });

  it("cancels the sign-in on Escape", () => {
    cardOver("wash");

    dismissTopmostModal();

    expect(webLogin.state).toBe("idle");
  });
});
