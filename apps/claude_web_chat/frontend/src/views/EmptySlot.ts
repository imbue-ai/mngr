import m from "mithril";
import { isSlotClaimed, getSlotRenderCallback } from "../slots";
import { runHook } from "../hooks";

/**
 * An empty extension point that plugins can claim and fill with their own DOM.
 *
 * When claimed, mithril skips child reconciliation (via onbeforeupdate returning
 * false) so that plugin-injected children survive redraws.
 *
 * If the slot was claimed with a render callback, the callback is automatically
 * invoked in oncreate so plugin content is restored after DOM recreation.
 */
export function EmptySlot(): m.Component<{ name: string; class?: string }> {
  return {
    oncreate(vnode: m.VnodeDOM<{ name: string; class?: string }>) {
      const slotName = vnode.attrs.name;
      if (isSlotClaimed(slotName)) {
        const renderCallback = getSlotRenderCallback(slotName);
        if (renderCallback) {
          renderCallback(vnode.dom as HTMLElement);
        }
        runHook("slot_rendered", { slotName, container: vnode.dom as HTMLElement });
      }
    },

    onbeforeupdate(vnode: m.Vnode<{ name: string; class?: string }>) {
      return !isSlotClaimed(vnode.attrs.name);
    },

    view(vnode: m.Vnode<{ name: string; class?: string }>) {
      return m("div", {
        class: vnode.attrs.class || vnode.attrs.name,
        "data-slot": vnode.attrs.name,
      });
    },
  };
}
