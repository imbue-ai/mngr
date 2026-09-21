// What a local backend needs from this machine and is not getting, with the
// command that installs it.
//
// The create form keeps every backend selectable: the user pastes the command
// into a terminal, clicks "Check again", and creates without leaving the page.
// Shown wherever a selection depends on the missing piece -- under the
// compute selector, the runtime selector, and the local preset card, and on
// the template flow's confirm step, which opens with its compute selector
// collapsed.

import m from "mithril";
import type { LocalBackendPrerequisite } from "../../../models/create";
import { Link } from "../../components/Link";
import { Notice } from "../../components/Notice";

interface PrerequisiteNoticeAttrs {
  prerequisite: LocalBackendPrerequisite;
  /** Re-probe the machine; the caller refetches the defaults. */
  onCheckAgain: () => void;
  id?: string;
}

export function PrerequisiteNotice(): m.Component<PrerequisiteNoticeAttrs> {
  let copiedCommand = "";
  return {
    view(vnode: m.Vnode<PrerequisiteNoticeAttrs>) {
      const { prerequisite, onCheckAgain, id } = vnode.attrs;
      const command = prerequisite.install_command;
      return m(Notice, { variant: "warn", extra: "mt-2", id }, [
        m("p", { class: "type-helper text-primary" }, prerequisite.summary),
        command !== ""
          ? m("div", { class: "mt-2 flex items-start gap-2" }, [
              m(
                "code",
                {
                  class:
                    "flex-1 min-w-0 type-helper font-mono bg-fill-subtle rounded-md px-2 py-1.5 " +
                    "whitespace-pre-wrap break-all select-all",
                },
                command,
              ),
              m(
                "button",
                {
                  type: "button",
                  class:
                    "shrink-0 type-helper text-tertiary hover:text-primary hover:underline cursor-pointer " +
                    "px-1 py-1.5",
                  onclick: () => {
                    const clipboard = navigator.clipboard;
                    if (!clipboard) {
                      console.error("Could not copy the install command: the clipboard is unavailable here");
                      return;
                    }
                    // The label follows the write, so a refused write never
                    // reads as a copied command.
                    void clipboard.writeText(command).then(
                      () => {
                        copiedCommand = command;
                        m.redraw();
                      },
                      (error: unknown) => {
                        console.error("Could not copy the install command to the clipboard:", error);
                      },
                    );
                  },
                },
                copiedCommand === command ? "Copied" : "Copy",
              ),
            ])
          : null,
        m("p", { class: "mt-2 type-helper text-tertiary" }, [
          m(Link, { href: prerequisite.docs_url, target: "_blank", rel: "noopener" }, "Installation guide"),
          " · ",
          m(
            "button",
            {
              type: "button",
              class: "type-helper text-tertiary hover:text-primary hover:underline cursor-pointer",
              onclick: onCheckAgain,
            },
            "Check again",
          ),
        ]),
      ]);
    },
  };
}
