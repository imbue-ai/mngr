import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  getSidebarAgents,
  getWorktreeProtoAgents,
  isConnected,
  type AgentState,
  type ProtoAgent,
} from "../models/AgentManager";
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

function renderAgentItem(agent: AgentState, isActive: boolean, isClaimed: boolean): m.Vnode {
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

function renderProtoAgentItem(proto: ProtoAgent, isActive: boolean): m.Vnode {
  const itemClass = ["conversation-selector-item", isActive ? "conversation-selector-item--active" : ""]
    .filter(Boolean)
    .join(" ");

  return m(
    "li",
    {
      key: proto.agent_id,
      class: itemClass,
      "data-agent-id": proto.agent_id,
      onclick: () => selectAgent(proto.agent_id),
    },
    [
      m("div", { class: "conversation-selector-item-name" }, proto.name),
      m("div", { class: "conversation-selector-item-meta" }, [
        m("span", { class: "conversation-selector-item-model", style: "color: #b45309;" }, "creating..."),
      ]),
    ],
  );
}

export const AgentSelector: m.Component = {
  view() {
    const currentAgentId = getSelectedAgentId();
    const agents = getSidebarAgents();
    const protoAgents = getWorktreeProtoAgents();
    const selectorItemClaimed = isSlotClaimed("conversation-selector-item");
    const wsConnected = isConnected();

    const hasItems = agents.length > 0 || protoAgents.length > 0;

    return m(
      "div",
      { class: "conversation-selector flex flex-col flex-1 min-h-0", "data-slot": "conversation-selector" },
      [
        m(EmptySlot, { name: "sidebar-before-list" }),
        !wsConnected && !hasItems
          ? m(
              "p",
              { class: "conversation-selector-empty mt-2 px-5 text-sm text-text-secondary" },
              "Connecting...",
            )
          : !hasItems
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
                  [
                    ...agents.map((agent) =>
                      renderAgentItem(agent, agent.id === currentAgentId, selectorItemClaimed),
                    ),
                    ...protoAgents.map((proto) =>
                      renderProtoAgentItem(proto, proto.agent_id === currentAgentId),
                    ),
                  ],
                ),
              ),
      ],
    );
  },
};

// Keep export name for backwards compat
export const ConversationSelector = AgentSelector;
