// What the shell says about the workspace on screen: the notice band over it,
// and the titlebar's status pill. One place for both, since the pill stands
// down whenever the band speaks.

import { electronBridge } from "../../electron-bridge";
import type { UpdatePill } from "../../models/updateSurfaces";
import { isPillSettledByOpening, updatePillFor } from "../../models/updateSurfaces";
import { updateChatAgentIdFor } from "../../models/updates";
import type { NoticePayload } from "./notice-band";
import { noticeBandFor } from "./notice-band";
import type { ShellState } from "./shell-state";
import type { TitlebarStatus } from "./titlebar-status";

/**
 * The band over the workspace on screen, or null. Keyed to the surface rather
 * than the route: the workspace route's own frame, and equally the machine an
 * app-level modal was opened over, which stays mounted behind the backdrop.
 */
export function surfaceNoticeBand(
  shell: ShellState,
  surfaceWorkspaceId: string | null,
): NoticePayload | null {
  const agentScoped =
    surfaceWorkspaceId === null
      ? null
      : shell.stores.workspaces.toAgentScopedId(surfaceWorkspaceId);
  const health =
    agentScoped === null
      ? "healthy"
      : shell.stores.health.statusFor(agentScoped);
  // A machine the recovery verdict reads as unreachable through its own
  // backend is unreachable for a reason the band can state, which is why it
  // reads unhealthy at all. The list frame already carries the verdict and
  // the backend's name per row, so the band can name it without a poll of
  // its own -- and says the same thing the card behind "Open recovery" will.
  const entry =
    agentScoped === null
      ? null
      : shell.stores.workspaces.entryByAnyId(agentScoped);
  const unreachableProviderLabel = entry?.is_backend_unreachable
    ? entry.provider_label || null
    : null;
  return noticeBandFor(
    health,
    shell.stores.health.discoveryHealth,
    surfaceWorkspaceId !== null,
    {
      isRestartAppAvailable: electronBridge.isDesktop,
      unreachableProviderLabel,
      deviceEnvironment: shell.stores.health.appEnvironmentCondition(),
      // So the band can tell the user's own bounce, which narrates itself,
      // from the app's unattended start, which must not hide the device.
      recoveryKind:
        agentScoped === null
          ? null
          : shell.stores.health.recoveryKindFor(agentScoped),
      // Which scopes that app-global condition to the machines it can
      // explain: one on an on-device backend answers over loopback with the
      // wifi off. A row we have no entry for keeps the conservative default.
      isWorkspaceNetworkDependent: entry?.is_network_dependent ?? true,
      isDeviceCannotConnect: entry?.is_device_cannot_connect ?? false,
      liveness: entry?.liveness ?? "",
      stopKind: entry?.stop_kind ?? "",
      isUpdateApplying:
        agentScoped !== null && shell.stores.updates.isApplying(agentScoped),
    },
  );
}

/** The workspace's run chat, "" while none is known. */
function updateChatAgentIdOf(shell: ShellState, agentScoped: string): string {
  const updates = shell.stores.updates;
  return updateChatAgentIdFor(
    updates.publishedFor(agentScoped),
    updates.isUpdating(agentScoped),
  );
}

/**
 * The update pill a workspace shows in the titlebar, or null. Stands down
 * while the band speaks: a connection or health problem is the more urgent
 * thing, and the pill's own news would read as its cause.
 */
export function shownUpdatePill(
  shell: ShellState,
  workspaceAnyId: string,
): UpdatePill | null {
  if (surfaceNoticeBand(shell, workspaceAnyId) !== null) return null;
  const agentScoped = shell.stores.workspaces.toAgentScopedId(workspaceAnyId);
  const updates = shell.stores.updates;
  return updatePillFor(
    updates.publishedFor(agentScoped),
    updates.isUpdating(agentScoped),
    updates.autoUpdateFor(agentScoped).isEnabled,
    updateChatAgentIdOf(shell, agentScoped) !== "",
  );
}

/** The titlebar's status pill for a workspace, or null; it stands down while the band speaks. */
export function titlebarStatusFor(
  shell: ShellState,
  workspaceAnyId: string | null,
): TitlebarStatus | null {
  if (workspaceAnyId === null) return null;
  const pill = shownUpdatePill(shell, workspaceAnyId);
  if (pill === null) return null;
  const agentScoped = shell.stores.workspaces.toAgentScopedId(workspaceAnyId);
  const chatAgentId = updateChatAgentIdOf(shell, agentScoped);
  return {
    key: `update-${pill.state}`,
    label: pill.label,
    tone: pill.tone,
    dot: pill.dot,
    isDotPulsing: pill.isDotPulsing,
    tooltip: pill.tooltip,
    onClick:
      pill.target === "chat"
        ? () => {
            shell.openWorkspaceChat(agentScoped, chatAgentId);
            if (!isPillSettledByOpening(pill)) return;
            void shell.stores.updates.dismissOutcome(agentScoped).then((result) => {
              if (!result.isOk) console.warn("The update's outcome could not be dismissed:", result.error, result.detail);
            });
          }
        : () => shell.openUpdatesTab(agentScoped),
  };
}
