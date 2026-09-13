// The Machines home screen: workspace rows, create-attempt rows, remote
// records, the sync-unlock banner, the collapsed providers panel, and the
// bottom-left app launchers. Live state comes from the channel stores; the
// facts that do not ride the channel (destroy statuses, locked accounts) are
// fetched from /ui/api/create/landing-extras and refetched whenever the
// workspace list changes.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { electronBridge } from "../../electron-bridge";
import type { LandingExtras, MindLiveness } from "../../models/create";
import { MIND_LIVENESS_LABELS, MindLivenessTracker, fetchLandingExtras, recoveryRoute } from "../../models/create";
import type { UiWorkspaceEntry } from "../../channel/messages";
import type { UiProviderEntry } from "../../generated/ui";
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
  lifecycleConfirmation,
  mindControlsFor,
  remoteLocationBadgeFor,
  remoteStateChipFor,
} from "./landing-controls";
import { Spinner } from "../components/Spinner";
import { StatusBadge } from "../components/StatusBadge";
import type { UpdateBadgeTone } from "../../models/updates";
import { updateBadgeFor } from "../../models/updates";

const BADGE_CLASS = "inline-flex items-center px-2 py-0.5 rounded-md type-label";

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
  isProvidersOpen: boolean;
  pendingProviderToggleAtByName: Map<string, number>;
  isExtrasFailed: boolean;
  /** `<agent id>:<version>` of each "Updated to X" note dismissed this
   * session, so a later success on the same machine is news again; hides the
   * note before the backend's dismissal comes back in a pushed frame. */
  dismissedNoteKeys: Set<string>;
  /** Which bulk press is held for the go-ahead-without-backups confirmation. */
  bulkNoBackupConfirm: "now" | "schedule" | null;
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
    isProvidersOpen: false,
    pendingProviderToggleAtByName: new Map(),
    isExtrasFailed: false,
    dismissedNoteKeys: new Set(),
    bulkNoBackupConfirm: null,
  };

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
          state.unlockError = result.data.error ?? "That password did not unlock any account.";
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
      .then((response) => {
        if (!response.ok) state.removedHostIds.delete(hostId);
        m.redraw();
      })
      .catch(() => {
        state.removedHostIds.delete(hostId);
        m.redraw();
      });
  }

  function toggleProvider(name: string, isEnabled: boolean): void {
    state.pendingProviderToggleAtByName.set(name, Date.now());
    fetch(`/api/v1/desktop/providers/${encodeURIComponent(name)}`, {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: isEnabled }),
    })
      .then(async (response) => {
        if (response.ok) return;
        state.pendingProviderToggleAtByName.delete(name);
        const body = (await response.json().catch(() => null)) as { error?: string; detail?: string } | null;
        const message = body?.error ?? body?.detail ?? `HTTP ${response.status}`;
        if (response.status === 409) {
          window.alert(`Cannot disable ${name}: ${message}`);
        } else {
          window.alert(`Could not ${isEnabled ? "enable" : "disable"} ${name}: ${message}`);
        }
        m.redraw();
      })
      .catch(() => {
        state.pendingProviderToggleAtByName.delete(name);
        window.alert(`Could not ${isEnabled ? "enable" : "disable"} ${name}: the request failed (network error).`);
        m.redraw();
      });
  }

  function deleteCloudAccount(name: string): void {
    const isConfirmed = window.confirm(
      `Delete "${name}" from minds? The stored keys are forgotten. ` +
        "Your cloud resources (and anything created outside minds) are untouched.",
    );
    if (!isConfirmed) return;
    fetch(`/api/v1/desktop/cloud-accounts/${encodeURIComponent(name)}`, {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then(async (response) => {
        if (!response.ok) {
          const body = (await response.json().catch(() => null)) as { error?: string } | null;
          window.alert(body?.error ?? "Could not delete the account.");
        }
        m.redraw();
      })
      .catch(() => window.alert("Could not delete the account."));
  }

  function rowClick(entry: UiWorkspaceEntry): void {
    const { shell } = getAppContext();
    const liveness = state.tracker.displayedLiveness(entry.id, entry.liveness ?? "");
    shell.enterWorkspaceOrRecover(entry, liveness);
  }

  function stopMind(entry: UiWorkspaceEntry): void {
    const isApplying = getAppContext().stores.updates.isApplying(entry.id);
    const question = lifecycleConfirmation("stop", entry.name, isApplying);
    if (question !== null && !window.confirm(question)) return;
    void state.tracker.stop(entry.id).then((isOk) => {
      if (!isOk) window.alert(`Could not stop "${entry.name}". Check the machine's provider and try again.`);
    });
  }

  function livenessBadge(liveness: MindLiveness): m.Children {
    if (liveness === "RUNNING") return null;
    const label = MIND_LIVENESS_LABELS[liveness] ?? "Status unknown";
    const tone =
      liveness === "STOPPING" || liveness === "STARTING"
        ? "bg-warning/15 text-warning"
        : "bg-fill-subtle text-primary";
    return m("span", { class: `${BADGE_CLASS} ${tone} landing-mind-state-badge` }, label);
  }

  // The backups page runs restic from this device, so it is reachable for a
  // stopped or remote machine whose own settings pane is not. Kept subtle
  // (muted ghost icon): it is a fallback route in, not the primary action.
  function backupsButton(entry: UiWorkspaceEntry, liveness: string): m.Children {
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

  function healthBadge(entry: UiWorkspaceEntry, liveness: MindLiveness): m.Children {
    // A stopped/transitioning container is expectedly unreachable; the state
    // badge already explains it, so suppress the health badge there.
    if (liveness === "STOPPED" || liveness === "STOPPING" || liveness === "STARTING") return null;
    // While the consumer is dead every machine reads unhealthy, and the cause
    // is the app's, not theirs. The page's notice names it once instead of
    // every row repeating a symptom.
    if (!isMachineStateKnown(getAppContext().stores.health.discoveryHealth)) return null;
    const healthStore = getAppContext().stores.health;
    const label = healthBadgeLabelFor(
      healthStore.statusFor(entry.id),
      healthStore.isRecoveryANoOpFor(entry.id),
      healthStore.recoveryKindFor(entry.id),
      entry.is_device_cannot_connect ?? false,
    );
    if (label === null) return null;
    return m("span", { class: `${BADGE_CLASS} bg-warning/15 text-warning landing-health-badge` }, label);
  }

  /** The row's update badge, or null when the machine has nothing to say. */
  function updateBadge(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    const badge = updateBadgeFor(updates.publishedFor(entry.id), updates.isUpdating(entry.id));
    if (badge === null) return null;
    return m(
      "button",
      {
        type: "button",
        class: `${UPDATE_BADGE_CLASS} ${UPDATE_BADGE_TONES[badge.tone]} landing-update-badge`,
        "data-update-state": badge.state,
        "data-tooltip": badge.tooltip,
        "aria-haspopup": "dialog",
        // Raised through the shell so it is one modal with one owner (Escape,
        // navigation). The row behind is clickable, so the press stops here.
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          getAppContext().shell.openUpdateModal(entry.id);
        },
      },
      [
        badge.isSpinnerShown ? m(Spinner, { size: "sm" }) : null,
        m("span", badge.label),
        m(Icon16, { name: "chevron-right", size: "sm", extra: "opacity-70" }),
      ],
    );
  }

  /** The dismissible "Updated to X" note, whether or not the run was watched.
   * Under the row rather than in it: it is news, not state. */
  function updatedNote(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    const update = updates.forAgent(entry.id);
    const version = update.success_note_version ?? "";
    const noteKey = `${entry.id}:${version}`;
    if (!version || state.dismissedNoteKeys.has(noteKey)) return null;
    return m("div", { class: "flex items-center gap-2 pl-3 type-helper text-secondary" }, [
      m(
        "span",
        update.verdict === "UPDATED_WITH_REBUILD_ITEMS"
          ? `Updated to ${version}, with a note for you.`
          : `Updated to ${version}.`,
      ),
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
    ]);
  }

  function runBulkAction(kind: "now" | "schedule", isNoBackupConfirmed: boolean): void {
    const updates = getAppContext().stores.updates;
    const agentIds = updates.updatableAgentIds();
    if (agentIds.length === 0) return;
    // Cleared on every press: a prompt left standing from the other bulk
    // action would offer to proceed with the wrong one.
    state.bulkNoBackupConfirm = null;
    if (!isNoBackupConfirmed && agentIds.some((agentId) => updates.needsNoBackupConfirmation(agentId))) {
      state.bulkNoBackupConfirm = kind;
      return;
    }
    const call = kind === "now" ? updates.updateAllNow(agentIds) : updates.scheduleAllUpdates(agentIds);
    void call.then((result) => {
      if (!result.isOk) {
        window.alert(result.error);
      }
      m.redraw();
    });
  }

  /** The bulk strip, only when more than one machine is out of date: one
   * machine has its own row action. */
  function bulkUpdateActions(): m.Children {
    const updates = getAppContext().stores.updates;
    const agentIds = updates.updatableAgentIds();
    if (agentIds.length < 2) return null;
    return m(Notice, { extra: "mb-4", id: "landing-bulk-updates" }, [
      m("div", { class: "flex items-center justify-between gap-3 flex-wrap" }, [
        m("span", `${agentIds.length} machines have an update available.`),
        m("div", { class: "flex items-center gap-2" }, [
          m(Button, { variant: "primary", onclick: () => runBulkAction("schedule", false) }, "Schedule all updates"),
          m(Button, { variant: "secondary", onclick: () => runBulkAction("now", false) }, "Update all now"),
        ]),
      ]),
      state.bulkNoBackupConfirm !== null
        ? m("div", { class: "flex items-center justify-between gap-3 pt-2 flex-wrap" }, [
            m(
              "span",
              { class: "type-helper" },
              "Some of these machines have no backups: their updates keep every version in git " +
                "and offer a rollback, but there's no full-machine restore to fall back on.",
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
                { variant: "secondary", onclick: () => (state.bulkNoBackupConfirm = null) },
                "Cancel",
              ),
            ]),
          ])
        : null,
    ]);
  }

  function liveRow(entry: UiWorkspaceEntry): m.Children {
    const { stores } = getAppContext();
    const extras = state.extras;
    const destroyStatus = extras?.destroying_status_by_agent_id[entry.id];
    if (destroyStatus !== undefined) {
      return m(
        Card,
        {
          layout: "row",
          interactive: true,
          extra: "accent-spine relative overflow-hidden cursor-pointer",
          style: `--workspace-accent: ${entry.accent};`,
          onclick: () => m.route.set(`/destroying/${entry.id}`),
        },
        [
          m("span", { class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1" }, entry.name),
          destroyStatus === "running"
            ? m(StatusBadge, { extra: "gap-2" }, [m(Spinner, { size: "sm" }), "Destroying..."])
            : m(StatusBadge, { variant: "error" }, "Destroy failed"),
        ],
      );
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
    const row = m(
      Card,
      {
        layout: "row",
        interactive: true,
        extra: "accent-spine relative overflow-hidden cursor-pointer",
        style: `--workspace-accent: ${entry.accent};`,
        "data-agent-id": entry.id,
        onclick: () => rowClick(entry),
      },
      [
        m("span", { class: "flex-1 min-w-0 truncate font-semibold text-primary pl-1" }, entry.name),
        providerLabel ? m("span", { class: `${BADGE_CLASS} bg-fill-subtle text-secondary` }, providerLabel) : null,
        // Slot for the backup badge (T4 wires the backup-status data source).
        m("span", { class: "landing-backup-badge hidden" }),
        (entry.supports_shutdown ?? false) ? livenessBadge(liveness) : null,
        healthBadge(entry, liveness),
        updateBadge(entry),
        backupsButton(entry, liveness),
        controls.isStartShown
          ? m(
              Button,
              {
                variant: "ghost",
                size: "icon",
                "aria-label": "Start machine",
                "data-tooltip": "Start machine",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  void state.tracker.start(entry.id).then((isOk) => {
                    if (!isOk) {
                      window.alert(`Could not start "${entry.name}". Check the machine's provider and try again.`);
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
                "aria-label": "Stop machine",
                "data-tooltip": "Stop machine",
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
                "aria-label": "Restart machine",
                "data-tooltip": "Restart machine",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  const question = lifecycleConfirmation("restart", entry.name, stores.updates.isApplying(entry.id));
                  if (question !== null && !window.confirm(question)) return;
                  const returnTo = `/goto/${entry.id}/`;
                  m.route.set(recoveryRoute(entry.id, returnTo, "restart"));
                },
              },
              m(Icon16, { name: "restart" }),
            )
          : null,
        m(
          Button,
          {
            variant: "ghost",
            size: "icon",
            "aria-label": "Open machine in new window",
            "data-tooltip": "Open in new window",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              if (electronBridge.isDesktop) {
                electronBridge.openWorkspaceInNewWindow(entry.id);
              } else {
                const forwardOrigin = getAppContext().shell.mngrForwardOrigin;
                window.open(`${forwardOrigin}/goto/${entry.id}/`, "_blank", "noopener");
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
            "aria-label": "Machine settings",
            "data-tooltip": "Settings",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              m.route.set(`/workspace/${entry.id}/options?tab=settings`);
            },
          },
          m(Icon16, { name: "settings" }),
        ),
      ],
    );
    const note = updatedNote(entry);
    return note === null ? row : m("div", { class: "flex flex-col gap-1" }, [row, note]);
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
        m("span", { class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1" }, entry.name),
        entry.create_attempt_state === "creating"
          ? m(StatusBadge, { extra: "gap-2" }, [m(Spinner, { size: "sm" }), "Creating…"])
          : entry.create_attempt_state === "interrupted"
            ? m(StatusBadge, { variant: "warn" }, "Interrupted")
            : m(StatusBadge, { variant: "error" }, "Create failed"),
      ],
    );
  }

  function remoteRow(entry: UiWorkspaceEntry): m.Children {
    if (state.removedHostIds.has(entry.host_id ?? "")) return null;
    const remoteState = getAppContext().stores.workspaces.remoteWorkspaceStates[entry.id] ?? "";
    const chip = remoteStateChipFor(remoteState);
    return m(
      Card,
      {
        layout: "row",
        extra: "accent-spine relative overflow-hidden opacity-60",
        style: `--workspace-accent: ${entry.accent};`,
        "data-agent-id": entry.id,
      },
      [
        m("span", { class: "flex-1 min-w-0 truncate font-semibold text-secondary pl-1" }, entry.name),
        m("span", { class: `${BADGE_CLASS} bg-fill-subtle text-secondary` }, remoteLocationBadgeFor(entry)),
        chip === null
          ? null
          : chip.isAccountsLink
            ? m(
                "button",
                {
                  type: "button",
                  class: `${BADGE_CLASS} bg-fill-subtle text-important cursor-pointer border-0`,
                  "data-tooltip": "Sign in again from the Accounts page to see this machine here",
                  onclick: () => m.route.set("/accounts"),
                },
                chip.label,
              )
            : m(
                "span",
                { class: `${BADGE_CLASS} bg-fill-subtle ${chip.isImportant ? "text-important" : "text-secondary"}` },
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

  function providerRow(entry: UiProviderEntry): m.Children {
    const providers = getAppContext().stores.providers;
    const pendingAt = state.pendingProviderToggleAtByName.get(entry.name);
    const snapshotAt = providers.lastFullSnapshotAt ? Date.parse(providers.lastFullSnapshotAt) : 0;
    const isPending = pendingAt !== undefined && (snapshotAt === 0 || pendingAt > snapshotAt);
    return m(Card, { layout: "row", extra: "px-4 py-2 gap-1.5", "data-provider-name": entry.name }, [
      m("span", { class: "font-semibold text-primary" }, entry.name),
      entry.backend ? m("span", { class: "type-helper text-secondary" }, entry.backend) : null,
      m(
        "span",
        {
          class:
            `${BADGE_CLASS} ` +
            (entry.status === "OK"
              ? "bg-success/15 text-success"
              : entry.status === "ERROR"
                ? "bg-important/15 text-important"
                : "bg-fill-subtle text-primary"),
        },
        entry.status === "OK" ? "OK" : entry.status === "ERROR" ? "Error" : "Disabled",
      ),
      entry.status === "ERROR" && entry.error_message
        ? m(
            "span",
            {
              class: "flex-1 type-helper text-primary truncate",
              title: `${entry.error_type ?? ""}: ${entry.error_message}`,
            },
            `${entry.error_type ?? ""}: ${entry.error_message}`,
          )
        : m("span", { class: "flex-1" }),
      m(
        Button,
        {
          variant: "secondary",
          disabled: isPending,
          onclick: () => toggleProvider(entry.name, entry.status === "DISABLED"),
        },
        isPending ? "Waiting…" : entry.status === "DISABLED" ? "Enable" : "Disable",
      ),
      entry.is_cloud_account
        ? m(
            Button,
            {
              variant: "secondary",
              disabled: (entry.workspace_count ?? 0) > 0,
              title:
                (entry.workspace_count ?? 0) > 0
                  ? `In use by ${entry.workspace_count} workspace${entry.workspace_count === 1 ? "" : "s"} — destroy them first.`
                  : undefined,
              onclick: () => deleteCloudAccount(entry.name),
            },
            "Delete",
          )
        : null,
    ]);
  }

  return {
    oninit() {
      loadExtras(state);
      state.unsubscribe = getAppContext().stores.workspaces.onChanged(() => loadExtras(state));
    },
    onremove() {
      state.unsubscribe?.();
    },
    view() {
      const { stores } = getAppContext();
      const entries = stores.workspaces.workspaces;
      const liveEntries = entries.filter((entry) => !(entry.is_remote ?? false) && (entry.create_attempt_state ?? "") === "");
      const createEntries = entries.filter((entry) => (entry.create_attempt_state ?? "") !== "");
      const remoteEntries = entries.filter((entry) => (entry.is_remote ?? false));
      const hasRows = liveEntries.length + createEntries.length + remoteEntries.length > 0;
      const extras = state.extras;
      const isDiscovering =
        !hasRows &&
        !state.isExtrasFailed &&
        (extras === null || !extras.is_discovery_complete || extras.has_restorable_workspaces);
      const providerEntries = stores.providers.providers;

      return m(PageContainer, [
        state.isExtrasFailed
          ? m(
              Notice,
              { variant: "error", extra: "mb-4", id: "landing-extras-error" },
              m("div", { class: "flex items-center justify-between gap-3" }, [
                m("span", "Could not load machine details. Some rows may be missing or incomplete."),
                m(Button, { variant: "secondary", onclick: () => loadExtras(state) }, "Retry"),
              ]),
            )
          : null,
        hasRows
          ? m("div", [
              m("div", { class: "flex items-center justify-between mb-4" }, [
                m("h1", { class: "type-heading text-primary" }, "Machines"),
                m(ButtonLink, { variant: "primary", ...routeLinkAttrs("/create") }, "Create"),
              ]),
              bulkUpdateActions(),
              extras !== null && extras.locked_account_emails.length > 0
                ? m(Notice, { extra: "mb-4", id: "sync-unlock-banner" }, [
                    m("div", { class: "flex flex-col gap-2" }, [
                      m(
                        "span",
                        { class: "type-body text-primary" },
                        `Enter your master password to unlock synced machines for ${extras.locked_account_emails.join(", ")}.`,
                      ),
                      m("div", { class: "flex items-center gap-2" }, [
                        m("input", {
                          type: "password",
                          id: "sync-unlock-password",
                          autocomplete: "current-password",
                          placeholder: "master password",
                          class: "h-[34px] px-3 rounded-full type-body bg-fill-subtle text-primary",
                          value: state.unlockPassword,
                          oninput: (event: InputEvent) => {
                            state.unlockPassword = (event.target as HTMLInputElement).value;
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
                        ? m("p", { class: "type-helper text-important", role: "alert" }, state.unlockError)
                        : null,
                    ]),
                  ])
                : null,
              m("div", { class: "flex flex-col gap-1.5" }, [
                liveEntries.map((entry) => liveRow(entry)),
                createEntries.map((entry) => createAttemptRow(entry)),
                remoteEntries.map((entry) => remoteRow(entry)),
              ]),
            ])
          : isDiscovering
            ? m("div", { class: "flex flex-col items-center justify-center min-h-[80vh] gap-6" }, [
                m("p", { class: "text-tertiary text-center" }, "Discovering workspaces..."),
                m(ButtonLink, { variant: "primary", ...routeLinkAttrs("/create") }, "Create"),
              ])
            : m("div", { class: "text-center py-12" }, [
                m("p", { class: "text-tertiary mb-6" }, "No machines yet"),
                m(ButtonLink, { variant: "primary", ...routeLinkAttrs("/create") }, "Create"),
              ]),
        m(
          "div",
          { class: "mt-6" },
          m(
            "a",
            {
              id: "landing-destroyed",
              class: "inline-flex items-center gap-2 type-helper text-tertiary hover:text-primary no-underline",
              ...routeLinkAttrs("/workspaces/destroyed"),
            },
            m("span", "Recently destroyed machines"),
          ),
        ),
        providerEntries.length > 0
          ? m("section", { class: "mt-8 pt-6 border-t border-default" }, [
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
                      const enabledCount = providerEntries.filter((entry) => entry.status !== "DISABLED").length;
                      const errorCount = providerEntries.filter((entry) => entry.status === "ERROR").length;
                      const providerWord = enabledCount === 1 ? "provider" : "providers";
                      const errorSuffix =
                        errorCount > 0 ? ` (${errorCount} ${errorCount === 1 ? "error" : "errors"})` : "";
                      return `${enabledCount} ${providerWord} enabled${errorSuffix}`;
                    })(),
                  ),
                  m("span", { class: "text-tertiary ml-2" }, state.isProvidersOpen ? "▾" : "▸"),
                ],
              ),
              state.isProvidersOpen
                ? m("div", { class: "mt-3 flex flex-col gap-1.5" }, providerEntries.map((entry) => providerRow(entry)))
                : null,
            ])
          : null,
        m("div", { class: "h-20", "aria-hidden": "true" }),
        m("div", { class: "fixed bottom-3 left-3 flex flex-col items-start gap-0.5" }, [
          m(
            "button",
            {
              type: "button",
              id: "landing-minds-settings",
              class:
                "flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-secondary hover:text-primary hover:bg-fill-hover bg-transparent border-0 text-left",
              onclick: () => m.route.set("/settings"),
            },
            [m(Icon16, { name: "settings", extra: "shrink-0" }), m("span", "Minds Settings")],
          ),
          m(
            "button",
            {
              type: "button",
              id: "landing-account",
              "data-signed-in": stores.accounts.accountEmail ? "true" : "false",
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
        ]),
      ]);
    },
  };
};
