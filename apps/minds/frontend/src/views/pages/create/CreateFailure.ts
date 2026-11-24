// A create that did not finish, on the creation page: the sentence, its raw text behind See details, the ways
// forward as buttons, and the quiet ways out beside Report a bug.
//
// What it shares with every other error: the mark that leads it, the copy markup its sentence is written in, the
// raw output behind See details, the rule that picks its leading button, and the Report a bug that carries the
// error into the report form. What it does not share is its dress: the creation page is a first-run page, so the
// buttons and links are that page's.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type { ErrorAction, UserFacingError } from "../../../models/errors";
import { detailsPanel } from "../../components/ErrorDisplay";
import {
  type ErrorTone,
  isFollowUpLed,
  markedLine,
  MARK_INDENT_CLASS,
  richErrorText,
} from "../../components/errorDesign";

export const REPORT_BUG_LABEL = "Report a bug";

const LINK_RESET_CLASS = "type-body border-0 bg-transparent p-0";
const LINK_CLASS = `first-run-link first-run-link--plain ${LINK_RESET_CLASS}`;
// Tailwind's `underline` cannot do this: `first-run-link`'s own decoration is unlayered, and beats a utility.
const DETAILS_LINK_CLASS = `first-run-link first-run-link--inline ${LINK_RESET_CLASS}`;

export interface CreateFailureAttrs {
  /** Unique on the page; the sentence, its See details toggle (`<id>-details`)
   * and the panel that toggle opens (`<id>-details-panel`) are named from it,
   * the same way an ErrorNotice names its own. */
  id: string;
  error: UserFacingError;
  tone?: ErrorTone;
  /** The fixes, then any way out (`isDismissal`), which joins Report a bug
   * among the quiet links rather than standing as a button. */
  actions?: readonly ErrorAction[];
  isBugReportOffered?: boolean;
  /** The id the sentence carries, for whatever reads it from outside
   * the page (the Electron e2e runner polls `#error-message`). */
  textId?: string;
}

function quietLink(action: ErrorAction, id: string): m.Children {
  return m(
    "button",
    {
      key: id,
      type: "button",
      "data-answer": id,
      class: LINK_CLASS,
      disabled: action.isDisabled ?? false,
      title: action.tooltip,
      onclick: action.onclick,
    },
    action.label,
  );
}

export function CreateFailure(): m.Component<CreateFailureAttrs> {
  let isDetailsOpen = false;
  return {
    view(vnode) {
      const { id, error, tone = "error", actions = [], isBugReportOffered = true, textId } = vnode.attrs;
      const detailsId = `${id}-details`;
      const fixes = actions.filter((action) => action.isDismissal !== true);
      const dismissals = actions.filter((action) => action.isDismissal === true);
      // The filled button is the leading fix the user can actually take: a
      // disabled one never carries it, and neither does one that only
      // confirms something the sentence asks them to do first.
      const leadIndex = fixes.findIndex((action) => action.isDisabled !== true);
      const isLeadEmphasized = leadIndex !== -1 && !isFollowUpLed(error.message, fixes[leadIndex]?.label);
      const hasQuietLinks = dismissals.length > 0 || isBugReportOffered;
      return m("div", { class: "flex w-full flex-col text-left" }, [
        markedLine(
          { tone },
          m("p", { id, class: "m-0" }, [
            m("span", { id: textId }, richErrorText(error.message)),
            error.details === ""
              ? null
              : [
                  " ",
                  m(
                    "button",
                    {
                      id: detailsId,
                      type: "button",
                      "aria-expanded": isDetailsOpen ? "true" : "false",
                      "aria-controls": `${detailsId}-panel`,
                      class: DETAILS_LINK_CLASS,
                      onclick: () => {
                        isDetailsOpen = !isDetailsOpen;
                      },
                    },
                    "See details",
                  ),
                ],
          ]),
        ),
        isDetailsOpen && error.details !== ""
          ? m("div", { class: MARK_INDENT_CLASS }, detailsPanel(detailsId, error.details))
          : null,
        fixes.length === 0 && !hasQuietLinks
          ? null
          : m("div", { class: "mt-8 flex flex-wrap items-center justify-between gap-x-6 gap-y-4" }, [
              // Every link is keyed, so an absent Report a bug is left out rather than drawn as a hole: Mithril
              // refuses keyed vnodes beside one.
              m("div", { class: "flex flex-wrap items-center gap-x-6 gap-y-2" }, [
                ...(isBugReportOffered
                  ? [
                      m(
                        "button",
                        {
                          key: `${id}-report`,
                          type: "button",
                          "data-aside": "",
                          class: LINK_CLASS,
                          onclick: () => getAppContext().shell.openHelpForError(error, null),
                        },
                        REPORT_BUG_LABEL,
                      ),
                    ]
                  : []),
                ...dismissals.map((action, index) => quietLink(action, action.id ?? `${id}-dismissal-${index}`)),
              ]),
              m(
                "div",
                { class: "flex flex-wrap items-center gap-3" },
                fixes.map((action, index) => {
                  const fixId = action.id ?? `${id}-fix-${index}`;
                  const isFilled = index === leadIndex && isLeadEmphasized;
                  return m(
                    "button",
                    {
                      key: fixId,
                      type: "button",
                      "data-answer": fixId,
                      class:
                        "first-run-button first-run-button--compact type-label" +
                        (isFilled ? "" : " first-run-button--secondary"),
                      disabled: action.isDisabled ?? false,
                      title: action.tooltip,
                      onclick: action.onclick,
                    },
                    action.label,
                  );
                }),
              ),
            ]),
      ]);
    },
  };
}
