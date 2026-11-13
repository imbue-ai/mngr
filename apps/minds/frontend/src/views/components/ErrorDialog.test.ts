// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ErrorDialogQueue,
  FALLBACK_DIALOG_TITLE,
  titledError,
} from "../../models/errorDialog";
import {
  buttonLabelled,
  mountInBody,
  primaryButtonLabels,
} from "../../testing";
import { BTN_SIZES } from "./constants";
import { ErrorDialogLayer } from "./ErrorDialog";

function mountLayer(queue: ErrorDialogQueue): HTMLElement {
  return mountInBody(() => m(ErrorDialogLayer, { queue }));
}

/** The dialog's x. It is the only way to dismiss one: a dialog beside it gets
 * no Close button of its own. */
function dismissX(root: ParentNode): HTMLButtonElement {
  const button = root.querySelector<HTMLButtonElement>(
    "button[aria-label=Close]",
  );
  if (button === null) throw new Error("no dismiss x");
  return button;
}

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("ErrorDialogLayer", () => {
  it("shows a raised error in a styled dialog, and dismissing it brings up the next one waiting", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    const alert = vi.spyOn(window, "alert").mockImplementation(() => undefined);

    queue.show({
      error: { message: 'Couldn\'t start "alpha".', details: "HTTP 502" },
    });
    queue.show({ error: { message: 'Couldn\'t stop "beta".', details: "" } });
    m.redraw.sync();

    expect(alert).not.toHaveBeenCalled();
    expect(root.querySelector("[role=alertdialog]")?.textContent).toContain(
      'Couldn\'t start "alpha".',
    );
    expect(root.textContent).not.toContain("beta");

    dismissX(root).click();
    m.redraw.sync();

    expect(root.textContent).toContain('Couldn\'t stop "beta".');
  });

  it("titles every dialog by what failed, falling back rather than going untitled", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    queue.show({
      title: 'Couldn\'t start "alpha"',
      error: { message: "Cannot connect to Docker daemon.", details: "" },
    });
    queue.show({ error: { message: "Couldn't do that.", details: "" } });
    m.redraw.sync();

    const title = root.querySelector(".type-heading");
    expect(title?.textContent).toBe('Couldn\'t start "alpha"');
    // The title carries the mark, so the sentence under it is flush.
    expect(title?.querySelector("svg")).not.toBeNull();
    expect(root.querySelector("p")?.querySelector("svg")).toBeNull();

    dismissX(root).click();
    m.redraw.sync();

    expect(root.querySelector(".type-heading")?.textContent).toBe(
      FALLBACK_DIALOG_TITLE,
    );
  });

  it("offers no Close beside the x, and no button row at all with nothing to press", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    queue.show({
      title: "Can't start \"alpha\"",
      error: { message: "This workspace has been retired.", details: "" },
      isBugReportOffered: false,
    });
    m.redraw.sync();

    expect(
      [...root.querySelectorAll("button")].map((button) => button.textContent),
    ).toEqual([""]);
  });

  it("draws the next dialog with its details shut, whatever the last one left open", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    queue.show({ error: { message: "First.", details: "first detail" } });
    queue.show({ error: { message: "Second.", details: "second detail" } });
    m.redraw.sync();
    buttonLabelled(root, "See details").click();
    m.redraw.sync();
    expect(root.textContent).toContain("first detail");

    dismissX(root).click();
    m.redraw.sync();

    expect(root.textContent).toContain("Second.");
    expect(root.textContent).not.toContain("second detail");
  });

  it("makes the error's first fix its main button, at the dialog's own size", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    queue.show({
      title: 'Couldn\'t start "alpha"',
      error: { message: "Cannot connect to Docker daemon.", details: "" },
      actions: [
        { label: "Start workspace", onclick: () => undefined },
        { label: "Try again", onclick: () => undefined },
      ],
    });
    queue.show({
      title: "Couldn't show this request",
      error: { message: "Imbue Studio doesn't know this scope.", details: "" },
      actions: [{ label: "Deny", onclick: () => undefined, isDestructive: true }],
    });
    m.redraw.sync();

    expect(primaryButtonLabels(root)).toEqual(["Start workspace"]);
    // A dialog's buttons are the dialog's own, not the compact ones a notice
    // tucks under its sentence.
    expect(buttonLabelled(root, "Start workspace").className).toContain(
      BTN_SIZES.md,
    );

    // A Deny leading the row is drawn in the danger color, never as the main
    // button -- and no Close steps in to be one.
    dismissX(root).click();
    m.redraw.sync();
    expect(primaryButtonLabels(root)).toEqual([]);
  });

  it("closes itself before running the action pressed", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    const startWorkspace = vi.fn();
    queue.show({
      title: 'Couldn\'t start "alpha"',
      error: { message: "Cannot connect to Docker daemon.", details: "" },
      actions: [{ label: "Start workspace", onclick: startWorkspace }],
    });
    m.redraw.sync();

    buttonLabelled(root, "Start workspace").click();
    m.redraw.sync();

    expect(startWorkspace).toHaveBeenCalledTimes(1);
    expect(queue.current).toBeNull();
    expect(root.querySelector("[role=alertdialog]")).toBeNull();
  });

  it("falls back to a native alert pointing at the log, with the details logged, when no dialog can be drawn", () => {
    const queue = new ErrorDialogQueue();
    const alert = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    const logged = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);

    queue.show({
      error: {
        message: "Couldn't remove this workspace from the list.",
        details: "HTTP 502\nNo such record",
      },
    });

    expect(queue.current).toBeNull();
    expect(alert).toHaveBeenCalledTimes(1);
    const alerted = String(alert.mock.calls[0][0]);
    expect(alerted).toContain("Couldn't remove this workspace from the list.");
    expect(alerted).toContain("See the log for details.");
    // The details are too much for an alert; they go to the log instead.
    expect(alerted).not.toContain("No such record");
    expect(String(logged.mock.calls[0][0])).toContain(
      "HTTP 502\nNo such record",
    );
  });

  it("still names what failed in the fallback alert, which has no title to draw", () => {
    const queue = new ErrorDialogQueue();
    const alert = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    queue.show(
      titledError('Couldn\'t remove "alpha"', {
        message:
          'Couldn\'t remove "alpha". Try again. If this persists, report a bug.',
        details: "HTTP 500",
      }),
    );

    expect(String(alert.mock.calls[0][0])).toBe(
      'Couldn\'t remove "alpha". Try again. If this persists, report a bug. ' +
        "See the log for details.",
    );
  });

  it("falls back again once the dialog's host is gone", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    m.mount(root, null);
    const alert = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    queue.show({ error: { message: "Couldn't do that.", details: "" } });

    expect(alert).toHaveBeenCalledTimes(1);
  });
});

describe("titledError", () => {
  it("moves what failed into the title and leaves the rest as the sentence", () => {
    const queue = new ErrorDialogQueue();
    const root = mountLayer(queue);
    queue.show(
      titledError('Couldn\'t start "alpha"', {
        message: 'Couldn\'t start "alpha". Cannot connect to Docker daemon.',
        details: "HTTP 502",
      }),
    );
    m.redraw.sync();

    expect(root.querySelector(".type-heading")?.textContent).toBe(
      'Couldn\'t start "alpha"',
    );
    expect(root.querySelector("p")?.textContent).toBe(
      "Cannot connect to Docker daemon. See details",
    );
  });
});
