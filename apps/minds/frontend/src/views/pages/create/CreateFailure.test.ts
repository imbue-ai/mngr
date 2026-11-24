import m from "mithril";
import { describe, expect, it } from "vitest";
import type { ErrorAction } from "../../../models/errors";
import { allText, attrsOf, byId, classesOf, renderDeep, required, withAttr } from "../../../testing";
import { CreateFailure, REPORT_BUG_LABEL } from "./CreateFailure";
import type { CreateFailureAttrs } from "./CreateFailure";

function mount(attrs: CreateFailureAttrs): { draw: () => unknown } {
  const component = CreateFailure();
  const vnode = { attrs } as m.Vnode<CreateFailureAttrs>;
  return { draw: () => renderDeep(component.view.call(component, vnode)) };
}

/** Each answer as drawn, in order: its id, its label, and whether it is a filled button, an outlined one, or a link. */
function drawnAnswers(drawn: unknown): [unknown, string, string][] {
  return withAttr(drawn, "data-answer").map((node) => {
    const classes = classesOf(node);
    const isButton = classes.includes("first-run-button");
    return [
      attrsOf(node)["data-answer"],
      allText(node),
      !isButton ? "link" : classes.includes("first-run-button--secondary") ? "outlined" : "filled",
    ];
  });
}

const ERROR = { message: "Couldn't create alpha. Try again.", details: "unknown or invalid runtime name: runsc" };

describe("CreateFailure", () => {
  it("keeps the ids the e2e workspace runner polls, around our sentence", () => {
    const { draw } = mount({ id: "failure-view", textId: "error-message", error: ERROR });
    expect(allText(required(byId(draw(), "error-message")))).toBe("Couldn't create alpha. Try again.");
    expect(allText(required(byId(draw(), "failure-view")))).toContain("Couldn't create alpha. Try again.");
  });

  it("names the See details toggle and the panel it opens from the notice's id", () => {
    const { draw } = mount({ id: "failure-view", textId: "error-message", error: ERROR });
    expect(byId(draw(), "failure-view-details-panel")).toBeUndefined();
    // Underlined by the first run's own modifier: the plain link's rule would strip a utility's underline.
    const toggleClasses = classesOf(required(byId(draw(), "failure-view-details")));
    expect(toggleClasses).toContain("first-run-link--inline");
    expect(toggleClasses).not.toContain("first-run-link--plain");
    (attrsOf(required(byId(draw(), "failure-view-details"))).onclick as () => void)();
    expect(allText(required(byId(draw(), "failure-view-details-panel")))).toContain(
      "unknown or invalid runtime name: runsc",
    );
  });

  it("fills the leading fix, outlines the rest, and sets the ways out beside Report a bug as quiet links", () => {
    const pressed: string[] = [];
    const actions: ErrorAction[] = [
      { label: "Run on this computer", onclick: () => pressed.push("run-here") },
      { label: "Retry", onclick: () => pressed.push("retry") },
      { label: "Dismiss", isDismissal: true, onclick: () => pressed.push("dismiss") },
    ];
    const { draw } = mount({ id: "failure-view", error: { message: "Couldn't create alpha.", details: "" }, actions });
    expect(drawnAnswers(draw())).toEqual([
      ["failure-view-dismissal-0", "Dismiss", "link"],
      ["failure-view-fix-0", "Run on this computer", "filled"],
      ["failure-view-fix-1", "Retry", "outlined"],
    ]);
    expect(withAttr(draw(), "data-aside").map((node) => allText(node))).toEqual([REPORT_BUG_LABEL]);
    for (const node of withAttr(draw(), "data-answer")) (attrsOf(node).onclick as () => void)();
    expect(pressed).toEqual(["dismiss", "run-here", "retry"]);
  });

  it("outlines a leading Retry that only follows something the sentence asks for first", () => {
    const { draw } = mount({
      id: "failure-view",
      error: { message: "Couldn't create alpha. To fix it, *start Docker*.", details: "" },
      actions: [{ label: "Retry", onclick: () => undefined }],
    });
    expect(drawnAnswers(draw())).toEqual([["failure-view-fix-0", "Retry", "outlined"]]);
  });

  it("offers no Report a bug where the caller says the notice is not a failure", () => {
    const { draw } = mount({
      id: "creation-interrupted",
      tone: "warn",
      isBugReportOffered: false,
      error: { message: "The app closed while this workspace was being created.", details: "" },
    });
    expect(withAttr(draw(), "data-aside")).toEqual([]);
  });

  it("draws a way out as the only quiet link when Report a bug is not offered", () => {
    const { draw } = mount({
      id: "creation-interrupted",
      isBugReportOffered: false,
      error: { message: "The app closed while this workspace was being created.", details: "" },
      actions: [{ label: "Dismiss", isDismissal: true, onclick: () => undefined }],
    });
    expect(withAttr(draw(), "data-aside")).toEqual([]);
    expect(withAttr(draw(), "data-answer").map((node) => allText(node))).toEqual(["Dismiss"]);
  });
});
