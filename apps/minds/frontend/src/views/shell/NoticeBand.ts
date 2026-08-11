// The full-width strip under the titlebar: what a machine's failure looks
// like while the machine itself stays on screen.
//
// It publishes its own measured height so the workspace surface shrinks by
// exactly that much rather than being covered by it. A fixed-height strip
// would have to truncate, and the longest message here -- the one naming a
// dead discovery consumer -- is the one the user most needs to finish
// reading.

import m from "mithril";
import { Button } from "../components/Button";
import { noticeVariantClass } from "../components/Notice";
import type { ShellState } from "./shell-state";
import type { NoticePayload } from "./notice-band";

export interface NoticeBandAttrs {
  shell: ShellState;
  payload: NoticePayload;
  onAction: () => void;
}

export function NoticeBand(): m.Component<NoticeBandAttrs> {
  let observer: ResizeObserver | null = null;

  return {
    oncreate(vnode) {
      const element = vnode.dom as HTMLElement;
      // The message wraps, so the height moves with the window width as well
      // as with the message.
      observer = new ResizeObserver(() => {
        vnode.attrs.shell.setNoticeBandHeight(element.getBoundingClientRect().height);
      });
      observer.observe(element);
      vnode.attrs.shell.setNoticeBandHeight(element.getBoundingClientRect().height);
    },
    onremove(vnode) {
      observer?.disconnect();
      observer = null;
      // The surface has to grow back when the condition clears.
      vnode.attrs.shell.setNoticeBandHeight(0);
    },
    view(vnode) {
      const { payload, onAction } = vnode.attrs;
      return m(
        "div#notice-band",
        {
          // Above the workspace surface and the options backdrop, as the
          // titlebar is: this reports on the app, not on the panel in front
          // of it.
          //
          // The app surface is painted here, under the variant tint on the
          // child. What lies behind the band is the body's per-workspace
          // accent, and the tint is translucent, so without this the band's
          // colour -- and its contrast -- would be set by a colour the user
          // picked: a light accent leaves the message near 1.5:1.
          //
          // Inset and rounded to match the card it sits on top of (the 4px /
          // 12px of .workspace-surface), whose top corners go square while
          // this is up: the two read as one card with the accent bleeding
          // evenly around it, rather than a full-bleed strip overhanging a
          // rounded surface. overflow-hidden clips the tinted child to the
          // radius.
          class:
            "fixed left-[4px] right-[4px] top-[38px] z-[95] " +
            "bg-surface-primary rounded-t-xl overflow-hidden",
        },
        m(
          "div",
          { class: "flex items-center gap-3 px-4 py-2 " + noticeVariantClass(payload.variant) },
          [
            m("span", { class: "type-body flex-1 min-w-0" }, payload.message),
            payload.action !== null
              ? m(Button, { variant: "secondary", size: "md", onclick: onAction }, payload.action.label)
              : null,
          ],
        ),
      );
    },
  };
}
