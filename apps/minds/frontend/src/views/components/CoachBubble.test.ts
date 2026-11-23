// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buttonLabelled, mountInBody } from "../../testing";
import { CoachBubble, coachBubbleSide } from "./CoachBubble";

afterEach(() => {
  for (const root of [...document.body.children]) m.mount(root, null);
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

/** A bubble mounted beside a stand-in control, as its owner draws it. */
function mountBubble(onDismiss: () => void = () => undefined): HTMLElement {
  return mountInBody(() =>
    m("span", { id: "control", class: "relative inline-flex" }, [
      m("button", "The control"),
      m(CoachBubble, {
        id: "the-bubble",
        text: "Use this to do the thing.",
        onDismiss,
      }),
    ]),
  );
}

function bubbleIn(root: ParentNode): HTMLElement {
  const bubble = root.querySelector<HTMLElement>("#the-bubble");
  if (bubble === null) throw new Error("no bubble");
  return bubble;
}

/** Report `rect` as the box of the stand-in control, and nothing else's. */
function placeControlAt(rect: { top: number; bottom: number }): void {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const box = this.id === "control" ? rect : { top: 0, bottom: 0 };
      return { ...box, left: 0, right: 0, width: 0, height: 0 } as DOMRect;
    },
  );
}

describe("coachBubbleSide", () => {
  const bounds = { top: 100, bottom: 600 };

  it("sits below the control when the bubble fits there", () => {
    expect(coachBubbleSide({ top: 200, bottom: 240 }, 80, bounds)).toBe(
      "below",
    );
    // The gap the arrow spans counts: 240 + 10 + 350 is exactly the bottom.
    expect(coachBubbleSide({ top: 200, bottom: 240 }, 350, bounds)).toBe(
      "below",
    );
  });

  it("sits above when it fits only there", () => {
    expect(coachBubbleSide({ top: 500, bottom: 540 }, 80, bounds)).toBe(
      "above",
    );
  });

  it("stays below when it fits on neither side", () => {
    expect(coachBubbleSide({ top: 150, bottom: 560 }, 80, bounds)).toBe(
      "below",
    );
  });
});

describe("CoachBubble", () => {
  it("says its text beside one button, and carries the id it was given", () => {
    const bubble = bubbleIn(mountBubble());

    expect(bubble.querySelector("p")?.textContent).toBe(
      "Use this to do the thing.",
    );
    expect(
      [...bubble.querySelectorAll("button")].map((b) => b.textContent),
    ).toEqual(["Got it"]);
    expect(bubble.dataset.coachBubble).toBe("below");
  });

  it("tells its owner when the button is pressed", () => {
    const onDismiss = vi.fn();
    const root = mountBubble(onDismiss);

    buttonLabelled(bubbleIn(root), "Got it").click();

    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("moves above a control too near the bottom of the window, and back when there is room again", () => {
    placeControlAt({ top: 700, bottom: window.innerHeight - 4 });
    const root = mountBubble();
    m.redraw.sync();
    expect(bubbleIn(root).dataset.coachBubble).toBe("above");

    placeControlAt({ top: 200, bottom: 240 });
    window.dispatchEvent(new Event("scroll"));
    m.redraw.sync();
    expect(bubbleIn(root).dataset.coachBubble).toBe("below");
  });

  it("stops watching the window once it is gone", () => {
    const removed: string[] = [];
    vi.spyOn(window, "removeEventListener").mockImplementation(((
      type: string,
    ) => {
      removed.push(type);
    }) as typeof window.removeEventListener);
    const root = mountBubble();

    m.mount(root, null);

    expect(removed).toEqual(["resize", "scroll"]);
  });
});
