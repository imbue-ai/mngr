// The Machines home screen: workspace rows, create-attempt rows, remote
// records, the sync-unlock banner, the collapsed providers panel, and the
// bottom-left app launchers. Live state comes from the channel stores; the
// facts that do not ride the channel (destroy statuses, locked accounts) are
// fetched from /ui/api/create/landing-extras and refetched whenever the
// workspace list changes.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { electronBridge } from "../../electron-bridge";
import type {
  CreateFormDefaults,
  LandingExtras,
  MindLiveness,
} from "../../models/create";
import {
  MindLivenessTracker,
  connectionBackendOptionFor,
  fetchCreateFormDefaults,
  fetchLandingExtras,
  recoveryRoute,
} from "../../models/create";
import { onboardingProgress } from "../../models/onboarding";
import type { UiWorkspaceEntry } from "../../channel/messages";
import type {
  UiAccountProvidersEntry,
  UiConnectionEntry,
  UiProviderEntry,
} from "../../generated/ui";
import type {
  ConnectionOwnerOption,
  StatusChip,
} from "../../models/providers";
import {
  accountSectionTitleFor,
  connectionDetailFor,
  connectionStatusChipFor,
  editControlFor,
  providerStatusChipFor,
  removeControlFor,
} from "../../models/providers";
import {
  CloudAccountsModal,
  CloudAccountsModalState,
  fetchLocalProfiles,
  redrawingLocalProfilesLoader,
} from "./create/CloudAccountsModal";
import { Button, ButtonLink } from "../components/Button";
import { Card } from "../components/Card";
import { Icon16 } from "../components/Icon";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { routeLinkAttrs } from "../components/route-link";
import {
  backupsControlFor,
  healthBadgeLabelFor,
  isMachineStateKnown,
  keyStateChipFor,
  lifecycleConfirmation,
  livenessBadgeLabelFor,
  mindControlsFor,
  remoteLocationBadgeFor,
  remoteStateChipFor,
  removeRecordFailureMessage,
  rowClickActionFor,
} from "./landing-controls";
import { Spinner } from "../components/Spinner";
import { StatusBadge } from "../components/StatusBadge";
import type { UpdateBadge, UpdateBadgeTone } from "../../models/updates";
import {
  displayVersion,
  isBadgeOpeningRunChat,
  isUpdateOffered,
  updateBadgeFor,
  updateChatAgentIdFor,
} from "../../models/updates";
import { autoUpdatePrompt } from "../../models/autoUpdatePrompt";
import { AutoUpdatePromptModal } from "../components/AutoUpdatePromptModal";

const BADGE_CLASS =
  "inline-flex items-center px-2 py-0.5 rounded-md type-label";

// The update badge is the only pill in a row that does something, so it is a
// real <button> with a border, hover fill, press scale, focus ring and chevron.
// The transparent border on the tinted tones keeps every badge the same height.
const UPDATE_BADGE_CLASS =
  "inline-flex items-center gap-1 py-0.5 pl-2 pr-1 rounded-md type-label border cursor-pointer " +
  "transition-transform duration-100 ease-in-out active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

const UPDATE_BADGE_TONES: Record<UpdateBadgeTone, string> = {
  neutral: "bg-fill-subtle text-secondary border-strong hover:bg-fill-hover",
  warn: "bg-[var(--c-warning-surface)] text-warning border-warning/50 hover:bg-warning/25",
  error: "bg-important text-white border-transparent hover:opacity-90",
};

interface LandingState {
  extras: LandingExtras | null;
  tracker: MindLivenessTracker;
  unsubscribe: (() => void) | null;
  isUnlockPending: boolean;
  unlockError: string;
  unlockPassword: string;
  removedHostIds: Set<string>;
  /** Connection instances whose removal is in flight or done, so Remove cannot be pressed twice. */
  removingConnectionInstances: Set<string>;
  /** Connection instances whose "Check now" is in flight, so it cannot be pressed twice. */
  recheckingConnectionInstances: Set<string>;
  isProvidersOpen: boolean;
  isExtrasFailed: boolean;
  /** `<agent id>:<version>` of each "Updated to X" note dismissed this
   * session, so a later success on the same machine is news again; hides the
   * note before the backend's dismissal comes back in a pushed frame. */
  dismissedNoteKeys: Set<string>;
  /** Which bulk press is held for the go-ahead-without-backups confirmation. */
  bulkNoBackupConfirm: "now" | "schedule" | null;
  /** The add-connection modal, and the create-form defaults it reads its region tables from (fetched on first open). */
  connectionModal: CloudAccountsModalState;
  formDefaults: CreateFormDefaults | null;
}

/**
 * An install that has never been taken past the start flow, with discovery
 * finished and nothing found, belongs on the start flow. Electron's startup
 * router lands there itself; this is the same rule for a plain browser, and
 * for a window that reaches home before the flow has completed.
 */
export function shouldRedirectToStartFlow(
  isOnboardingComplete: boolean,
  hasRows: boolean,
  extras: LandingExtras | null,
): boolean {
  if (isOnboardingComplete || hasRows || extras === null) return false;
  return extras.is_discovery_complete && !extras.has_restorable_workspaces;
}

function loadExtras(state: LandingState): void {
  fetchLandingExtras()
    .then((extras) => {
      state.extras = extras;
      state.isExtrasFailed = false;
      m.redraw();
    })
    .catch((error: unknown) => {
      // A failed fetch must not read as "still discovering" forever: record
      // it so the view can end the discovering state and offer a retry.
      console.error("Failed to load landing extras", error);
      state.isExtrasFailed = true;
      m.redraw();
    });
}

export const LandingPage: m.ClosureComponent = () => {
  const state: LandingState = {
    extras: null,
    tracker: new MindLivenessTracker(),
    unsubscribe: null,
    isUnlockPending: false,
    unlockError: "",
    unlockPassword: "",
    removedHostIds: new Set(),
    removingConnectionInstances: new Set(),
    recheckingConnectionInstances: new Set(),
    isProvidersOpen: false,
    isExtrasFailed: false,
    dismissedNoteKeys: new Set(),
    bulkNoBackupConfirm: null,
    connectionModal: new CloudAccountsModalState(),
    formDefaults: null,
  };

  /** Run `open` with the create-form defaults the connection modal reads its backend and region tables from (fetched on first use). */
  function withFormDefaults(open: (defaults: CreateFormDefaults) => void): void {
    if (state.formDefaults !== null) {
      open(state.formDefaults);
      return;
    }
    fetchCreateFormDefaults(null)
      .then((defaults) => {
        state.formDefaults = defaults;
        open(defaults);
      })
      .catch((error: unknown) => {
        console.error("Failed to load the connection form defaults", error);
        window.alert("Could not load the connection form. Please try again.");
      });
  }

  function openAddConnection(owner: ConnectionOwnerOption): void {
    const ownerOptions =
      getAppContext().stores.providers.connectionOwnerOptions();
    withFormDefaults((defaults) => {
      // The server lists the backends in its own order; the first is the modal's opening pick.
      const backend = defaults.connection_backends[0];
      if (backend === undefined) {
        window.alert("No cloud backend is available to add a connection for.");
        return;
      }
      state.connectionModal.openForBackend(
        backend,
        owner,
        defaults.region_selected_by_launch_mode,
        ownerOptions,
      );
      m.redraw();
    });
  }

  function openEditConnection(
    entry: UiConnectionEntry,
    owner: ConnectionOwnerOption,
  ): void {
    withFormDefaults((defaults) => {
      const backend = connectionBackendOptionFor(
        defaults.connection_backends,
        entry.backend,
      );
      if (backend === null) {
        window.alert(
          "This connection's provider is not known to this version of the app.",
        );
        return;
      }
      state.connectionModal.openForEdit(
        entry,
        backend,
        owner,
        redrawingLocalProfilesLoader(fetchLocalProfiles),
      );
      m.redraw();
    });
  }

  function submitSyncUnlock(): void {
    state.isUnlockPending = true;
    state.unlockError = "";
    fetch("/_chrome/sync-unlock", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: state.unlockPassword }),
    })
      .then(async (response) => ({
        status: response.status,
        data: (await response.json()) as { ok?: boolean; error?: string },
      }))
      .then((result) => {
        state.isUnlockPending = false;
        if (result.status === 200 && result.data.ok) {
          state.unlockPassword = "";
          loadExtras(state);
        } else {
          state.unlockError =
            result.data.error ?? "That password did not unlock any account.";
        }
        m.redraw();
      })
      .catch(() => {
        state.isUnlockPending = false;
        state.unlockError = "The unlock request failed (network error).";
        m.redraw();
      });
  }

  function removeRemoteRecord(entry: UiWorkspaceEntry): void {
    const hostId = entry.host_id ?? "";
    if (!hostId) return;
    state.removedHostIds.add(hostId);
    fetch("/_chrome/workspaces/remove-record", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host_id: hostId }),
    })
      .then(async (response) => {
        if (response.ok) {
          m.redraw();
          return;
        }
        state.removedHostIds.delete(hostId);
        const body = (await response.json().catch(() => null)) as {
          error?: string;
        } | null;
        window.alert(removeRecordFailureMessage(response.status, body));
        m.redraw();
      })
      .catch(() => {
        state.removedHostIds.delete(hostId);
        window.alert(removeRecordFailureMessage(null, null));
        m.redraw();
      });
  }

  /** Re-check a connection whose login was set up outside the app: bounce discovery and wait for the next frame. */
  function recheckConnection(entry: UiConnectionEntry): void {
    if (state.recheckingConnectionInstances.has(entry.instance)) return;
    state.recheckingConnectionInstances.add(entry.instance);
    fetch(
      `/api/v1/desktop/cloud-accounts/${encodeURIComponent(entry.instance)}/recheck`,
      { method: "POST", credentials: "same-origin" },
    )
      .then(async (response) => {
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as { error?: string } | null;
          window.alert(body?.error ?? "Could not re-check the connection.");
        }
      })
      .catch((error: unknown) => {
        console.error("Failed to re-check connection", entry.instance, error);
        window.alert("Could not re-check the connection.");
      })
      .finally(() => {
        state.recheckingConnectionInstances.delete(entry.instance);
        m.redraw();
      });
  }

  function removeConnection(entry: UiConnectionEntry): void {
    const whatIsForgotten =
      entry.credential_source === "local_profile"
        ? "The connection is removed from every device that syncs this account; the login it uses on each " +
          "device is untouched."
        : "The stored keys are forgotten on every device that syncs this account.";
    const isConfirmed = window.confirm(
      `Remove "${entry.alias}" (${entry.backend_label}) from Imbue Studio? ${whatIsForgotten} ` +
        "Your cloud resources (and anything created outside Imbue Studio) are " +
        "untouched; a workspace created on this connection by an older version of the app, or never linked to " +
        "the account, would disappear from the list for good (adding the connection again makes a new one, " +
        "which does not bring such a workspace back).",
    );
    if (!isConfirmed || state.removingConnectionInstances.has(entry.instance))
      return;
    state.removingConnectionInstances.add(entry.instance);
    fetch(
      `/api/v1/desktop/cloud-accounts/${encodeURIComponent(entry.instance)}`,
      {
        method: "DELETE",
        credentials: "same-origin",
      },
    )
      .then(async (response) => {
        if (!response.ok) {
          state.removingConnectionInstances.delete(entry.instance);
          const body = (await response.json().catch(() => null)) as {
            error?: string;
          } | null;
          window.alert(body?.error ?? "Could not remove the connection.");
        }
        m.redraw();
      })
      .catch((error: unknown) => {
        console.error("Failed to remove connection", entry.instance, error);
        state.removingConnectionInstances.delete(entry.instance);
        window.alert("Could not remove the connection.");
        m.redraw();
      });
  }

  function rowClick(entry: UiWorkspaceEntry): void {
    const { shell } = getAppContext();
    const liveness = state.tracker.displayedLiveness(
      entry.id,
      entry.liveness ?? "",
    );
    shell.enterWorkspaceOrRecover(entry, liveness);
  }

  function stopMind(entry: UiWorkspaceEntry): void {
    const isApplying = getAppContext().stores.updates.isApplying(entry.id);
    const question = lifecycleConfirmation("stop", entry.name, isApplying);
    if (question !== null && !window.confirm(question)) return;
    void state.tracker.stop(entry.id).then((isOk) => {
      if (!isOk)
        window.alert(
          `Could not stop "${entry.name}". Check the workspace's provider and try again.`,
        );
    });
  }

  function livenessBadge(liveness: MindLiveness, stopKind: string): m.Children {
    if (liveness === "RUNNING") return null;
    const label = livenessBadgeLabelFor(liveness, stopKind);
    const tone =
      liveness === "STOPPING" || liveness === "STARTING"
        ? "bg-warning/15 text-warning"
        : "bg-fill-subtle text-primary";
    return m(
      "span",
      { class: `${BADGE_CLASS} ${tone} landing-mind-state-badge` },
      label,
    );
  }

  // The backups page runs restic from this device, so it is reachable for a
  // stopped or remote machine whose own settings pane is not. Kept subtle
  // (muted ghost icon): it is a fallback route in, not the primary action.
  function backupsButton(
    entry: UiWorkspaceEntry,
    liveness: string,
  ): m.Children {
    const control = backupsControlFor(entry, liveness);
    if (!control.isShown) return null;
    return m(
      Button,
      {
        variant: "ghost",
        size: "icon",
        extra: "opacity-60",
        "aria-label": "Backups",
        "data-tooltip": control.tooltip,
        disabled: !control.isEnabled,
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          m.route.set(`/workspace/${entry.id}/backups`);
        },
      },
      m(Icon16, { name: "box" }),
    );
  }

  function healthBadge(
    entry: UiWorkspaceEntry,
    liveness: MindLiveness,
  ): m.Children {
    // A stopped/transitioning container is expectedly unreachable; the state
    // badge already explains it, so suppress the health badge there.
    if (
      liveness === "STOPPED" ||
      liveness === "STOPPING" ||
      liveness === "STARTING"
    )
      return null;
    // While the consumer is dead every machine reads unhealthy, and the cause
    // is the app's, not theirs. The page's notice names it once instead of
    // every row repeating a symptom.
    if (!isMachineStateKnown(getAppContext().stores.health.discoveryHealth))
      return null;
    const healthStore = getAppContext().stores.health;
    const label = healthBadgeLabelFor(
      healthStore.statusFor(entry.id),
      healthStore.isRecoveryANoOpFor(entry.id),
      healthStore.recoveryKindFor(entry.id),
      entry.is_device_cannot_connect ?? false,
    );
    if (label === null) return null;
    return m(
      "span",
      {
        class: `${BADGE_CLASS} bg-warning/15 text-warning landing-health-badge`,
      },
      label,
    );
  }

  function rowUpdateBadge(agentId: string): UpdateBadge | null {
    const updates = getAppContext().stores.updates;
    return updateBadgeFor(updates.publishedFor(agentId), updates.isUpdating(agentId), updates.isAutoUpdateEnabled);
  }

  /** The row's one update control, or null when the machine has nothing to
   * say: the window it opens holds the actions. */
  function updateBadge(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    const published = updates.publishedFor(entry.id);
    const badge = rowUpdateBadge(entry.id);
    if (badge === null) return null;
    const isOpeningRunChat = isBadgeOpeningRunChat(published, updates.isUpdating(entry.id));
    return m(
      "button",
      {
        type: "button",
        class: `${UPDATE_BADGE_CLASS} ${UPDATE_BADGE_TONES[badge.tone]} landing-update-badge`,
        "data-update-state": badge.state,
        "data-tooltip": badge.tooltip,
        "aria-haspopup": isOpeningRunChat ? undefined : "dialog",
        // Raised through the shell so it is one modal with one owner (Escape,
        // navigation). The row behind is clickable, so the press stops here.
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (isOpeningRunChat)
            getAppContext().shell.openWorkspaceChat(
              entry.id,
              updateChatAgentIdFor(published, updates.isUpdating(entry.id)),
            );
          else getAppContext().shell.openUpdateModal(entry.id);
        },
      },
      [
        badge.isSpinnerShown ? m(Spinner, { size: "sm" }) : null,
        m("span", badge.label),
        m(Icon16, { name: "chevron-right", size: "sm", extra: "opacity-70" }),
      ],
    );
  }

  /** Into the run's chat, wherever the run was started from: a run the
   * workspace's own schedule started never had a window the user saw. */
  function openRunChatButton(agentId: string, chatAgentId: string): m.Children {
    return m(
      Button,
      {
        variant: "ghost",
        extra: "landing-update-open-chat",
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          getAppContext().shell.openWorkspaceChat(agentId, chatAgentId);
        },
      },
      "Open chat",
    );
  }

  /** The dismissible "Updated to X" note, whether or not the run was watched.
   * Under the row rather than in it: it is news, not state. */
  function updatedNote(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    // A row says one thing about its updates: while it has a badge, the badge.
    if (rowUpdateBadge(entry.id) !== null) return null;
    const update = updates.forAgent(entry.id);
    const version = update.success_note_version ?? "";
    const noteKey = `${entry.id}:${version}`;
    if (!version || state.dismissedNoteKeys.has(noteKey)) return null;
    // The note outlives its run: once a newer run starts, the row's chat is that run's.
    const chatAgentId = update.verdict === "UPDATED" ? (update.chat_agent_id ?? "") : "";
    return m(
      "div",
      { class: "flex items-center gap-2 pl-3 type-helper text-secondary" },
      [
        m("span", `Updated to ${displayVersion(version)}.`),
        chatAgentId ? openRunChatButton(entry.id, chatAgentId) : null,
        m(
          Button,
          {
            variant: "ghost",
            "aria-label": "Dismiss",
            onclick: () => {
              state.dismissedNoteKeys.add(noteKey);
              void updates.dismissNote(entry.id).then((result) => {
                // Put it back on a refusal rather than have the note vanish this
                // session and return next launch with nothing said.
                if (!result.isOk) {
                  state.dismissedNoteKeys.delete(noteKey);
                  m.redraw();
                }
              });
            },
          },
          "Dismiss",
        ),
      ],
    );
  }

  function runBulkAction(
    kind: "now" | "schedule",
    isNoBackupConfirmed: boolean,
  ): void {
    const updates = getAppContext().stores.updates;
    const agentIds = updates.updatableAgentIds();
    if (agentIds.length === 0) return;
    // Cleared on every press: a prompt left standing from the other bulk
    // action would offer to proceed with the wrong one.
    state.bulkNoBackupConfirm = null;
    if (
      !isNoBackupConfirmed &&
      agentIds.some((agentId) => updates.needsNoBackupConfirmation(agentId))
    ) {
      state.bulkNoBackupConfirm = kind;
      return;
    }
    const call =
      kind === "now"
        ? updates.updateAllNow(agentIds)
        : updates.scheduleAllUpdates(agentIds);
    void call.then((result) => {
      if (!result.isOk) {
        window.alert(result.error);
      }
      m.redraw();
    });
  }

  /** The bulk strip, only when more than one machine is out of date: one
   * machine has its own row action. Under auto-update the app arms them
   * itself, so there is nothing to offer in bulk. */
  function bulkUpdateActions(): m.Children {
    const updates = getAppContext().stores.updates;
    if (updates.isAutoUpdateEnabled) return null;
    const agentIds = updates.updatableAgentIds();
    if (agentIds.length < 2) return null;
    return m(Notice, { extra: "mb-4", id: "landing-bulk-updates" }, [
      m("div", { class: "flex items-center justify-between gap-3 flex-wrap" }, [
        m("span", `${agentIds.length} workspaces have an update available.`),
        m("div", { class: "flex items-center gap-2" }, [
          m(
            Button,
            {
              variant: "primary",
              onclick: () => runBulkAction("schedule", false),
            },
            "Schedule all updates",
          ),
          m(
            Button,
            {
              variant: "secondary",
              onclick: () => runBulkAction("now", false),
            },
            "Update all now",
          ),
        ]),
      ]),
      state.bulkNoBackupConfirm !== null
        ? m(
            "div",
            { class: "flex items-center justify-between gap-3 pt-2 flex-wrap" },
            [
              m(
                "span",
                { class: "type-helper" },
                "Some of these workspaces have no backups: their updates keep every version in git " +
                  "and offer a rollback, but there's no full workspace restore to fall back on.",
              ),
              m("div", { class: "flex items-center gap-2" }, [
                m(
                  Button,
                  {
                    variant: "primary",
                    onclick: () => {
                      const kind = state.bulkNoBackupConfirm;
                      state.bulkNoBackupConfirm = null;
                      if (kind !== null) runBulkAction(kind, true);
                    },
                  },
                  "Go ahead without backups",
                ),
                m(
                  Button,
                  {
                    variant: "secondary",
                    onclick: () => (state.bulkNoBackupConfirm = null),
                  },
                  "Cancel",
                ),
              ]),
            ],
          )
        : null,
    ]);
  }

  function destroyRow(
    agentId: string,
    name: string,
    accent: string,
    destroyStatus: string,
  ): m.Children {
    return m(
      Card,
      {
        layout: "row",
        interactive: true,
        extra: "accent-spine relative overflow-hidden cursor-pointer",
        style: `--workspace-accent: ${accent};`,
        onclick: () => m.route.set(`/destroying/${agentId}`),
      },
      [
        m(
          "span",
          {
            class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1",
          },
          name,
        ),
        destroyStatus === "running"
          ? m(StatusBadge, { extra: "gap-2" }, [
              m(Spinner, { size: "sm" }),
              "Destroying...",
            ])
          : m(StatusBadge, { variant: "error" }, "Destroy failed"),
      ],
    );
  }

  function liveRow(entry: UiWorkspaceEntry): m.Children {
    const { stores } = getAppContext();
    const extras = state.extras;
    const destroyStatus = extras?.destroying_status_by_agent_id[entry.id];
    if (destroyStatus !== undefined) {
      return destroyRow(entry.id, entry.name, entry.accent, destroyStatus);
    }
    const discoveryHealth = stores.health.discoveryHealth;
    // Nothing is arriving to correct a frozen reading, so the row reports
    // "Status unknown" rather than showing the last one as if it were current.
    const liveness =
      (entry.supports_shutdown ?? false) && isMachineStateKnown(discoveryHealth)
        ? state.tracker.displayedLiveness(entry.id, entry.liveness ?? "")
        : ("UNKNOWN" as MindLiveness);
    const controls = mindControlsFor(entry, liveness, discoveryHealth);
    const providerLabel = entry.provider_label ?? "";
    // A row whose click would go nowhere (a cloud machine this device holds no
    // key for, a stop an operator holds) is not clickable; the chip or the
    // badge says why in place of an open that would hang. Decided by the same
    // rule the click runs, so the two cannot disagree.
    const keyChip = keyStateChipFor(entry.key_state ?? "");
    const isHealthy = stores.health.statusFor(entry.id) === "healthy";
    const isOpenable =
      rowClickActionFor(
        entry,
        state.tracker.displayedLiveness(entry.id, entry.liveness ?? ""),
        isHealthy,
      ) !== "blocked";
    const row = m(
      Card,
      {
        layout: "row",
        interactive: isOpenable,
        extra: `accent-spine relative overflow-hidden ${isOpenable ? "cursor-pointer" : "cursor-default"}`,
        style: `--workspace-accent: ${entry.accent};`,
        "data-agent-id": entry.id,
        onclick: isOpenable ? () => rowClick(entry) : undefined,
      },
      [
        m(
          "span",
          { class: "flex-1 min-w-0 truncate font-semibold text-primary pl-1" },
          entry.name,
        ),
        providerLabel
          ? m(
              "span",
              { class: `${BADGE_CLASS} bg-fill-subtle text-secondary` },
              providerLabel,
            )
          : null,
        keyChip === null
          ? null
          : m(
              "span",
              {
                class: `${BADGE_CLASS} bg-fill-subtle text-important landing-key-state-chip`,
                "data-tooltip": keyChip.tooltip,
              },
              keyChip.label,
            ),
        // Slot for the backup badge (T4 wires the backup-status data source).
        m("span", { class: "landing-backup-badge hidden" }),
        (entry.supports_shutdown ?? false)
          ? livenessBadge(liveness, entry.stop_kind ?? "")
          : null,
        healthBadge(entry, liveness),
        updateBadge(entry),
        backupsButton(entry, liveness),
        controls.isStartShown
          ? m(
              Button,
              {
                variant: "ghost",
                size: "icon",
                "aria-label": "Start workspace",
                "data-tooltip": "Start workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  void state.tracker.start(entry.id).then((isOk) => {
                    if (!isOk) {
                      window.alert(
                        `Could not start "${entry.name}". Check the workspace's provider and try again.`,
                      );
                    }
                  });
                },
              },
              m(Icon16, { name: "play" }),
            )
          : null,
        controls.isStopShown
          ? m(
              Button,
              {
                variant: "ghost",
                size: "icon",
                "aria-label": "Stop workspace",
                "data-tooltip": "Stop workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  stopMind(entry);
                },
              },
              m(Icon16, { name: "pause" }),
            )
          : null,
        !(entry.supports_shutdown ?? false)
          ? m(
              Button,
              {
                variant: "ghost",
                size: "icon",
                "aria-label": "Restart workspace",
                "data-tooltip": "Restart workspace",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  const question = lifecycleConfirmation(
                    "restart",
                    entry.name,
                    stores.updates.isApplying(entry.id),
                  );
                  if (question !== null && !window.confirm(question)) return;
                  const returnTo = `/goto/${entry.id}/`;
                  m.route.set(recoveryRoute(entry.id, returnTo, "restart"));
                },
              },
              m(Icon16, { name: "restart" }),
            )
          : null,
        !isOpenable
          ? null
          : m(
              Button,
              {
                variant: "ghost",
                size: "icon",
                "aria-label": "Open workspace in new window",
                "data-tooltip": "Open in new window",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  if (electronBridge.isDesktop) {
                    electronBridge.openWorkspaceInNewWindow(entry.id);
                  } else {
                    const forwardOrigin =
                      getAppContext().shell.mngrForwardOrigin;
                    window.open(
                      `${forwardOrigin}/goto/${entry.id}/`,
                      "_blank",
                      "noopener",
                    );
                  }
                },
              },
              m(Icon16, { name: "arrow-up-right" }),
            ),
        m(
          Button,
          {
            variant: "ghost",
            size: "icon",
            "aria-label": "Workspace settings",
            "data-tooltip": "Settings",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              getAppContext().shell.routeTo(
                `/workspace/${entry.id}/options?tab=settings`,
              );
            },
          },
          m(Icon16, { name: "settings" }),
        ),
      ],
    );
    const note = updatedNote(entry);
    return note === null
      ? row
      : m("div", { class: "flex flex-col gap-1" }, [row, note]);
  }

  function createAttemptRow(entry: UiWorkspaceEntry): m.Children {
    return m(
      Card,
      {
        layout: "row",
        interactive: true,
        extra: "accent-spine relative overflow-hidden cursor-pointer",
        style: `--workspace-accent: ${entry.accent};`,
        onclick: () => m.route.set(`/creating/${entry.id}`),
      },
      [
        m(
          "span",
          {
            class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1",
          },
          entry.name,
        ),
        entry.create_attempt_state === "creating"
          ? m(StatusBadge, { extra: "gap-2" }, [
              m(Spinner, { size: "sm" }),
              "Creating…",
            ])
          : entry.create_attempt_state === "interrupted"
            ? m(StatusBadge, { variant: "warn" }, "Interrupted")
            : m(StatusBadge, { variant: "error" }, "Create failed"),
      ],
    );
  }

  function remoteRow(entry: UiWorkspaceEntry): m.Children {
    if (state.removedHostIds.has(entry.host_id ?? "")) return null;
    const tile = getAppContext().stores.workspaces.remoteWorkspaceStates[
      entry.id
    ] ?? { state: "" };
    const chip = remoteStateChipFor(tile);
    return m(
      Card,
      {
        layout: "row",
        extra: "accent-spine relative overflow-hidden opacity-60",
        style: `--workspace-accent: ${entry.accent};`,
        "data-agent-id": entry.id,
      },
      [
        m(
          "span",
          {
            class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1",
          },
          entry.name,
        ),
        m(
          "span",
          { class: `${BADGE_CLASS} bg-fill-subtle text-secondary` },
          remoteLocationBadgeFor(entry),
        ),
        chip === null
          ? null
          : m(
              "span",
              {
                class: `${BADGE_CLASS} bg-fill-subtle ${chip.isImportant ? "text-important" : "text-secondary"}`,
                title: chip.title,
              },
              chip.label,
            ),
        backupsButton(entry, ""),
        m(
          Button,
          {
            variant: "ghost",
            size: "icon",
            "aria-label": "Remove from this list",
            "data-tooltip": "Remove from this list",
            onclick: () => removeRemoteRecord(entry),
          },
          m(Icon16, { name: "close" }),
        ),
      ],
    );
  }

  function badgeClassForTone(tone: StatusChip["tone"]): string {
    switch (tone) {
      case "ok":
        return "bg-success/15 text-success";
      case "error":
        return "bg-important/15 text-important";
      case "warn":
        return "bg-warning/15 text-warning";
      case "muted":
        return "bg-fill-subtle text-secondary";
    }
  }

  function statusChipBadge(chip: StatusChip): m.Children {
    return m(
      "span",
      { class: `${BADGE_CLASS} ${badgeClassForTone(chip.tone)}` },
      chip.label,
    );
  }

  /** The row's detail text beside its chip, or the empty spacer that keeps the controls right-aligned. */
  function detailSpan(text: string): m.Children {
    if (!text) return m("span", { class: "flex-1" });
    return m(
      "span",
      { class: "flex-1 type-helper text-primary truncate", title: text },
      text,
    );
  }

  function errorDetail(
    entry: Pick<UiProviderEntry, "error_type" | "error_message">,
  ): m.Children {
    if (!entry.error_message) return detailSpan("");
    return detailSpan(`${entry.error_type ?? ""}: ${entry.error_message}`);
  }

  function providerRow(entry: UiProviderEntry): m.Children {
    return m(
      Card,
      {
        layout: "row",
        extra: "px-4 py-2 gap-1.5",
        "data-provider-name": entry.name,
      },
      [
        m("span", { class: "font-semibold text-primary" }, entry.name),
        entry.backend
          ? m("span", { class: "type-helper text-secondary" }, entry.backend)
          : null,
        statusChipBadge(providerStatusChipFor(entry.status)),
        errorDetail(entry),
      ],
    );
  }

  function connectionRow(
    entry: UiConnectionEntry,
    owner: ConnectionOwnerOption,
  ): m.Children {
    const remove = removeControlFor(entry);
    const edit = editControlFor(entry);
    const isRemoving = state.removingConnectionInstances.has(entry.instance);
    const isRechecking = state.recheckingConnectionInstances.has(entry.instance);
    const placement =
      entry.placement.default_region ?? entry.placement.default_zone ?? "";
    return m(
      Card,
      {
        layout: "row",
        extra: "px-4 py-2 gap-1.5",
        "data-connection-instance": entry.instance,
      },
      [
        m("span", { class: "font-semibold text-primary" }, entry.alias),
        m(
          "span",
          { class: "type-helper text-secondary" },
          entry.backend_label,
        ),
        placement
          ? m("span", { class: "type-helper text-tertiary" }, placement)
          : null,
        m(
          "span",
          { class: "type-helper text-tertiary", title: entry.instance },
          entry.identifier,
        ),
        statusChipBadge(connectionStatusChipFor(entry.status)),
        detailSpan(connectionDetailFor(entry)),
        entry.status === "PROFILE_MISSING"
          ? m(
              Button,
              {
                variant: "secondary",
                disabled: isRechecking || isRemoving,
                title: "Check again whether the login is set up on this device",
                "data-connection-recheck": entry.instance,
                onclick: () => recheckConnection(entry),
              },
              isRechecking ? "Checking…" : "Check now",
            )
          : null,
        m(
          Button,
          {
            variant: "secondary",
            disabled: !edit.isEnabled || isRemoving,
            title: isRemoving ? "Removing this connection" : edit.tooltip,
            onclick: () => openEditConnection(entry, owner),
          },
          "Edit",
        ),
        m(
          Button,
          {
            variant: "secondary",
            disabled: !remove.isEnabled || isRemoving,
            title: isRemoving ? "Removing this connection" : remove.tooltip,
            onclick: () => removeConnection(entry),
          },
          "Remove",
        ),
      ],
    );
  }

  function accountSection(entry: UiAccountProvidersEntry): m.Children {
    const owner: ConnectionOwnerOption = {
      userId: entry.user_id,
      label: accountSectionTitleFor(entry),
    };
    const rows: m.Children[] = [];
    if (entry.imbue_cloud !== null) rows.push(providerRow(entry.imbue_cloud));
    for (const connection of entry.connections)
      rows.push(connectionRow(connection, owner));
    return m("div", { class: "mt-3", "data-account-section": entry.user_id }, [
      m("div", { class: "flex items-center justify-between gap-3 mb-1.5" }, [
        m(
          "span",
          { class: "type-label uppercase tracking-wide text-secondary" },
          accountSectionTitleFor(entry),
        ),
        // Only an owner the modal would keep: a locked or signed-out account
        // would silently fall back to this device.
        entry.is_connection_owner_eligible
          ? m(
              Button,
              {
                variant: "ghost",
                onclick: () => openAddConnection(owner),
              },
              "Add connection",
            )
          : null,
      ]),
      rows.length > 0
        ? m("div", { class: "flex flex-col gap-1.5" }, rows)
        : m("p", { class: "type-helper text-tertiary" }, "No connections yet."),
    ]);
  }

  function deviceSection(entries: readonly UiProviderEntry[]): m.Children {
    if (entries.length === 0) return null;
    return m("div", { class: "mt-3", "data-account-section": "device" }, [
      m("div", { class: "mb-1.5" }, [
        m(
          "span",
          { class: "type-label uppercase tracking-wide text-secondary" },
          "This device's providers",
        ),
      ]),
      m(
        "div",
        { class: "flex flex-col gap-1.5" },
        entries.map((entry) => providerRow(entry)),
      ),
    ]);
  }

  // Every input (the extras landing, the workspace list, the onboarding copy)
  // arrives with a redraw, so the hook sees each change; view() stays free of
  // navigation.
  function redirectToStartFlowIfDue(): void {
    const hasRows = getAppContext().stores.workspaces.workspaces.length > 0;
    if (
      shouldRedirectToStartFlow(
        onboardingProgress.isComplete,
        hasRows,
        state.extras,
      )
    ) {
      m.route.set("/start");
    }
  }

  /** The one-time auto-update offer, when it is due. */
  function autoUpdatePromptModal(): m.Children {
    const { stores } = getAppContext();
    const updates = stores.updates;
    const hasOutOfDateWorkspace = stores.workspaces.workspaces.some((entry) =>
      isUpdateOffered(updates.forAgent(entry.id)),
    );
    const trigger = autoUpdatePrompt.triggerFor(
      updates.hasReceivedUpdates ? updates.isAutoUpdateEnabled : null,
      hasOutOfDateWorkspace,
    );
    if (trigger === null) return null;
    return m(AutoUpdatePromptModal, { model: autoUpdatePrompt, trigger });
  }

  return {
    oninit() {
      void autoUpdatePrompt.load();
      loadExtras(state);
      state.unsubscribe = getAppContext().stores.workspaces.onChanged(() =>
        loadExtras(state),
      );
    },
    onupdate() {
      redirectToStartFlowIfDue();
    },
    onremove() {
      state.unsubscribe?.();
    },
    view() {
      const { stores } = getAppContext();
      const entries = stores.workspaces.workspaces;
      const liveEntries = entries.filter(
        (entry) =>
          !(entry.is_remote ?? false) &&
          (entry.create_attempt_state ?? "") === "",
      );
      const createEntries = entries.filter(
        (entry) => (entry.create_attempt_state ?? "") !== "",
      );
      const extras = state.extras;
      // A destroy that failed after its host went away has no live entry to badge.
      const orphanedFailedDestroys = (
        extras?.orphaned_failed_destroys ?? []
      ).filter(
        (orphan) => !liveEntries.some((entry) => entry.id === orphan.agent_id),
      );
      // Until discovery re-lists the destroyed host, its still-active record also reads as a remote tile.
      const remoteEntries = entries.filter(
        (entry) =>
          (entry.is_remote ?? false) &&
          !orphanedFailedDestroys.some(
            (orphan) => orphan.agent_id === entry.id,
          ),
      );
      const hasRows =
        liveEntries.length +
          orphanedFailedDestroys.length +
          createEntries.length +
          remoteEntries.length >
        0;
      const isDiscovering =
        !hasRows &&
        !state.isExtrasFailed &&
        (extras === null ||
          !extras.is_discovery_complete ||
          extras.has_restorable_workspaces);
      const providersStore = stores.providers;

      return m(PageContainer, [
        state.isExtrasFailed
          ? m(
              Notice,
              { variant: "error", extra: "mb-4", id: "landing-extras-error" },
              m("div", { class: "flex items-center justify-between gap-3" }, [
                m(
                  "span",
                  "Could not load workspace details. Some rows may be missing or incomplete.",
                ),
                m(
                  Button,
                  { variant: "secondary", onclick: () => loadExtras(state) },
                  "Retry",
                ),
              ]),
            )
          : null,
        hasRows
          ? m("div", [
              m("div", { class: "flex items-center justify-between mb-4" }, [
                m("h1", { class: "type-heading text-primary" }, "Workspaces"),
                m(
                  ButtonLink,
                  { variant: "primary", ...routeLinkAttrs("/create") },
                  "Create",
                ),
              ]),
              bulkUpdateActions(),
              extras !== null && extras.locked_account_emails.length > 0
                ? m(Notice, { extra: "mb-4", id: "sync-unlock-banner" }, [
                    m("div", { class: "flex flex-col gap-2" }, [
                      m(
                        "span",
                        { class: "type-body text-primary" },
                        `Enter your master password to unlock synced workspaces for ${extras.locked_account_emails.join(", ")}.`,
                      ),
                      m("div", { class: "flex items-center gap-2" }, [
                        m("input", {
                          type: "password",
                          id: "sync-unlock-password",
                          autocomplete: "current-password",
                          placeholder: "master password",
                          class:
                            "h-[34px] px-3 rounded-full type-body bg-fill-subtle text-primary",
                          value: state.unlockPassword,
                          oninput: (event: InputEvent) => {
                            state.unlockPassword = (
                              event.target as HTMLInputElement
                            ).value;
                          },
                        }),
                        m(
                          Button,
                          {
                            variant: "secondary",
                            id: "sync-unlock-btn",
                            disabled: state.isUnlockPending,
                            onclick: submitSyncUnlock,
                          },
                          "Unlock",
                        ),
                      ]),
                      state.unlockError
                        ? m(
                            "p",
                            {
                              class: "type-helper text-important",
                              role: "alert",
                            },
                            state.unlockError,
                          )
                        : null,
                    ]),
                  ])
                : null,
              m("div", { class: "flex flex-col gap-1.5" }, [
                liveEntries.map((entry) => liveRow(entry)),
                orphanedFailedDestroys.map((orphan) =>
                  destroyRow(
                    orphan.agent_id,
                    orphan.name,
                    orphan.accent,
                    "failed",
                  ),
                ),
                createEntries.map((entry) => createAttemptRow(entry)),
                remoteEntries.map((entry) => remoteRow(entry)),
              ]),
            ])
          : isDiscovering
            ? m(
                "div",
                {
                  class:
                    "flex flex-col items-center justify-center min-h-[80vh] gap-6",
                },
                [
                  m(
                    "p",
                    { class: "text-tertiary text-center" },
                    "Discovering workspaces...",
                  ),
                  m(
                    ButtonLink,
                    { variant: "primary", ...routeLinkAttrs("/create") },
                    "Create",
                  ),
                ],
              )
            : m("div", { class: "text-center py-12" }, [
                m("p", { class: "text-tertiary mb-6" }, "No workspaces yet"),
                m(
                  ButtonLink,
                  { variant: "primary", ...routeLinkAttrs("/create") },
                  "Create",
                ),
              ]),
        m(
          "div",
          { class: "mt-6" },
          m(
            "a",
            {
              id: "landing-destroyed",
              class:
                "inline-flex items-center gap-2 type-helper text-tertiary hover:text-primary no-underline",
              ...routeLinkAttrs("/workspaces/destroyed"),
            },
            m("span", "Recently destroyed workspaces"),
          ),
        ),
        m(
          "section",
          {
            class: "mt-8 pt-6 border-t border-default",
            id: "landing-providers",
          },
          [
            m(
              "button",
              {
                type: "button",
                class:
                  "w-full flex items-center justify-between text-left type-body text-primary bg-transparent border-0 cursor-pointer p-0",
                onclick: () => {
                  state.isProvidersOpen = !state.isProvidersOpen;
                },
              },
              [
                m(
                  "span",
                  (() => {
                    const rowCount = providersStore.rowCount();
                    const errorCount = providersStore.errorCount();
                    const providerWord =
                      rowCount === 1 ? "provider" : "providers";
                    const errorSuffix =
                      errorCount > 0
                        ? ` (${errorCount} ${errorCount === 1 ? "error" : "errors"})`
                        : "";
                    return `${rowCount} ${providerWord}${errorSuffix}`;
                  })(),
                ),
                m(
                  "span",
                  { class: "text-tertiary ml-2" },
                  state.isProvidersOpen ? "▾" : "▸",
                ),
              ],
            ),
            state.isProvidersOpen
              ? m("div", [
                  providersStore.accounts.map((entry) => accountSection(entry)),
                  deviceSection(providersStore.deviceProviders),
                ])
              : null,
          ],
        ),
        m(CloudAccountsModal, {
          state: state.connectionModal,
          regionOptionsByBackend:
            state.formDefaults?.region_options_by_launch_mode ?? {},
          backends: state.formDefaults?.connection_backends ?? [],
          ownerOptions: providersStore.connectionOwnerOptions(),
          // The panel re-renders from the next providers frame (the materializer bounces discovery).
          onAccountAdded: () => m.redraw(),
        }),
        autoUpdatePromptModal(),
        m("div", { class: "h-20", "aria-hidden": "true" }),
        m(
          "div",
          { class: "fixed bottom-3 left-3 flex flex-col items-start gap-0.5" },
          [
            m(
              "button",
              {
                type: "button",
                id: "landing-minds-settings",
                class:
                  "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
                onclick: () => m.route.set("/settings"),
              },
              [
                m(Icon16, { name: "settings", extra: "shrink-0" }),
                m("span", "Imbue Studio Settings"),
              ],
            ),
            m(
              "button",
              {
                type: "button",
                id: "landing-account",
                "data-signed-in": stores.accounts.accountEmail
                  ? "true"
                  : "false",
                class:
                  "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
                onclick: () => m.route.set("/accounts"),
              },
              [
                m(Icon16, { name: "user", extra: "shrink-0" }),
                m(
                  "span",
                  stores.accounts.accountEmail
                    ? stores.accounts.extraAccountCount > 0
                      ? `${stores.accounts.accountEmail} (+${stores.accounts.extraAccountCount})`
                      : stores.accounts.accountEmail
                    : "Log in",
                ),
              ],
            ),
          ],
        ),
      ]);
    },
  };
};
