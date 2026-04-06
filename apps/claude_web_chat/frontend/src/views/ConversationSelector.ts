import m from "mithril";
import { isSlotClaimed } from "../slots";
import { fetchAgents, getAgents, getLoadingError, type Agent } from "../models/Conversation";
import { getSelectedAgentId, selectAgent } from "../navigation";
import { EmptySlot } from "./EmptySlot";

function stateLabel(state: string): string {
  switch (state) {
    case "RUNNING":
      return "running";
    case "WAITING":
      return "waiting";
    case "STOPPED":
      return "stopped";
    case "DONE":
      return "done";
    default:
      return state.toLowerCase();
  }
}

function renderAgentItem(agent: Agent, isActive: boolean, isClaimed: boolean): m.Vnode {
  const itemClass = ["conversation-selector-item", isActive ? "conversation-selector-item--active" : ""]
    .filter(Boolean)
    .join(" ");

  return m(
    "li",
    {
      key: agent.id,
      class: itemClass,
      "data-slot": "conversation-selector-item",
      "data-agent-id": agent.id,
      onclick: () => selectAgent(agent.id),
    },
    isClaimed
      ? null
      : [
          m("div", { class: "conversation-selector-item-name" }, agent.name || "Unnamed agent"),
          m("div", { class: "conversation-selector-item-meta" }, [
            m("span", { class: "conversation-selector-item-model" }, stateLabel(agent.state)),
          ]),
        ],
  );
}

export const AgentSelector: m.Component = {
  oninit() {
    fetchAgents();
  },
  view() {
    const currentAgentId = getSelectedAgentId();
    const agents = getAgents();
    const loadingError = getLoadingError();
    const selectorItemClaimed = isSlotClaimed("conversation-selector-item");

    return m(
      "div",
      { class: "conversation-selector flex flex-col flex-1 min-h-0", "data-slot": "conversation-selector" },
      [
        m(EmptySlot, { name: "sidebar-before-list" }),
        loadingError
          ? m("p", { class: "conversation-selector-error mt-2 text-sm text-red-500" }, `Error: ${loadingError}`)
          : agents.length === 0
            ? m(
                "p",
                { class: "conversation-selector-empty mt-2 px-5 text-sm text-text-secondary" },
                "No agents found.",
              )
            : m(
                "div",
                { class: "conversation-selector-list-wrapper flex-1 overflow-y-auto" },
                m(
                  "ul",
                  { class: "conversation-selector-list" },
                  agents.map((agent) =>
                    renderAgentItem(agent, agent.id === currentAgentId, selectorItemClaimed),
                  ),
                ),
              ),
      ],
    );
  },
};

// Keep export name for backwards compat
export const ConversationSelector = AgentSelector;
