import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnyVnode } from "../../testing";
import { allText, attrsOf, collectVnodes } from "../../testing";
import { ImageLightbox } from "./ImageLightbox";
import type { ImageLightboxAttrs } from "./ImageLightbox";

function makeLightbox(onClose: () => void): {
  component: m.Component<ImageLightboxAttrs>;
  render: () => AnyVnode;
} {
  const vnode = {
    attrs: {
      imageUrl: "https://example.com/recording.gif",
      title: "To-do list",
      onClose,
    },
  } as m.Vnode<ImageLightboxAttrs>;
  const component = ImageLightbox(vnode) as m.Component<ImageLightboxAttrs>;
  return {
    component,
    render: () =>
      (component.view as (v: m.Vnode<ImageLightboxAttrs>) => m.Vnode).call(
        component,
        vnode,
      ) as AnyVnode,
  };
}

describe("ImageLightbox", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("draws the image enlarged in a dialog with its title and a close control", () => {
    const { render } = makeLightbox(() => undefined);

    const overlay = render();

    expect(attrsOf(overlay).role).toBe("dialog");
    expect(attrsOf(overlay)["aria-modal"]).toBe("true");
    expect(attrsOf(overlay)["aria-label"]).toBe("To-do list");
    expect(allText(overlay)).toContain("To-do list");
    const image = collectVnodes(overlay).find((node) => node.tag === "img") as AnyVnode;
    expect(attrsOf(image).src).toBe("https://example.com/recording.gif");
    expect(attrsOf(image).alt).toBe("To-do list");
    const close = collectVnodes(overlay).find(
      (node) => attrsOf(node)["aria-label"] === "Close image viewer",
    ) as AnyVnode;
    expect(close.tag).toBe("button");
  });

  it("closes on the close control and on a click that lands on the backdrop itself, not one on the image", () => {
    let closedCount = 0;
    const { render } = makeLightbox(() => {
      closedCount += 1;
    });
    const overlay = render();

    const close = collectVnodes(overlay).find(
      (node) => attrsOf(node)["aria-label"] === "Close image viewer",
    ) as AnyVnode;
    (attrsOf(close).onclick as () => void)();
    expect(closedCount).toBe(1);

    const backdrop = {};
    (attrsOf(overlay).onclick as (event: unknown) => void)({
      target: backdrop,
      currentTarget: backdrop,
    });
    expect(closedCount).toBe(2);
    (attrsOf(overlay).onclick as (event: unknown) => void)({
      target: {},
      currentTarget: backdrop,
    });
    expect(closedCount).toBe(2);
  });

  it("closes on Escape through a document listener it removes with itself", () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const listenersByType = new Map<string, (event: unknown) => void>();
    vi.stubGlobal("document", {
      addEventListener: (type: string, listener: (event: unknown) => void) => {
        listenersByType.set(type, listener);
      },
      removeEventListener: (type: string) => {
        listenersByType.delete(type);
      },
    });
    let closedCount = 0;
    const { component, render } = makeLightbox(() => {
      closedCount += 1;
    });
    render();
    (component.oncreate as () => void).call(component);
    const onKeydown = listenersByType.get("keydown");
    expect(onKeydown).toBeDefined();

    onKeydown?.({ key: "a" });
    expect(closedCount).toBe(0);
    onKeydown?.({ key: "Escape" });
    expect(closedCount).toBe(1);

    (component.onremove as () => void).call(component);
    expect(listenersByType.has("keydown")).toBe(false);
  });
});
