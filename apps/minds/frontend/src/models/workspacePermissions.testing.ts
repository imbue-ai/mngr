// Shared UiWorkspacePermissions fixtures for the Permissions model and view
// suites. Deliberately NOT named *.test.ts so vitest does not collect it, the
// same rule src/testing.ts follows.
//
// One copy because the payload is generated from the server's pydantic models:
// a field added, renamed or dropped upstream has to land in exactly one place
// here, rather than in each suite that happens to describe the same tree.

import type {
  UiAvailableConnection,
  UiPermissionConnection,
  UiServiceSignIn,
  UiWorkspacePermissions,
} from "../generated/ui";

/** Slack signs in through a browser; AWS is connected by typing credentials in.
 * Every fixture below picks one deliberately, so no test can pass by treating
 * the two the same way. */
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

export function slackConnection(overrides: Partial<UiPermissionConnection> = {}): UiPermissionConnection {
  return {
    sign_in: BROWSER_SIGN_IN,
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
    file_sharing_toggles: [],
    workspace_toggles: [],
    waiting_requests: [],
    permissions_unavailable: false,
    ...overrides,
  };
}
