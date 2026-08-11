// The recovery card as a modal over a machine that is still on screen.
//
// It carries its own backdrop rather than the shared Modal so the dimming
// starts BELOW the titlebar, exactly as the options overlay does: the home
// button, switcher and inbox stay reachable while the card is up, so a
// machine that will not come back can never trap the window. The other
// modals keep their full-window backdrops; nothing here generalizes to them.
//
// Always dismissible. It is only ever used with something behind it that
// still reports the machine -- the band over a painted surface -- so a
// dismissal always lands somewhere that can bring it back.

import m from "mithril";
import { DialogCloseButton } from "../components/Modal";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { RecoveryPanel } from "./RecoveryCard";
import { browserLifecycleDeps, RecoveryModel } from "../../models/backups";

export interface RecoveryModalAttrs {
  workspaceAnyId: string;
  onClose: () => void;
  /** Whether the switcher popover is open above this card (it out-z-indexes
   * the card). An Escape belongs to it then, not to the card. */
  isSidebarAbove: boolean;
}

export function RecoveryModal(): m.Component<RecoveryModalAttrs> {
  let model: RecoveryModel | null = null;
  let onKeyDown: ((event: KeyboardEvent) => void) | null = null;
  // What the document listener below reads, refreshed on every draw. It cannot
  // close over a vnode: mithril builds a fresh one per update and leaves the
  // create-time one untouched, so the listener would keep calling the first
  // onClose it ever saw -- and that one names the machine this instance was
  // mounted for, not the one it has since been rebound to.
  let currentAttrs: RecoveryModalAttrs | null = null;

  function bindModel(workspaceAnyId: string): void {
    model?.stop();
    model = new RecoveryModel(workspaceAnyId, browserLifecycleDeps(() => m.redraw()));
    void model.load();
  }

  return {
    oninit(vnode) {
      currentAttrs = vnode.attrs;
      bindModel(vnode.attrs.workspaceAnyId);
    },
    // The shell renders this at a fixed position in an unkeyed list, so moving
    // between two machines that both have a card up keeps this instance -- and
    // without a rebind it would keep showing, and restarting, the machine the
    // user just left.
    onbeforeupdate(vnode) {
      currentAttrs = vnode.attrs;
      if (model !== null && vnode.attrs.workspaceAnyId !== model.workspaceAnyId) {
        bindModel(vnode.attrs.workspaceAnyId);
      }
      return true;
    },
    oncreate() {
      onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== "Escape") return;
        // Capture phase, and stopImmediatePropagation within it. The options
        // overlay listens for Escape on this same document node, and among
        // bubble listeners on one node the key goes to whichever registered
        // first -- so to whichever surface mounted first, not to the one on
        // top. The card can open over an already-open overlay, so on the
        // bubble phase one Escape dismisses both. Capture runs ahead of every
        // bubble listener, so the topmost surface is the one that closes.
        event.stopImmediatePropagation();
        // With the switcher popover above the card, the key is the popover's:
        // the card stays, and the propagation stop above keeps the overlays
        // beneath from acting on a keypress that was never theirs. (Electron's
        // escape-pressed forward is what closes the popover; its redraw lands
        // before any next keypress, so the card takes the one after.)
        if (currentAttrs?.isSidebarAbove === true) return;
        currentAttrs?.onClose();
        m.redraw();
      };
      document.addEventListener("keydown", onKeyDown, true);
    },
    onremove() {
      if (onKeyDown !== null) document.removeEventListener("keydown", onKeyDown, true);
      onKeyDown = null;
      model?.stop();
      model = null;
    },
    view(vnode) {
      if (model === null) return null;
      const { onClose } = vnode.attrs;
      return m(
        "div#recovery-modal-backdrop",
        {
          class:
            "fixed left-0 right-0 top-[38px] bottom-0 z-[110] bg-black/20 " +
            "flex items-start justify-center p-4 pt-10",
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) onClose();
          },
        },
        model.loadError !== null
          ? m(
              "div#recovery-modal-panel",
              {
                class:
                  "relative w-[560px] max-w-full flex flex-col gap-4 rounded-[12px] " +
                  "border border-subtle bg-surface-primary shadow-overlay px-6 py-5",
              },
              [
                // The X the loaded panel carries: this modal is always
                // dismissible, and an error state must not be the one place
                // that shows no way out.
                m(DialogCloseButton, { onClose }),
                m("div", { class: "type-heading pr-10" }, "Machine recovery"),
                m(Notice, { variant: "error" }, model.loadError),
              ],
            )
          : model.info === null
            ? m("div", { class: "flex items-center gap-2 type-body text-primary" }, [
                m(Spinner, { size: "sm" }),
                "Loading...",
              ])
            : m(RecoveryPanel, { panelId: "recovery-modal-panel", model, onClose }),
      );
    },
  };
}
