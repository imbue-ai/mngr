// The styled error dialog, drawn by the Shell for the queue any surface raises
// through showErrorDialog. It takes the place of a native alert: a title that
// names what failed and carries the mark, our sentence under it saying why and
// what to do, the details behind a See details that trails that sentence, then
// the buttons that fix it and Report a bug.
//
// There is no Close button: the x dismisses it, and a dialog with nothing to
// press has no button row at all.

import m from "mithril";
import {
  ErrorDialogQueue,
  errorDialogs,
  FALLBACK_DIALOG_TITLE,
} from "../../models/errorDialog";
import {
  detailsPanel,
  errorActionButtons,
  errorButtonRow,
  ErrorDetailsToggle,
} from "./ErrorDisplay";
import { ErrorTitle, richErrorText } from "./errorDesign";
import { DialogCloseButton, Modal } from "./Modal";

interface ErrorDialogLayerAttrs {
  queue?: ErrorDialogQueue;
}

const DETAILS_ID = "error-dialog-details";

export function ErrorDialogLayer(): m.Component<ErrorDialogLayerAttrs> {
  let attached: ErrorDialogQueue | null = null;
  let openForId: number | null = null;
  return {
    oncreate(vnode) {
      attached = vnode.attrs.queue ?? errorDialogs;
      attached.attachHost();
    },
    onremove() {
      attached?.detachHost();
      attached = null;
    },
    view(vnode) {
      const queue = vnode.attrs.queue ?? errorDialogs;
      const request = queue.current;
      if (request === null) return null;
      const close = (): void => queue.dismiss();
      // Each dialog opens with its details shut: the open one is remembered by
      // the request it belongs to, so the next in the queue starts closed.
      const isOpen = openForId === request.id;
      // A stacking layer above every other shell surface, the sidebar switcher
      // included: an error can be raised from any of them.
      return m(
        "div",
        { class: "fixed z-[210]" },
        m(
          Modal,
          { isOpen: true, onClose: close, size: "md", cardExtra: "relative" },
          m(
            "div",
            {
              key: request.id,
              role: "alertdialog",
              "aria-modal": "true",
            },
            [
              m(DialogCloseButton, { onClose: close }),
              m(
                ErrorTitle,
                { typeClass: "type-heading", extra: "mb-2 pr-8" },
                request.title ?? FALLBACK_DIALOG_TITLE,
              ),
              m("p", { class: "type-body text-primary m-0" }, [
                richErrorText(request.error.message),
                request.error.details === ""
                  ? null
                  : [
                      " ",
                      m(ErrorDetailsToggle, {
                        id: DETAILS_ID,
                        isOpen,
                        onToggle: () => {
                          openForId = isOpen ? null : (request.id ?? null);
                        },
                      }),
                    ],
              ]),
              isOpen && request.error.details !== ""
                ? detailsPanel(DETAILS_ID, request.error.details)
                : null,
              errorButtonRow(
                errorActionButtons(request.error, {
                  actions: request.actions,
                  isBugReportOffered: request.isBugReportOffered,
                  workspaceAgentId: request.workspaceAgentId,
                  beforeAction: close,
                  size: "md",
                }),
              ),
            ],
          ),
        ),
      );
    },
  };
}
