// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it } from "vitest";
import { mountInBody } from "../../testing";
import {
  InlineError,
  isFollowUpLed,
  markedLine,
  richErrorText,
} from "./errorDesign";

describe("richErrorText", () => {
  it("sets the ask apart and renders a command as code, keeping the sentence whole", () => {
    const root = mountInBody(() =>
      m(
        "p",
        richErrorText(
          "The SSO session has expired. To fix it, *run `aws sso login`*.",
        ),
      ),
    );

    expect(root.textContent).toBe(
      "The SSO session has expired. To fix it, run aws sso login.",
    );
    const ask = root.querySelector("em");
    expect(ask?.textContent).toBe("run aws sso login");
    expect(root.querySelector("code")?.textContent).toBe("aws sso login");
    // The code sits inside the ask, so the whole instruction reads as one run.
    expect(ask?.querySelector("code")).not.toBeNull();
  });

  it("leaves a command's own asterisks to the command", () => {
    const root = mountInBody(() =>
      m("p", richErrorText("*Run `rm *.log`, then check again.*")),
    );

    expect(root.textContent).toBe("Run rm *.log, then check again.");
    expect(root.querySelector("code")?.textContent).toBe("rm *.log");
    expect(root.querySelector("em")?.textContent).toBe(
      "Run rm *.log, then check again.",
    );
  });

  it("leaves a sentence with no markup, and a lone delimiter, as plain text", () => {
    const root = mountInBody(() =>
      m(
        "p",
        richErrorText("Couldn't turn sharing on. 2 * 3 workspaces are `on`."),
      ),
    );

    expect(root.textContent).toBe(
      "Couldn't turn sharing on. 2 * 3 workspaces are on.",
    );
    expect(root.querySelector("em")).toBeNull();
    expect(root.querySelector("code")?.textContent).toBe("on");
  });
});

describe("isFollowUpLed", () => {
  it("is led by a follow-up only when the copy asks for something first", () => {
    expect(
      isFollowUpLed("Couldn't create it. To fix it, *start Docker*.", "Retry"),
    ).toBe(true);
    // Nothing is asked of the user, so the button is the whole fix.
    expect(
      isFollowUpLed("Couldn't turn sharing on. Try again.", "Try again"),
    ).toBe(false);
    // The button does the asked-for thing itself rather than confirming it.
    expect(
      isFollowUpLed("*Sign in to Imbue Cloud again* to rename it.", "Sign in"),
    ).toBe(false);
    expect(isFollowUpLed("*Start Docker*, then retry.", undefined)).toBe(false);
    // The asterisks are the command's, so nothing is asked of the user.
    expect(
      isFollowUpLed("Couldn't read `~/.aws/*.json` or `~/.ssh/*`.", "Retry"),
    ).toBe(false);
  });
});

describe("markedLine", () => {
  it("leaves the sentence flush when the surface's title already carries the mark", () => {
    const marked = mountInBody(() => markedLine({}, "Couldn't start it."));
    const flush = mountInBody(() =>
      markedLine({ isTitleMarked: true }, "Couldn't start it."),
    );

    expect(marked.querySelectorAll("svg")).toHaveLength(1);
    expect(flush.querySelectorAll("svg")).toHaveLength(0);
  });
});

describe("InlineError", () => {
  it("keeps its text red at helper size, with its own mark", () => {
    const root = mountInBody(() =>
      m(InlineError, { message: "*Pick a different name.*" }),
    );

    const line = root.firstElementChild;
    expect(line?.className).toContain("text-important");
    expect(line?.className).toContain("type-helper");
    expect(root.querySelector("svg")).not.toBeNull();
    expect(root.querySelector("em")?.textContent).toBe(
      "Pick a different name.",
    );
  });
});
