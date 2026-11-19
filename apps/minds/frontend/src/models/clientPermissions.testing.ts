// Shared client-permissions fixtures for the model, tab and prompt suites.
// Deliberately NOT named *.test.ts so vitest does not collect it, the same
// rule src/testing.ts follows.

import type { ClientPermissionAsk } from "../electron-bridge";
import type {
  UiClientPermissionRow,
  UiClientPermissions,
} from "../generated/ui";

/** The microphone row as a fresh workspace reads it: deferring to the Imbue
 * default, which asks. */
export function microphoneRow(
  overrides: Partial<UiClientPermissionRow> = {},
): UiClientPermissionRow {
  return {
    permission: "microphone",
    label: "Microphone",
    description:
      "Listen to an audio input: the built-in mic, a headset, a USB interface.",
    decision: "ask",
    source: "default",
    stored: null,
    ...overrides,
  };
}

export function clientPermissionsView(
  rows: UiClientPermissionRow[] = [microphoneRow()],
): UiClientPermissions {
  return { rows };
}

/** Main's ask to decide the microphone for workspace alpha. */
export function clientPermissionAsk(
  overrides: Partial<ClientPermissionAsk> = {},
): ClientPermissionAsk {
  return {
    promptId: "prompt-1",
    workspaceId: "agent-" + "c".repeat(8),
    permission: "microphone",
    workspaceName: "alpha",
    ...overrides,
  };
}
