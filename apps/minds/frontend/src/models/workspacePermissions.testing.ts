// Shared UiWorkspacePermissions fixtures for the Permissions model and view
// suites. Deliberately NOT named *.test.ts so vitest does not collect it, the
// same rule src/testing.ts follows.
//
// One copy because the payload is generated from the server's pydantic models:
// a field added, renamed or dropped upstream has to land in exactly one place
// here, rather than in each suite that happens to describe the same tree.

import type {
  UiAvailableConnection,
  UiDesktopEgressSetting,
  UiPathSync,
  UiPermissionConnection,
  UiSharedPath,
  UiServiceSignIn,
  UiWorkspaceDesktop,
  UiWorkspacePermissions,
} from "../generated/ui";
import { desktopEgressMode, thisComputerDeviceId } from "./desktopEgressRoute";

/** A two-way sync, up and running. */
export function pathSync(overrides: Partial<UiPathSync> = {}): UiPathSync {
  return {
    activity: "ACTIVE",
    state: "SYNCED",
    message: "",
    direction: "BOTH",
    conflict: "NEWER",
    workspace_path: "~/synced_folders/home/me/notes",
    bytes_done: 0,
    bytes_total: 0,
    ...overrides,
  };
}

/** One shared-path row: ~/notes, readable, not synced. */
export function sharedPath(overrides: Partial<UiSharedPath> = {}): UiSharedPath {
  return {
    path: "/home/me/notes",
    path_label: "~/notes",
    access: "READ",
    sync: null,
    ...overrides,
  };
}

export const BROWSER_SIGN_IN: UiServiceSignIn = {
  is_browser_supported: true,
  credential_parameters: [],
  is_account_name_required: false,
};

export function credentialsSignIn(overrides: Partial<UiServiceSignIn> = {}): UiServiceSignIn {
  return {
    is_browser_supported: false,
    credential_parameters: [
      { name: "access-key-id", label: "Access key id" },
      { name: "secret-access-key", label: "Secret access key" },
    ],
    is_account_name_required: false,
    ...overrides,
  };
}

/** The setting of a workspace that runs on this computer: not offered at all. */
export const DESKTOP_EGRESS_UNSUPPORTED: UiDesktopEgressSetting = {
  is_supported: false,
  mode: "OFF",
  route: ["self"],
};

/** The desktops a route can name: this computer, then two others some route
 * of the workspace already names. */
export const DESKTOPS: UiWorkspaceDesktop[] = [
  { device_id: "host-here", is_this_computer: true },
  { device_id: "host-office", is_this_computer: false },
  { device_id: "host-studio", is_this_computer: false },
];

/** The setting of a remote workspace: offered, and on the given route (off
 * unless one is given). The mode follows the route as seen from this
 * computer, as the server's does. */
export function desktopEgress(route: string[] = ["self"]): UiDesktopEgressSetting {
  return { is_supported: true, mode: desktopEgressMode(route, thisComputerDeviceId(DESKTOPS)), route };
}

export function slackConnection(overrides: Partial<UiPermissionConnection> = {}): UiPermissionConnection {
  return {
    sign_in: BROWSER_SIGN_IN,
    desktop_egress: DESKTOP_EGRESS_UNSUPPORTED,
    service_name: "slack",
    display_name: "Slack",
    account: "",
    account_label: "Default account",
    is_connected: true,
    show_account_label: false,
    granted_count: 1,
    scopes: [
      {
        scope: "slack-api",
        heading: "Slack",
        groups: [
          {
            heading: "Chat",
            toggles: [
              { permission: "slack-chat-read", label: "Read chat", description: "Read messages", is_granted: false },
              { permission: "slack-chat-write", label: "Post messages", description: "", is_granted: true },
            ],
          },
        ],
      },
    ],
    ...overrides,
  };
}

export function awsConnection(overrides: Partial<UiPermissionConnection> = {}): UiPermissionConnection {
  return slackConnection({
    service_name: "aws",
    display_name: "AWS",
    sign_in: credentialsSignIn({ is_account_name_required: true }),
    ...overrides,
  });
}

export function awsAvailable(overrides: Partial<UiServiceSignIn> = {}): UiAvailableConnection {
  return { service_name: "aws", display_name: "AWS", sign_in: credentialsSignIn(overrides) };
}

export function permissionsView(overrides: Partial<UiWorkspacePermissions> = {}): UiWorkspacePermissions {
  return {
    host_id: "host-" + "b".repeat(8),
    connections: [slackConnection()],
    available_connections: [{ service_name: "notion", display_name: "Notion", sign_in: BROWSER_SIGN_IN }],
    shared_paths: [],
    workspace_toggles: [],
    waiting_requests: [],
    permissions_unavailable: false,
    is_sync_supported: true,
    is_credential_store_shared: true,
    ...overrides,
  };
}
