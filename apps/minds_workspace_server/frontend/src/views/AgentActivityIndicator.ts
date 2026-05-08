/**
 * Per-agent "is the agent working?" indicator rendered above the message input.
 *
 * State derivation lives on the workspace_server side; the frontend just
 * picks the right label and animation for the broadcast ``activity_state``.
 *
 * Hidden entirely for ``IDLE``, ``null``, or any value the server doesn't
 * recognize -- the empty slot collapses so the message input does not jump.
 */

import m from "mithril";
import { getAgentById } from "../models/AgentManager";

const ACTIVITY_STATE_LABEL: Record<string, string> = {
  THINKING: "Thinking…",
  TOOL_RUNNING: "Running tool…",
  WAITING_ON_PERMISSION: "Waiting for permission",
};

export function AgentActivityIndicator(): m.Component<{ agentId: string }> {
  return {
    view(vnode) {
      const agent = getAgentById(vnode.attrs.agentId);
      if (!agent) {
        return null;
      }
      const state = agent.activity_state ?? null;
      if (state === null) {
        return null;
      }
      const label = ACTIVITY_STATE_LABEL[state];
      if (label === undefined) {
        // Unknown / IDLE / future enum value — leave the slot collapsed.
        return null;
      }
      return m(
        "div",
        {
          class: "agent-activity-indicator",
          "data-state": state,
          role: "status",
          "aria-live": "polite",
        },
        [
          m("span", { class: "agent-activity-indicator__dot" }),
          m("span", { class: "agent-activity-indicator__label" }, label),
        ],
      );
    },
  };
}
