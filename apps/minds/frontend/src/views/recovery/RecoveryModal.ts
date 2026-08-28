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
  /** Whether the shell raised this card rather than the user. Only an
   * auto-raised card leaves on its own when the machine comes back (the shell
   * drops it); a card the user opened stays until they close it. */
  isAutoRaised: boolean;
}

export function RecoveryModal(): m.Component<RecoveryModalAttrs> {
  let model: RecoveryModel | null = null;

  function bindModel(workspaceAnyId: string): void {
    model?.stop();
    model = new RecoveryModel(workspaceAnyId, browserLifecycleDeps(() => m.redraw()));
    void model.load();
  }

  // Escape is the shell's (ShellState.handleEscape), which closes this card
  // via closeOpenRecoveryModal. A listener here would have to know what is
  // stacked above it to stay out of the way.
  return {
    oninit(vnode) {
      bindModel(vnode.attrs.workspaceAnyId);
    },
    // The shell renders this at a fixed position in an unkeyed list, so moving
    // between two machines that are both recovery_failed keeps this instance --
    // and without a rebind it would keep showing, and restarting, the machine
    // the user just left.
    onbeforeupdate(vnode) {
      if (model !== null && vnode.attrs.workspaceAnyId !== model.workspaceAnyId) {
        bindModel(vnode.attrs.workspaceAnyId);
      }
      return true;
    },
    onremove() {
      model?.stop();
      model = null;
    },
    view(vnode) {
      if (model === null) return null;
      const { onClose, isAutoRaised } = vnode.attrs;
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
            : m(RecoveryPanel, {
                panelId: "recovery-modal-panel",
                model,
                onClose,
                isSelfDismissing: isAutoRaised,
              }),
      );
    },
  };
}
