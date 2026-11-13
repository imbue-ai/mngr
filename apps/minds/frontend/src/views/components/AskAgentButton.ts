// A button that asks a workspace's agent for help: it starts a chat in the
// workspace seeded with the description (POST /help/assist). A failed start
// shows its error under the button, or, for an error's action
// (askAgentActions), under that error's row through AskAgentFailure. A start
// the route refused can't succeed on a second press, so the button is then
// withdrawn and only the refusal shows. The caller owns the AskAgentModel, so
// the state survives redraws and the caller decides what follows a start.

import m from "mithril";
import type { AskAgentModel } from "../../models/assistChat";
import type { ErrorAction } from "../../models/errors";
import { Button } from "./Button";
import type { ButtonVariant } from "./constants";
import { ErrorNotice } from "./ErrorDisplay";

export interface AskAgentButtonAttrs {
  /** Unique on the page; the error display's id is built from it. */
  id: string;
  model: AskAgentModel;
  workspaceAgentId: string;
  /** What Imbue Studio asks the agent to do, in its own voice: the agent reads
   * it as the chat's first message, and it is not the user speaking. */
  description: string;
  label: string;
  variant?: ButtonVariant;
}

export function AskAgentButton(): m.Component<AskAgentButtonAttrs> {
  return {
    view(vnode) {
      const {
        id,
        model,
        workspaceAgentId,
        variant = "secondary",
      } = vnode.attrs;
      const buttons = askAgentActions(vnode.attrs).map((action) =>
        m(
          Button,
          {
            id: action.id,
            variant,
            disabled: action.isDisabled,
            onclick: action.onclick,
          },
          action.label,
        ),
      );
      return m("div", { class: "flex flex-col items-start" }, [
        ...buttons,
        m(AskAgentFailure, { id, model, workspaceAgentId }),
      ]);
    },
  };
}

export type AskAgentActionOptions = Omit<AskAgentButtonAttrs, "variant">;

/** The ask as an error's action (none once the route refused it); its label
 * says so while the agent starts. Pass an AskAgentFailure with the same id as
 * that error's footer, or a failed start shows nowhere. */
export function askAgentActions(options: AskAgentActionOptions): ErrorAction[] {
  const { id, model, workspaceAgentId, description, label } = options;
  if (model.isRefused) return [];
  return [
    {
      id,
      label: model.isBusy ? "Starting an agent…" : label,
      isDisabled: model.isBusy,
      onclick: () => void model.start({ workspaceAgentId, description }),
    },
  ];
}

interface AskAgentFailureAttrs {
  /** The button's id; the error display's id is built from it. */
  id: string;
  model: AskAgentModel;
  workspaceAgentId: string;
  isBugReportOffered?: boolean;
  /** Off where this sits inside another error's box, as that error's footer. */
  isBoxed?: boolean;
}

/** Why the button's start failed; nothing while it hasn't. */
export function AskAgentFailure(): m.Component<AskAgentFailureAttrs> {
  return {
    view(vnode) {
      const { id, model, workspaceAgentId, isBugReportOffered, isBoxed } =
        vnode.attrs;
      return model.error !== null
        ? m(ErrorNotice, {
            id: `${id}-error`,
            error: model.error,
            workspaceAgentId,
            isBugReportOffered: isBugReportOffered ?? model.isFailureReportable,
            isBoxed,
          })
        : null;
    },
  };
}
