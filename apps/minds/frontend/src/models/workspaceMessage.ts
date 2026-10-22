// A message for a workspace's page, posted into its frame over the embed
// contract once the page is listening: the chat app's focus ask (an agent
// message's click), or an app message the workspace shell routes to the app
// whose manifest registers its type (a notification's destination).

import type { NotificationDestination } from "../channel/messages";

export type WorkspaceMessage =
  | { kind: "focus-chat"; chatAgentId: string }
  | { kind: "app-message"; type: string; fields: Record<string, string> };

/** The app message a notification's destination delivers: the wire's `type`
 * beside its remaining fields, as the frame receives it. Null for a
 * destination that names no type (the backend refuses one, so only a
 * hand-built entry can). */
export function workspaceMessageFromDestination(
  destination: NotificationDestination,
): WorkspaceMessage | null {
  const { type, ...fields } = destination;
  if (type === undefined || type === "") return null;
  return { kind: "app-message", type, fields };
}
