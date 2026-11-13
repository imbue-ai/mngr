// The error display: a tinted box holding the mark, our sentence, the status
// and server text behind a "See details" that trails the sentence, and the
// buttons that fix it -- its own fixes first, then any way out, then "Report a
// bug", which opens the help page's report form with the details attached.
//
// The pieces are exported on their own for surfaces that draw an error in
// their own shape (a transcript turn, a card's meta line) but still owe the
// details and the report button.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { electronBridge } from "../../electron-bridge";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import { Button } from "./Button";
import type { ButtonVariant } from "./constants";
import {
  emIcon,
  isFollowUpLed,
  markedLine,
  MARK_INDENT_CLASS,
  richErrorText,
  TONE_SURFACE_CLASS,
  type ErrorTone,
} from "./errorDesign";
import { machineVerdict } from "./MachineVerdict";
import { Spinner } from "./Spinner";

export type { ErrorTone as ErrorNoticeTone };

interface ErrorDetailsToggleAttrs {
  /** Unique on the page; ties the toggle to the panel it opens. */
  id: string;
  isOpen: boolean;
  onToggle: () => void;
}

/**
 * "See details", trailing the sentence it belongs to. Plain inline text, so it
 * sits on the sentence's own baseline rather than starting a line of its own;
 * its chevron turns down when the panel under it opens.
 *
 * It draws only the trigger. Whatever it opens is the caller's, drawn under
 * the sentence so the raw output lands clear of the text it explains.
 */
export function ErrorDetailsToggle(): m.Component<ErrorDetailsToggleAttrs> {
  return {
    view(vnode) {
      const { id, isOpen, onToggle } = vnode.attrs;
      return m(
        "button",
        {
          id,
          type: "button",
          "aria-expanded": isOpen ? "true" : "false",
          "aria-controls": `${id}-panel`,
          onclick: onToggle,
          class:
            "inline whitespace-nowrap bg-transparent border-0 p-0 cursor-pointer " +
            "text-secondary hover:text-primary not-italic font-normal " +
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        },
        [
          "See details",
          emIcon(
            "chevron-right",
            "ml-[0.15em] inline-block align-[-0.2em] transition-transform duration-150" +
              (isOpen ? " rotate-90" : ""),
          ),
        ],
      );
    },
  };
}

/** The raw output a See details opens, named by its trigger. */
export function detailsPanel(id: string, details: string): m.Children {
  return m(
    "div",
    { id: `${id}-panel`, role: "region", "aria-labelledby": id },
    machineVerdict(details),
  );
}

interface ReportBugButtonAttrs {
  error: UserFacingError;
  /** The workspace the error is about, so the report can be scoped to it. */
  workspaceAgentId?: string | null;
  /** Runs before the report form opens (a dialog closing itself). */
  beforeOpen?: () => void;
  size?: "xs" | "md";
  /** The only button there: a secondary rather than a ghost, so the error is
   * not left with nothing to press. */
  isAlone?: boolean;
}

/** "Report a bug", at regular weight so its text matches the line-art glyph
 * beside it. */
export function ReportBugButton(): m.Component<ReportBugButtonAttrs> {
  return {
    view(vnode) {
      const {
        error,
        workspaceAgentId = null,
        beforeOpen,
        size = "xs",
        isAlone = false,
      } = vnode.attrs;
      return m(
        Button,
        {
          size,
          variant: isAlone ? "secondary" : "ghost",
          extra: "font-normal",
          onclick: () => {
            beforeOpen?.();
            getAppContext().shell.openHelpForError(error, workspaceAgentId);
          },
        },
        [emIcon("bug"), "Report a bug"],
      );
    },
  };
}

/** "Restart Imbue Studio", in the desktop app; elsewhere there is nothing to
 * press. */
export function restartAppActions(): ErrorAction[] {
  return electronBridge.isDesktop
    ? [
        {
          label: "Restart Imbue Studio",
          onclick: () => electronBridge.restartApp(),
        },
      ]
    : [];
}

/** "Reopen Imbue Studio", in the desktop app on macOS; elsewhere there is
 * nothing to press. The remedy for a Full Disk Access grant, which applies
 * only to the app started after it. */
export function relaunchAppActions(): ErrorAction[] {
  return electronBridge.canRelaunchApp
    ? [
        {
          label: "Reopen Imbue Studio",
          onclick: () => electronBridge.relaunchApp(),
        },
      ]
    : [];
}

/** "Update Imbue Studio", in the desktop app, opening the app's Updates
 * settings; elsewhere there is no app updater to send anyone to. The remedy
 * for a refusal of this version as too old. */
export function updateAppActions(): ErrorAction[] {
  return electronBridge.isDesktop
    ? [
        {
          label: "Update Imbue Studio",
          onclick: () =>
            m.route.set(
              "/settings",
              getAppContext().shell.overlayQuery({ section: "updates" }),
            ),
        },
      ]
    : [];
}

export interface ErrorActionRowOptions {
  actions?: readonly ErrorAction[];
  /** Off for an error that explains itself and asks for no report. */
  isBugReportOffered?: boolean;
  workspaceAgentId?: string | null;
  /** Runs before any button's own action (a dialog closing itself). */
  beforeAction?: () => void;
  /** The size every button in the row is drawn at: `xs` inside a notice,
   * `md` in a dialog, whose buttons are the dialog's own. */
  size?: "xs" | "md";
}

/** The row an error's buttons sit in; nothing when there are none. */
export function errorButtonRow(buttons: m.Children[]): m.Children {
  return buttons.length === 0
    ? null
    : m("div", { class: "flex flex-wrap items-center gap-2 mt-3" }, buttons);
}

/**
 * The error's own buttons, for the row a surface draws around them.
 *
 * The fixes come first, the leading one as the primary button -- unless the
 * copy asks the user to do something and that button only confirms it
 * (`isFollowUpLed`), in which case the ask is the next step and the button
 * waits, secondary, for it. Ways out follow as ghosts, and Report a bug last.
 */
export function errorActionButtons(
  error: UserFacingError,
  options: ErrorActionRowOptions,
): m.Children[] {
  const {
    actions = [],
    isBugReportOffered = true,
    workspaceAgentId = null,
    beforeAction,
    size = "xs",
  } = options;
  const press = (action: ErrorAction) => () => {
    beforeAction?.();
    action.onclick();
  };
  const actionButton = (action: ErrorAction, variant: ButtonVariant) =>
    m(
      Button,
      {
        id: action.id,
        size,
        variant,
        disabled: (action.isDisabled ?? false) || (action.isBusy ?? false),
        "data-tooltip": action.tooltip,
        onclick: press(action),
      },
      action.isBusy ? m(Spinner, { size: "sm" }) : action.label,
    );
  const fixes = actions.filter((action) => action.isDismissal !== true);
  const dismissals = actions.filter((action) => action.isDismissal === true);
  const leadVariant = isFollowUpLed(error.message, fixes[0]?.label)
    ? "secondary"
    : "primary";
  const buttons = [
    ...fixes.map((action, index) =>
      actionButton(
        action,
        action.isDestructive
          ? "danger"
          : index === 0
            ? leadVariant
            : "secondary",
      ),
    ),
    ...dismissals.map((action) => actionButton(action, "ghost")),
  ];
  return isBugReportOffered
    ? [
        ...buttons,
        m(ReportBugButton, {
          error,
          workspaceAgentId,
          beforeOpen: beforeAction,
          size,
          isAlone: buttons.length === 0,
        }),
      ]
    : buttons;
}

export interface ErrorNoticeAttrs extends ErrorActionRowOptions {
  /** Unique on the page; the details toggle's id is built from it. */
  id: string;
  error: UserFacingError;
  tone?: ErrorTone;
  /** Drawn in place of the sentence when it needs more than a sentence (steps
   * to follow, a list of problems). A report still carries the error's own
   * sentence. */
  content?: m.Children;
  /** The surface's own title already carries the mark (an ErrorTitle), so the
   * sentence here is flush under it rather than marked a second time. */
  isTitleMarked?: boolean;
  /** The app is still retrying on its own, so nothing has failed yet: amber,
   * with a quiet "Still trying..." under the sentence. */
  isRetrying?: boolean;
  /** Drawn under the buttons, e.g. an AskAgentFailure for askAgentActions. */
  footer?: m.Children;
  /** The surface's own buttons (Deny / Approve, Cancel / Disconnect), drawn
   * in the surface's footer below the notice rather than inside it. */
  surfaceActions?: m.Children[];
  /** Off for an error drawn inside another error's box (an AskAgentFailure
   * under that error's buttons): one failure is one box, so the inner one is
   * said on the tint already there. */
  isBoxed?: boolean;
  /** Layout classes for the wrapper (e.g. shrink-0 in a flex column). */
  extra?: string;
}

/** An error shown in place, on the surface where it happened. */
export function ErrorNotice(): m.Component<ErrorNoticeAttrs> {
  let isOpen = false;
  return {
    view(vnode) {
      const {
        id,
        error,
        tone = "error",
        content,
        isTitleMarked = false,
        isRetrying = false,
        footer,
        surfaceActions,
        isBoxed = true,
        extra = "",
      } = vnode.attrs;
      // While the app keeps retrying, nothing has failed yet: the notice reads
      // as a warning whatever tone the caller asked for, and turns red only
      // once it gives up.
      const shownTone = isRetrying ? "warn" : tone;
      const detailsId = `${id}-details`;
      const trigger =
        error.details === ""
          ? null
          : [
              " ",
              m(ErrorDetailsToggle, {
                id: detailsId,
                isOpen,
                onToggle: () => {
                  isOpen = !isOpen;
                },
              }),
            ];
      return m(
        "div",
        { id, role: "alert", class: `type-body wrap-anywhere ${extra}` },
        [
          m(
            "div",
            {
              class: isBoxed
                ? `rounded-xl px-4 py-3 ${TONE_SURFACE_CLASS[shownTone]}`
                : "mt-3",
            },
            [
              markedLine(
                { tone: shownTone, isTitleMarked },
                content === undefined
                  ? m("p", { class: "m-0" }, [
                      richErrorText(error.message),
                      trigger,
                    ])
                  : [content, trigger],
              ),
              m(
                "div",
                { class: isTitleMarked ? "" : MARK_INDENT_CLASS },
                [
                  isOpen && error.details !== ""
                    ? detailsPanel(detailsId, error.details)
                    : null,
                  isRetrying
                    ? m(
                        "div",
                        {
                          class:
                            "mt-1 flex items-center gap-[0.43em] type-helper text-tertiary",
                        },
                        [m(Spinner, { size: "sm" }), "Still trying…"],
                      )
                    : null,
                  errorButtonRow(errorActionButtons(error, vnode.attrs)),
                  footer,
                ],
              ),
            ],
          ),
          surfaceActions === undefined || surfaceActions.length === 0
            ? null
            : m(
                "div",
                {
                  class:
                    "mt-4 flex flex-wrap items-center justify-end gap-2",
                },
                surfaceActions,
              ),
        ],
      );
    },
  };
}
