// The one-time offer to turn on auto-update, over the machines list. Which
// launch or reading raises it, and what an answer records, is the model's
// (models/autoUpdatePrompt.ts); this only says it and takes the answer.

import m from "mithril";
import type { AutoUpdatePromptModel } from "../../models/autoUpdatePrompt";
import { Button } from "./Button";
import { Modal } from "./Modal";
import { Notice } from "./Notice";
import { Spinner } from "./Spinner";
import { renderUpdateWindowPicker } from "./UpdateWindowPicker";

export interface AutoUpdatePromptModalAttrs {
  model: AutoUpdatePromptModel;
}

export function AutoUpdatePromptModal(): m.Component<AutoUpdatePromptModalAttrs> {
  return {
    view(vnode) {
      const { model } = vnode.attrs;
      return m(
        Modal,
        // Closing without an answer only puts it away for this session.
        { isOpen: true, onClose: () => model.putAway(), size: "md" },
        m("div", { class: "flex flex-col gap-4", id: "auto-update-prompt" }, [
          m(
            "h2",
            { class: "type-heading text-primary" },
            "Turn on auto-updates?",
          ),
          m(
            "p",
            { class: "type-body text-secondary" },
            "Studio keeps itself up to date, but your workspaces don't update on their own. " +
              "Updating them may use tokens, so Studio won't do it without your OK.",
          ),
          m(
            "p",
            { class: "type-body text-secondary" },
            "Auto-updates keep them current, overnight:",
          ),
          m("div", { class: "flex flex-col gap-2" }, [
            renderUpdateWindowPicker({
              idSuffix: "prompt",
              startHour: model.startHour,
              endHour: model.endHour,
              onChange: (start, end) => model.setWindow(start, end),
            }),
            m("ul", { class: "type-helper text-tertiary list-disc pl-4" }, [
              m("li", "Workspaces need to be running during this window"),
              m("li", "You can change this anytime in Settings"),
            ]),
          ]),
          model.error ? m(Notice, { variant: "error" }, model.error) : null,
          m("div", { class: "flex flex-wrap items-center gap-2 pt-1" }, [
            m(
              Button,
              {
                variant: "primary",
                id: "auto-update-prompt-accept",
                disabled: model.isBusy,
                onclick: () => void model.choose("accepted"),
              },
              model.isBusy
                ? m(Spinner, { size: "sm" })
                : "Turn on auto-updates",
            ),
            m(
              Button,
              {
                variant: "secondary",
                id: "auto-update-prompt-decline",
                disabled: model.isBusy,
                onclick: () => void model.choose("declined"),
              },
              "Not now",
            ),
          ]),
        ]),
      );
    },
  };
}
