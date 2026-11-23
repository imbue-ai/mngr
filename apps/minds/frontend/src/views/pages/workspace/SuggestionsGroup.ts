// A workspace's Suggestions page in Settings (specs/chat-suggestions, 3.7): one switch per place the
// workspace's chat offers suggestions. The values live in the workspace, so the page reads them when it opens,
// and says so when the workspace has none to give.

import m from "mithril";
import type { SuggestionSwitchKey } from "../../../models/suggestionSettings";
import { SuggestionSettingsModel } from "../../../models/suggestionSettings";
import { ErrorNotice } from "../../components/ErrorDisplay";
import { SectionHeader } from "../../components/Layout";
import { renderSwitch, renderSwitchPlaceholder } from "../../components/Switch";

export const SUGGESTIONS_UNAVAILABLE_LINE =
  "Suggestions settings are available while the workspace is running and up to date.";

interface SuggestionSwitchRow {
  key: SuggestionSwitchKey;
  /** The row's id stem: `suggestions-<slug>-switch` is its switch. */
  slug: string;
  label: string;
  note: string;
}

export const SUGGESTION_SWITCH_ROWS: readonly SuggestionSwitchRow[] = [
  {
    key: "is_next_step_suggestions_enabled",
    slug: "next-step",
    label: "Next step suggestions",
    note: "Suggested replies, offered after the agent finishes a reply.",
  },
  {
    key: "is_new_chat_suggestions_enabled",
    slug: "new-chat",
    label: "New chat suggestions",
    note: "Things to try in Studio, offered on a new chat.",
  },
];

export interface SuggestionsGroupAttrs {
  agentId: string;
}

interface SuggestionsGroupState {
  model: SuggestionSettingsModel;
}

function switchFor(model: SuggestionSettingsModel, row: SuggestionSwitchRow): m.Children {
  const id = `suggestions-${row.slug}-switch`;
  if (model.settings === null) {
    return renderSwitchPlaceholder({
      id,
      isChecking: model.status === "loading",
      label: model.status === "loading" ? `Reading ${row.label}` : `${row.label} unavailable`,
    });
  }
  const isWriting = model.isWriting(row.key);
  return renderSwitch({
    id,
    isOn: model.settings[row.key],
    label: row.label,
    describedBy: `suggestions-${row.slug}-note`,
    isWaiting: isWriting,
    isDisabled: isWriting,
    onFlip: (isOn) => void model.flip(row.key, isOn),
  });
}

function switchRow(model: SuggestionSettingsModel, row: SuggestionSwitchRow): m.Children {
  return m(
    "div",
    {
      "data-suggestion-switch": row.slug,
      class: "flex items-start justify-between gap-3 py-3 border-b border-subtle",
    },
    [
      m("span", [
        m("span", { class: "type-body text-primary font-semibold" }, row.label),
        m("span", { id: `suggestions-${row.slug}-note`, class: "block type-helper text-tertiary" }, row.note),
      ]),
      switchFor(model, row),
    ],
  );
}

export function SuggestionsGroup(): m.Component<SuggestionsGroupAttrs, SuggestionsGroupState> {
  return {
    oninit(vnode) {
      vnode.state.model = new SuggestionSettingsModel(vnode.attrs.agentId);
      void vnode.state.model.load();
    },
    onremove(vnode) {
      vnode.state.model.stop();
    },
    view(vnode) {
      const { agentId } = vnode.attrs;
      // A route change between machines preserves this component instance, so the model is swapped by hand
      // rather than trusting oninit.
      if (vnode.state.model.agentId !== agentId) {
        vnode.state.model.stop();
        vnode.state.model = new SuggestionSettingsModel(agentId);
        void vnode.state.model.load();
      }
      const { model } = vnode.state;
      return m("section", { id: "ws-suggestions-group", "data-status": model.status, class: "max-w-[560px]" }, [
        m(SectionHeader, "Suggestions"),
        m(
          "p",
          { class: "type-body text-secondary mb-1" },
          "Suggestions sit above the message box in this workspace's chats, to click instead of typing. " +
            "These apply to the whole workspace, for everyone in it.",
        ),
        model.status === "unavailable"
          ? m(
              "p",
              { id: "suggestions-unavailable", class: "type-body text-secondary mb-1", role: "status" },
              SUGGESTIONS_UNAVAILABLE_LINE,
            )
          : null,
        SUGGESTION_SWITCH_ROWS.map((row) => switchRow(model, row)),
        model.writeError !== null
          ? m(
              "div",
              { class: "mt-3" },
              m(ErrorNotice, { id: "suggestions-write-error", error: model.writeError, workspaceAgentId: agentId }),
            )
          : null,
      ]);
    },
  };
}
