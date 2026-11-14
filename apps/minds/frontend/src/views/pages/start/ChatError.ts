// A failure inside the onboarding chat, drawn with the chat's own pieces
// rather than the tinted notice the rest of the app shows.
//
// What it shares with every other error: the mark that leads it, the copy
// markup its sentence is written in, the raw output behind See details, the
// rule that picks its leading button, and the Report a bug that carries the
// error into the report form. What it does not share is the shape -- here
// the sentence is an agent turn and the fixes are the user's answer row, so a
// failed create reads as the next thing said rather than as a box dropped
// into the conversation.

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
import { AGENT_TURN_CLASS, answerRow, linkButton } from "./transcript";
import type { AnswerButton } from "./transcript";

export interface ChatErrorAttrs {
  /** Unique on the page; the turn, its See details toggle (`<id>-details`)
   * and the panel that toggle opens (`<id>-details-panel`) are named from it,
   * the same way an ErrorNotice names its own. */
  id: string;
  error: UserFacingError;
  tone?: ErrorTone;
  /** The fixes, then any way out (`isDismissal`), which joins Report a bug
   * among the row's quiet links rather than standing as an answer. */
  actions?: readonly ErrorAction[];
  isBugReportOffered?: boolean;
  /** The id the turn's sentence carries, for whatever reads it from outside
   * the page (the Electron e2e runner polls `#error-message`). */
  textId?: string;
  workspaceAgentId?: string | null;
}

export function ChatError(): m.Component<ChatErrorAttrs> {
  let isDetailsOpen = false;
  return {
    view(vnode) {
      const {
        id,
        error,
        tone = "error",
        actions = [],
        isBugReportOffered = true,
        textId,
        workspaceAgentId = null,
      } = vnode.attrs;
      const detailsId = `${id}-details`;
      const fixes = actions.filter((action) => action.isDismissal !== true);
      const dismissals = actions.filter(
        (action) => action.isDismissal === true,
      );
      // The emphasized answer is the leading fix the user can actually give:
      // a disabled one never carries it, and neither does one that only
      // confirms something the sentence asks them to do first.
      const leadIndex = fixes.findIndex((action) => action.isDisabled !== true);
      const isLeadEmphasized =
        leadIndex !== -1 &&
        !isFollowUpLed(error.message, fixes[leadIndex]?.label);
      const answers: AnswerButton[] = fixes.map((action, index) => ({
        id: action.id ?? `${id}-fix-${index}`,
        label: action.label,
        isEmphasized: index === leadIndex && isLeadEmphasized,
        isDisabled: action.isDisabled,
        title: action.tooltip,
        onPress: action.onclick,
      }));
      const quiet: AnswerButton[] = dismissals.map((action, index) => ({
        id: action.id ?? `${id}-dismissal-${index}`,
        label: action.label,
        isEmphasized: false,
        isDisabled: action.isDisabled,
        title: action.tooltip,
        onPress: action.onclick,
      }));
      return [
        m(
          "div",
          { key: `${id}-turn`, class: `mt-10 ${AGENT_TURN_CLASS} first:mt-0` },
          [
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
                          class:
                            "cursor-pointer border-0 bg-transparent p-0 text-tertiary " +
                            "underline hover:text-primary not-italic font-normal",
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
              ? m(
                  "div",
                  { class: MARK_INDENT_CLASS },
                  detailsPanel(detailsId, error.details),
                )
              : null,
          ],
        ),
        answers.length === 0 && quiet.length === 0 && !isBugReportOffered
          ? null
          : answerRow({
              key: `${id}-answers`,
              delayMs: 0,
              isInstant: true,
              buttons: answers,
              minorButtons: quiet,
              aside: isBugReportOffered
                ? {
                    label: "Report a bug",
                    onPress: () =>
                      getAppContext().shell.openHelpForError(
                        error,
                        workspaceAgentId,
                      ),
                  }
                : undefined,
            }),
      ];
    },
  };
}

/** The quiet link a chat turn offers beside an error it words itself. */
export function reportBugLink(
  error: UserFacingError,
  workspaceAgentId: string | null = null,
): m.Children {
  return linkButton("Report a bug", () =>
    getAppContext().shell.openHelpForError(error, workspaceAgentId),
  );
}
