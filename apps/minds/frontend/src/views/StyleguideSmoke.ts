// A deliberately tiny component the dev styleguide mounts through the full
// mount protocol. It proves the toolchain end to end: the IIFE bundle loaded
// the namespace, the boot island parsed, mithril mounted synchronously, event
// handlers trigger a redraw, and Tailwind generated utilities written only in
// frontend/src (app.css declares this tree as an @source).
import m from "mithril";

import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";

interface StyleguideSmokeAttrs {
  message: string;
}

export function StyleguideSmoke(): m.Component<StyleguideSmokeAttrs> {
  let redrawCount = 0;
  return {
    view(vnode) {
      return m("div", { class: "minds-card flex items-center gap-3 px-4 py-3" }, [
        m("span", { class: "flex-1 type-body text-primary italic" }, vnode.attrs.message),
        m(
          "button",
          {
            type: "button",
            class:
              "px-2 py-1 type-helper rounded-md border border-default bg-surface-primary hover:bg-fill-hover text-primary cursor-pointer",
            onclick: () => {
              redrawCount += 1;
            },
          },
          redrawCount === 0 ? "Click to redraw" : `Redrawn ${redrawCount}×`,
        ),
      ]);
    },
  };
}

export function mountStyleguideSmoke(target: Element | null): void {
  const el = requireElement(target, "styleguide smoke container");
  const bootState = readBootState() as { styleguide_smoke?: { message?: unknown } };
  const message = bootState.styleguide_smoke?.message;
  if (typeof message !== "string") {
    throw new MindsUIError("boot state is missing styleguide_smoke.message");
  }
  mountWithTeardown(el, { view: () => m(StyleguideSmoke, { message }) });
}
