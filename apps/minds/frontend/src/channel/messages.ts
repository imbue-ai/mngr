// Curated view over the generated wire types (src/generated/ui.ts).
//
// json-schema-to-typescript emits structurally-duplicated `Foo1` interfaces
// where a model is referenced from several parents; this module re-exports
// the canonical names once and defines the discriminated server-message
// union the channel dispatches on. The generated file remains the source of
// truth for shapes; nothing here adds or changes fields.

import type {
  UiAccountsMessage,
  UiBootstrap,
  UiDiscoveryHealthMessage,
  UiEnvironmentMessage,
  UiHealthMessage,
  UiHelloMessage,
  UiNotificationEntry,
  UiNotificationsMessage,
  UiOpenHelpMessage,
  UiProvidersMessage,
  UiReloadMessage,
  UiRequestsMessage,
  UiSnapshot,
  UiWorkspaceEntry,
  UiWorkspaceRefreshMessage,
  UiWorkspaceStoppedMessage,
  UiWorkspacesMessage,
} from "../generated/ui";

export type {
  UiAccountsMessage,
  UiBootstrap,
  UiDiscoveryHealthMessage,
  UiEnvironmentMessage,
  UiHealthMessage,
  UiHelloMessage,
  UiNotificationEntry,
  UiNotificationsMessage,
  UiOpenHelpMessage,
  UiProvidersMessage,
  UiReloadMessage,
  UiRequestsMessage,
  UiSnapshot,
  UiWorkspaceEntry,
  UiWorkspaceRefreshMessage,
  UiWorkspaceStoppedMessage,
  UiWorkspacesMessage,
};

// pydantic emits literal-defaulted `type` fields as optional in JSON Schema,
// so the generated interfaces have `type?`. The wire always carries the
// field; require it here so the union discriminates properly.
type Framed<M, T extends string> = Omit<M, "type"> & { type: T };

export type UiServerMessage =
  | Framed<UiHelloMessage, "hello">
  | Framed<UiWorkspacesMessage, "workspaces">
  | Framed<UiAccountsMessage, "accounts">
  | Framed<UiProvidersMessage, "providers">
  | Framed<UiRequestsMessage, "requests">
  | Framed<UiNotificationsMessage, "notifications">
  | Framed<UiHealthMessage, "health">
  | Framed<UiDiscoveryHealthMessage, "discovery_health">
  | Framed<UiEnvironmentMessage, "environment">
  | Framed<UiWorkspaceStoppedMessage, "workspace_stopped">
  | Framed<UiOpenHelpMessage, "open_help">
  | Framed<UiWorkspaceRefreshMessage, "workspace_refresh">
  | Framed<UiReloadMessage, "reload_ui">;

export interface UiClientState {
  type: "client_state";
  client_id: string;
  route: string;
  workspace_agent_id: string | null;
  has_focus: boolean;
}

/** Parse one channel frame; null for frames that are not a known server message. */
export function parseServerMessage(raw: string): UiServerMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const type = (data as { type?: unknown }).type;
  if (typeof type !== "string") return null;
  switch (type) {
    case "hello":
    case "workspaces":
    case "accounts":
    case "providers":
    case "requests":
    case "notifications":
    case "health":
    case "discovery_health":
    case "environment":
    case "workspace_stopped":
    case "open_help":
    case "workspace_refresh":
    case "reload_ui":
      return data as UiServerMessage;
    default:
      // Tolerant policy: unknown types are ignored, mirroring the embed
      // contract's stance (forward compatibility for additive changes).
      return null;
  }
}
