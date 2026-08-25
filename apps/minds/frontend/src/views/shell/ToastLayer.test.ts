import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UiNotificationEntry } from "../../channel/messages";
import type { AnyVnode } from "../../testing";
import {
  allText,
  attrsOf,
  classesOf,
  classTokensOf,
  collectVnodes,
  notificationEntry as entry,
} from "../../testing";
import type { ToastLayerAttrs } from "./ToastLayer";
import {
  TOAST_EST_HEIGHT,
  TOAST_EXPAND_GAP,
  TOAST_MS,
  TOAST_PEEK,
  TOAST_STACK_MAX,
  ToastCard,
  ToastLayer,
  ToastStackItem,
  collapsedToastStyle,
  expandedToastStyle,
  expandedToastTops,
  toastMoreTopPx,
  toastOverflowCount,
} from "./ToastLayer";

/** Expands one level of an unrendered component vnode by calling its own
 * view() -- mirrors the shallow-render helper used elsewhere in this repo's
 * mithril tests (e.g. Shell.test.ts's renderComponent). */
function renderComponentVnode(vnode: AnyVnode): AnyVnode {
  const component = (vnode.tag as unknown as () => m.Component)();
  return (component.view as unknown as (v: unknown) => AnyVnode).call(
    component,
    { attrs: vnode.attrs, children: vnode.children },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("toast stack layout math", () => {
  it("shows the front card full, peeks the next few as scaled slivers, hides the rest", () => {
    // Front card: fully shown, on top, the only one that takes clicks.
    expect(collapsedToastStyle(0, 5, 70)).toEqual({
      transform: "translateY(0)",
      opacity: "1",
      "z-index": "5",
    });
    // Peeking cards: clipped to the front card's height plus i*PEEK,
    // bottom-aligned, scaled and faded by depth, behind by z.
    expect(collapsedToastStyle(1, 5, 70)).toEqual({
      height: `${70 + TOAST_PEEK}px`,
      overflow: "hidden",
      display: "flex",
      "flex-direction": "column",
      "justify-content": "flex-end",
      transform: "scale(0.97)",
      "transform-origin": "bottom center",
      opacity: "0.94",
      "z-index": "4",
      "pointer-events": "none",
    });
    expect(collapsedToastStyle(2, 5, 70).transform).toBe("scale(0.94)");
    expect(collapsedToastStyle(2, 5, 70).opacity).toBe("0.88");
    // Past the peek depth: folded into the "N more" line.
    expect(collapsedToastStyle(TOAST_STACK_MAX, 5, 70)).toEqual({
      opacity: "0",
      "pointer-events": "none",
      "z-index": String(5 - TOAST_STACK_MAX),
    });
  });

  it("stacks the open list by cumulative measured heights plus the gap", () => {
    expect(expandedToastTops([70, 50, 90])).toEqual([
      0,
      70 + TOAST_EXPAND_GAP,
      120 + 2 * TOAST_EXPAND_GAP,
    ]);
    expect(expandedToastStyle(1, 3, 78)).toEqual({
      transform: "translateY(78px)",
      opacity: "1",
      "z-index": "2",
    });
  });

  it("counts everything but the front card -- peeking slivers are not legible either -- and only while collapsed", () => {
    expect(toastOverflowCount(5, false)).toBe(4);
    expect(toastOverflowCount(1, false)).toBe(0);
    expect(toastOverflowCount(5, true)).toBe(0);
  });

  it("floats the 'N more' line below the collapsed pile", () => {
    expect(toastMoreTopPx(70, 5)).toBe(
      70 + (TOAST_STACK_MAX - 1) * TOAST_PEEK + 12,
    );
    expect(toastMoreTopPx(70, 2)).toBe(70 + TOAST_PEEK + 12);
  });
});

function makeAttrs(
  toasts: UiNotificationEntry[],
  overrides: Partial<ToastLayerAttrs> = {},
): ToastLayerAttrs {
  return {
    toasts,
    isReconnecting: false,
    onDismiss: () => undefined,
    onReview: () => undefined,
    ...overrides,
  };
}

function renderLayer(attrs: ToastLayerAttrs): AnyVnode | null {
  const instance = ToastLayer() as unknown as m.Component;
  const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
  return (instance.view as unknown as (v: m.Vnode) => AnyVnode | null).call(
    instance,
    vnode,
  );
}

describe("ToastLayer", () => {
  it("renders nothing without live toasts, e.g. while the feed overlay is open (caller passes [] then)", () => {
    expect(renderLayer(makeAttrs([]))).toBeNull();
    expect(renderLayer(makeAttrs([entry("n1")]))).not.toBeNull();
  });

  it("stacks collapsed slivers behind the front card and folds everything but the front into 'N more'", () => {
    const ids = ["n1", "n2", "n3", "n4", "n5"];
    const root = renderLayer(makeAttrs(ids.map((id) => entry(id)))) as AnyVnode;
    expect(allText(root)).toContain("4 more");
    // The stack items carry the collapsed styles (front card untransformed,
    // peeked ones scaled); styles land on the stack-item component vnodes.
    const styles = collectVnodes(root)
      .map(
        (vnode) => attrsOf(vnode).style as Record<string, string> | undefined,
      )
      .filter((style) => style !== undefined && style.transform !== undefined);
    expect(styles[0]?.transform).toBe("translateY(0)");
    expect(styles[1]?.transform).toBe("scale(0.97)");
    // The overflow line rides at the computed offset for the estimate height.
    const moreStyle = styles.at(-1);
    expect(moreStyle?.transform).toBe(
      `translateY(${toastMoreTopPx(TOAST_EST_HEIGHT, ids.length)}px)`,
    );
  });

  it("keeps every stack slot's gap hit-testable, so moving between open cards can't fall through to the page behind and flap the stack shut", () => {
    const ids = ["n1", "n2", "n3"];
    const root = renderLayer(makeAttrs(ids.map((id) => entry(id)))) as AnyVnode;
    const itemVnodes = collectVnodes(root).filter(
      (vnode) => vnode.tag === ToastStackItem,
    );
    expect(itemVnodes.length).toBe(ids.length);
    const wrappers = itemVnodes.map(renderComponentVnode);
    for (const wrapper of wrappers) {
      expect(classTokensOf(wrapper)).toContain("pointer-events-auto");
    }
    // A collapsed peeking slot (not the front) still forces itself
    // unclickable via its own inline style, which wins over the class.
    expect(
      (attrsOf(wrappers[1]).style as Record<string, string>)[
        "pointer-events"
      ],
    ).toBe("none");
    // The front card carries no such override: the class stands.
    expect(
      (attrsOf(wrappers[0]).style as Record<string, string>)[
        "pointer-events"
      ],
    ).toBeUndefined();
  });

  it("collapses again after the stack empties mid-hover (mouseleave never fires on removal)", () => {
    const ids = ["n1", "n2"];
    let live = [...ids];
    const entries = ids.map((id) => entry(id));
    const instance = ToastLayer() as unknown as m.Component;
    const render = (): AnyVnode | null => {
      const attrs = makeAttrs(entries.filter((e) => live.includes(e.id)));
      const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
      return (instance.view as unknown as (v: m.Vnode) => AnyVnode | null).call(
        instance,
        vnode,
      );
    };
    const transformsOf = (root: AnyVnode): (string | undefined)[] =>
      collectVnodes(root)
        .map(
          (vnode) => attrsOf(vnode).style as Record<string, string> | undefined,
        )
        .filter((style) => style?.transform !== undefined)
        .map((style) => style?.transform);
    // Hovering fans the stack open: the second card is translated down.
    const collapsed = render() as AnyVnode;
    (attrsOf(collapsed).onmouseenter as () => void)();
    const open = render() as AnyVnode;
    expect(transformsOf(open)[1]).toBe(
      `translateY(${TOAST_EST_HEIGHT + TOAST_EXPAND_GAP}px)`,
    );
    // Every toast retires while still hovered; the layer unmounts without a
    // mouseleave. The next batch must start collapsed, not pre-fanned-open.
    live = [];
    expect(render()).toBeNull();
    live = [...ids];
    const next = render() as AnyVnode;
    expect(transformsOf(next)[1]).toBe("scale(0.97)");
  });

  it("starts below the Reconnecting chip when it is visible", () => {
    const low = renderLayer(makeAttrs([entry("n1")])) as AnyVnode;
    expect(classesOf(low)).toContain("top-[42px]");
    const dropped = renderLayer(
      makeAttrs([entry("n1")], { isReconnecting: true }),
    ) as AnyVnode;
    expect(classesOf(dropped)).toContain("top-[74px]");
  });

  it("renders a plain list under prefers-reduced-motion", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const ids = ["n1", "n2", "n3", "n4", "n5"];
    const root = renderLayer(makeAttrs(ids.map((id) => entry(id)))) as AnyVnode;
    // No choreography: no overflow line, no transforms -- a flex column.
    expect(allText(root)).not.toContain("more");
    expect(classesOf(root)).toContain("flex-col");
  });

  it("passes isPaused down to every card while the pointer hovers the stack", () => {
    const ids = ["n1", "n2"];
    const instance = ToastLayer() as unknown as m.Component;
    const render = (): AnyVnode | null => {
      const attrs = makeAttrs(ids.map((id) => entry(id)));
      const vnode = m(instance, attrs as unknown as m.Attributes) as m.Vnode;
      return (instance.view as unknown as (v: m.Vnode) => AnyVnode | null).call(
        instance,
        vnode,
      );
    };
    const pausedFlagsOf = (root: AnyVnode): boolean[] =>
      collectVnodes(root)
        .filter((vnode) => "isPaused" in attrsOf(vnode))
        .map((vnode) => attrsOf(vnode).isPaused as boolean);
    const before = render() as AnyVnode;
    expect(pausedFlagsOf(before)).toEqual([false, false]);
    (attrsOf(before).onmouseenter as () => void)();
    const hovered = render() as AnyVnode;
    expect(pausedFlagsOf(hovered)).toEqual([true, true]);
    (attrsOf(hovered).onmouseleave as () => void)();
    const after = render() as AnyVnode;
    expect(pausedFlagsOf(after)).toEqual([false, false]);
  });
});

interface RenderCardOptions {
  isReducedMotion?: boolean;
  isPaused?: boolean;
  onReview?: (workspaceAgentId: string, requestId: string) => void;
}

/** Mounts a ToastCard (paused if `options.isPaused`) and returns a
 * `setPaused` driver that replays the real mithril lifecycle for every call
 * after the mount: onupdate fires with the new attrs. */
function mountCard(
  card: UiNotificationEntry,
  onDismiss: () => void,
  options: RenderCardOptions = {},
): { root: AnyVnode; setPaused: (isPaused: boolean) => AnyVnode } {
  const instance = ToastCard() as unknown as m.Component;
  let isMounted = false;
  const setPaused = (isPaused: boolean): AnyVnode => {
    const vnode = m(instance, {
      entry: card,
      isReducedMotion: options.isReducedMotion ?? false,
      isPaused,
      onDismiss,
      onReview: options.onReview ?? (() => undefined),
    } as unknown as m.Attributes) as m.Vnode;
    const rendered = (
      instance.view as unknown as (v: m.Vnode) => AnyVnode
    ).call(instance, vnode);
    if (!isMounted) {
      isMounted = true;
      (instance.oncreate as unknown as (v: m.Vnode) => void)(vnode);
    } else {
      (instance.onupdate as unknown as (v: m.Vnode) => void)(vnode);
    }
    return rendered;
  };
  return { root: setPaused(options.isPaused ?? false), setPaused };
}

describe("ToastCard", () => {
  it("is a status card whose whole body reviews the request and dismisses the flash", () => {
    let dismissed = 0;
    const reviewed: [string, string][] = [];
    const { root } = mountCard(entry("n1"), () => (dismissed += 1), {
      isReducedMotion: true,
      onReview: (workspaceAgentId, requestId) =>
        reviewed.push([workspaceAgentId, requestId]),
    });
    expect(attrsOf(root).role).toBe("status");
    // The review gesture is a real button (keyboard/screen-reader
    // accessible, like NotificationsPage.ts's feedRow), not the div itself.
    const reviewButton = collectVnodes(root).find(
      (vnode) => vnode.tag === "button" && attrsOf(vnode)["aria-label"] !== "Dismiss",
    );
    expect(reviewButton).toBeDefined();
    (attrsOf(reviewButton as AnyVnode).onclick as () => void)();
    expect(reviewed).toEqual([["agent-aa11", "req-n1"]]);
    expect(dismissed).toBe(1);
  });

  it("gives the corner X a label and keeps its click from also reviewing", () => {
    let dismissed = 0;
    let reviewed = 0;
    const { root } = mountCard(entry("n1"), () => (dismissed += 1), {
      isReducedMotion: true,
      onReview: () => (reviewed += 1),
    });
    const dismissButton = collectVnodes(root).find(
      (vnode) => attrsOf(vnode)["aria-label"] === "Dismiss",
    );
    expect(dismissButton).toBeDefined();
    let propagationStopped = 0;
    (attrsOf(dismissButton as AnyVnode).onclick as (event: unknown) => void)({
      stopPropagation: () => (propagationStopped += 1),
    });
    expect(propagationStopped).toBe(1);
    expect(dismissed).toBe(1);
    expect(reviewed).toBe(0);
  });

  it("does not auto-dismiss while the pointer hovers the stack, and resumes with the time that was left", () => {
    vi.useFakeTimers();
    try {
      let dismissed = 0;
      const { setPaused } = mountCard(entry("n1"), () => (dismissed += 1), {
        isReducedMotion: true,
      });
      // Mounted unpaused (constructor call): the TOAST_MS countdown starts.
      vi.advanceTimersByTime(TOAST_MS - 500);
      expect(dismissed).toBe(0);
      setPaused(true); // onupdate: hovered -- freeze with ~500ms banked.
      vi.advanceTimersByTime(10_000); // Far past TOAST_MS; paused, so no-op.
      expect(dismissed).toBe(0);
      setPaused(false); // onupdate: hover ends -- resume with the ~500ms left.
      vi.advanceTimersByTime(499);
      expect(dismissed).toBe(0);
      vi.advanceTimersByTime(2);
      expect(dismissed).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("starts already paused when mounted under a pointer already hovering", () => {
    vi.useFakeTimers();
    try {
      let dismissed = 0;
      const { setPaused } = mountCard(entry("n1"), () => (dismissed += 1), {
        isReducedMotion: true,
        isPaused: true, // mounted already hovered
      });
      vi.advanceTimersByTime(TOAST_MS + 10_000);
      expect(dismissed).toBe(0);
      setPaused(false); // hover ends: the full TOAST_MS starts fresh
      vi.advanceTimersByTime(TOAST_MS - 1);
      expect(dismissed).toBe(0);
      vi.advanceTimersByTime(1);
      expect(dismissed).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels the entrance rAF chain on an early close, so it can't flip the card back to 'shown' mid-exit", () => {
    // Regression guard: a corner-X or review click landing before the
    // double-rAF entrance chain (oncreate) resolves used to leave that chain
    // pending -- its callback would still fire afterward and set
    // isShown = true, flickering the card back to "shown" mid-exit right
    // before onDismiss actually removes it.
    const rafIds: number[] = [];
    const cancelledIds: number[] = [];
    let nextId = 1;
    vi.stubGlobal("requestAnimationFrame", () => {
      const id = nextId++;
      rafIds.push(id);
      return id;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      cancelledIds.push(id);
    });
    // close() calls m.redraw(); this harness never m.mount()s, so stub it.
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);

    // isReducedMotion: false (the default) -- the rAF entrance chain only
    // runs then; oncreate schedules its first rAF (raf1) here.
    const { root } = mountCard(entry("n1"), () => undefined);
    expect(rafIds).toHaveLength(1);

    const dismissButton = collectVnodes(root).find(
      (vnode) => attrsOf(vnode)["aria-label"] === "Dismiss",
    );
    (attrsOf(dismissButton as AnyVnode).onclick as (event: unknown) => void)({
      stopPropagation: () => undefined,
    });

    expect(cancelledIds).toEqual(expect.arrayContaining([rafIds[0]]));
  });
});
