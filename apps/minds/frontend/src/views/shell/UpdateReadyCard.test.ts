// The downloaded-update offer: what it says, and which control does what.

import { describe, expect, it } from "vitest";
import { allText, attrsOf, collectVnodes, renderRoot, renderedText } from "../../testing";
import { UpdateReadyCard, type UpdateReadyCardAttrs } from "./UpdateReadyCard";

/** The card as the plain macOS offer, with `overrides` picking the state under test. */
function card(overrides: Partial<UpdateReadyCardAttrs> = {}) {
  const attrs: UpdateReadyCardAttrs = {
    version: "0.4.2",
    installPolicy: "on-quit",
    needsPassword: false,
    error: null,
    isInstalling: false,
    onRestart: () => {},
    onDismiss: () => {},
    ...overrides,
  };
  return renderRoot(UpdateReadyCard, attrs);
}

/** The card's clickable controls, which is every vnode carrying an onclick. */
function controls(root: unknown) {
  return collectVnodes(root).filter((vnode) => typeof attrsOf(vnode).onclick === "function");
}

/** The held button an installing card shows in place of a live one. */
function heldControl(root: unknown) {
  const held = collectVnodes(root).find((vnode) => attrsOf(vnode).disabled === true);
  expect(held).toBeDefined();
  return held!;
}

describe("the update-ready card", () => {
  it("names the version and says what restarting costs", () => {
    // The second line is what makes dismissing feel like a choice rather than
    // refusing the update: it installs on the next restart either way.
    const text = renderedText(card());

    expect(text).toContain("Mind 0.4.2 is ready");
    expect(text).toContain("Installs when you restart");
  });

  it("says the install waits for the click where nothing installs on quit", () => {
    // On Linux the update goes in only from this control, and on a .deb the
    // click is followed by the system password prompt -- both worth saying
    // before the button is pressed, since "Restart now" would promise a
    // restart that instead pauses on a dialog.
    const appImage = renderedText(card({ installPolicy: "on-request", needsPassword: false }));
    expect(appImage).toContain("Installs when you ask");
    expect(appImage).not.toContain("password");
    expect(appImage).toContain("Install and restart");
    expect(appImage).not.toContain("Restart now");

    const deb = renderedText(card({ installPolicy: "on-request", needsPassword: true }));
    expect(deb).toContain("you'll be asked for your password");
  });

  it("restarts from the control that offers to, and dismisses from the other", () => {
    // Each control is found by what it says, never by its position, so handlers
    // wired to the wrong one fail here instead of shipping a prominent button
    // that quietly dismisses and a glyph that restarts the app mid-edit.
    const clicked: string[] = [];
    const root = card({
      onRestart: () => clicked.push("restart"),
      onDismiss: () => clicked.push("dismiss"),
    });

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

  it("says why the last install did not go through, so the click does not look ignored", () => {
    // On a .deb the install runs under the system password prompt; cancelling
    // it leaves the app up with the download still staged. The card stays,
    // with the button live, and this line is the only account of what happened.
    const failed = card({
      installPolicy: "on-request",
      needsPassword: true,
      error: "Installing the update failed: Command failed: pkexec",
    });

    expect(renderedText(failed)).toContain("Installing the update failed: Command failed: pkexec");
    expect(controls(failed)).toHaveLength(2);
  });

  it("holds the button and says what is happening while the install runs", () => {
    // On a .deb the install blocks the main process for as long as dpkg takes,
    // with the password prompt as the only other sign of life; a card still
    // offering "Install and restart" through that reads as a click that did
    // nothing. Nothing is dismissible either: this is the only account of it.
    const installing = card({ installPolicy: "on-request", needsPassword: true, isInstalling: true });

    const text = renderedText(installing);
    expect(text).toContain("Installing Mind 0.4.2");
    expect(text).toContain("Enter your password when asked");
    expect(text).not.toContain("Install and restart");
    expect(controls(installing)).toHaveLength(0);
    expect(allText(heldControl(installing).children)).toContain("Installing...");
  });

  it("says it is restarting, not installing, where the update installs on quit", () => {
    // On macOS the click was "Restart now" and Squirrel does the install as
    // part of that restart, so the held state continues that sentence rather
    // than starting a new one about installing.
    const restarting = card({ isInstalling: true });

    const text = renderedText(restarting);
    expect(text).toContain("Restarting into Mind 0.4.2");
    expect(text).not.toContain("Installing");
    expect(allText(heldControl(restarting).children)).toContain("Restarting...");
  });
});
