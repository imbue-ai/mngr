// The home screen (specs/home-screen-redesign): a tab strip over three tabs.
// Workspaces holds the account's own workspaces as wallpaper cards, then the
// workspaces shared with it; Apps holds the apps of those workspaces and the
// apps shared with it, as desktop icons; Invitations holds the shares still
// waiting for an answer. Live state comes from the channel stores; the facts
// that do not ride the channel (destroy statuses, the window's account's
// locked state) are fetched from /ui/api/create/landing-extras and refetched
// whenever the workspace list or the window's account changes.

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
import {
  REPORT_CONFIRMATION,
  type SharedApp,
  byNewestGrant,
  homeSharedRows,
  isDeclinedInvitation,
  isInvitation,
  isRetryOffered,
  isShareLive,
  isSharedEntry,
  isWorkspaceShare,
  ownedWorkspaceEntries,
  openTargetApp,
  ownerLabel,
  primaryTarget,
  relativeTimeLabel,
  sharedActionErrorMessage,
  sharedAppsOf,
  sharedWithMeApi,
  verdictLabel,
} from "../../models/sharedWithMe";
import { showErrorDialog, titledError } from "../../models/errorDialog";
import { errorFromException, errorFromResponse, fallbackErrorSentence } from "../../models/errors";
import type { RequestOutcome } from "../../models/errors";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { webLogin } from "../../models/webLogin";
import type { UiAccountEntry, UiWorkspaceEntry } from "../../channel/messages";
import type {
  UiAccountProvidersEntry,
  UiConnectionEntry,
  UiProviderEntry,
  UiSharedWorkspace,
} from "../../generated/ui";
import type {
  ConnectionOwnerOption,
  RowProblem,
  StatusChip,
} from "../../models/providers";
import {
  accountSectionTitleFor,
  connectionRemovalDialog,
  connectionRowProblemFor,
  connectionStatusChipFor,
  editControlFor,
  providerRowProblemFor,
  providersSummaryFor,
  providerStatusChipFor,
  removeControlFor,
} from "../../models/providers";
import {
  CloudAccountsModal,
  CloudAccountsModalState,
  fetchLocalProfiles,
  redrawingLocalProfilesLoader,
} from "./create/CloudAccountsModal";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { ErrorNotice, restartAppActions, updateAppActions } from "../components/ErrorDisplay";
import { Icon16 } from "../components/Icon";
import type { IconName } from "../components/icons";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { variantBesideErrors } from "../components/variantBesideErrors";
import { alertWithDetailsInLog } from "../../models/errors";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import { routeLinkAttrs } from "../components/route-link";
import { Avatar } from "../components/Avatar";
import { CARD_ICON_BUTTON_CLASS, CardMenu, type CardMenuFact, type CardMenuItem } from "../components/CardMenu";
import { Disclosure } from "../components/Disclosure";
import { HomeAppIcon } from "../components/HomeAppIcon";
import { HomeBadge, homeBadgeClass } from "../components/HomeBadge";
import { MonogramTile } from "../components/MonogramTile";
import { TabStrip, type TabStripTab } from "../components/TabStrip";
import { TitlebarButton } from "../components/TitlebarButton";
import {
  backupsControlFor,
  badgeToneFor,
  healthBadgeLabelFor,
  isMachineStateKnown,
  isSpinningBadgeLabel,
  isOutageExplainedByDevice,
  keyStateChipFor,
  lifecycleConfirmation,
  lifecycleFailureError,
  livenessBadgeFor,
  mindControlsFor,
  remoteLocationBadgeFor,
  type RemoteStateChip,
  remoteStateChipFor,
  remoteTileEntries,
  removeRecordFailureFor,
  rowClickActionFor,
} from "./landing-controls";
import { Spinner } from "../components/Spinner";
import { accountSwitcherRows } from "../shell/AccountSwitcher";
import type { UpdateBadge } from "../../models/updates";
import {
  bulkUpdateRefusal,
  displayVersion,
  isBadgeOpeningRunChat,
  updateBadgeFor,
  updateChatAgentIdFor,
} from "../../models/updates";
import { type HomeTab, readRememberedHomeTab, rememberHomeTab } from "../../models/homeTab";
import { appIconUrl, wallpaperUrl } from "../../models/workspaceAssets";
import { wallpaperToneFor } from "../../models/wallpaperTone";

const BADGE_CLASS =
  "inline-flex items-center px-1.5 py-0.5 rounded-md type-helper font-semibold";

// The update badge is the only badge on a card that does something, so it is a
// real <button> with a press scale, focus ring and chevron.
const UPDATE_BADGE_CLASS =
  "cursor-pointer transition-transform duration-100 ease-in-out active:scale-[0.98] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

/** A card's height, which the Add workspace card matches. */
const CARD_MIN_HEIGHT_CLASS = "min-h-24";

/** The two-column grid every card section lays out in. */
const CARD_GRID_CLASS = "grid gap-3 grid-cols-2";

const NO_ADDRESS_MESSAGE = "The workspace has not registered its address yet";
// What a shared row shows from its open's click until the frame takes over (specs/shared-with-me, 4.7).
const CONNECTING_LABEL = "Connecting…";
/** An own app's tooltip when its workspace cannot be opened from here; the workspace's card says why. */
const BLOCKED_WORKSPACE_MESSAGE = "The workspace cannot be opened from this device right now; its card says why";

interface LandingState {
  extras: LandingExtras | null;
  /** The account `extras` answers for; null until a fetch has landed. */
  extrasAccountId: string | null;
  /** The account of the newest extras request, in flight or landed, so a
   * redraw does not re-ask and a stale answer does not overwrite a newer one. */
  requestedExtrasAccountId: string | null;
  tracker: MindLivenessTracker;
  unsubscribe: (() => void) | null;
  isUnlockPending: boolean;
  unlockError: string;
  unlockPassword: string;
  removedHostIds: Set<string>;
  /** Connection instances whose removal is in flight or done, so Remove cannot be pressed twice. */
  removingConnectionInstances: Set<string>;
  /** Connection instances whose "Check again" is in flight, so it cannot be pressed twice. */
  recheckingConnectionInstances: Set<string>;
  isProvidersOpen: boolean;
  isExtrasFailed: boolean;
  /** `<agent id>:<version>` of each "Updated to X" note dismissed this
   * session, so a later success on the same machine is news again; hides the
   * note before the backend's dismissal comes back in a pushed frame. */
  dismissedNoteKeys: Set<string>;
  /** Which bulk press is held for the go-ahead-without-backups confirmation. */
  bulkNoBackupConfirm: "now" | "schedule" | null;
  /** Why the last bulk press was refused, drawn under the bulk buttons. */
  bulkError: { error: UserFacingError; actions: ErrorAction[]; isBugReportOffered: boolean } | null;
  /** The add-connection modal, and the create-form defaults it reads its region tables from (fetched on first open). */
  connectionModal: CloudAccountsModalState;
  formDefaults: CreateFormDefaults | null;
  /** The shared rows' state: the pasted-link input, which rows have an action
   * in flight, and the last refusal to show (specs/shared-with-me, 4.7). */
  sharedLinkDraft: string;
  sharedBusyKeys: Set<string>;
  sharedError: string;
  /** The share key the deep link's `?shared=` named and the page already scrolled to, so a redraw never scrolls again. */
  scrolledSharedKey: string | null;
  /** The share key the deep link named and the page already picked a tab for. */
  deepLinkTabKey: string | null;
  /** The tab up (specs/home-screen-redesign, 4.3). */
  tab: HomeTab;
  /** Invitations declined while the Invitations tab was up: their cards stay
   * in place, reading Declined with Undo, until the tab is left. */
  pendingDeclinedKeys: Set<string>;
  isDeclinedOpen: boolean;
}

/**
 * An install that has never been taken past the start flow, with discovery
 * finished and no workspace under any account, belongs on the start flow.
 * Electron's startup router lands there itself; this is the same rule for a
 * plain browser, and for a window that reaches home before the flow has
 * completed.
 */
export function shouldRedirectToStartFlow(
  isOnboardingComplete: boolean,
  hasWorkspacesOnInstall: boolean,
  extras: LandingExtras | null,
): boolean {
  if (isOnboardingComplete || hasWorkspacesOnInstall || extras === null) return false;
  return extras.is_discovery_complete;
}

/** An account (or the device) other than the window's that has workspaces here. */
export interface OtherAccountWithWorkspaces {
  accountId: string;
  label: string;
  workspaceCount: number;
}

/** The accounts other than the window's with workspaces here, in the switcher's order. */
export function otherAccountsWithWorkspaces(
  windowAccountId: string,
  workspaceCountByAccountId: Readonly<Record<string, number>>,
  signedIn: readonly UiAccountEntry[],
  signedOut: readonly UiAccountEntry[],
): OtherAccountWithWorkspaces[] {
  return accountSwitcherRows(windowAccountId, signedIn, signedOut)
    .filter((row) => !row.isCurrent && (workspaceCountByAccountId[row.accountId] ?? 0) > 0)
    .map((row) => ({
      accountId: row.accountId,
      label: row.label,
      workspaceCount: workspaceCountByAccountId[row.accountId],
    }));
}

function otherAccountsOfWindow(): OtherAccountWithWorkspaces[] {
  const { stores, shell } = getAppContext();
  return otherAccountsWithWorkspaces(
    shell.accountId,
    stores.workspaces.workspaceCountByAccountId,
    stores.accounts.accounts,
    stores.accounts.signedOutAccounts,
  );
}

/** Offers to move the window to each account that has workspaces, for a home page with none of its own. */
function otherAccountsNotice(others: readonly OtherAccountWithWorkspaces[]): m.Children {
  if (others.length === 0) return null;
  const { shell } = getAppContext();
  return m(
    Notice,
    { id: "landing-other-accounts", extra: "mb-4" },
    m("div", { class: "flex flex-col gap-2" }, [
      m(
        "span",
        { class: "type-body text-primary" },
        "Other accounts have workspaces here. Switch this window to:",
      ),
      m(
        "div",
        { class: "flex flex-wrap gap-2" },
        others.map((other) =>
          m(
            Button,
            {
              variant: "secondary",
              "data-account-id": other.accountId,
              disabled: shell.isAccountSwitchPending,
              onclick: () => void shell.switchAccount(other.accountId),
            },
            `${other.label} · ${other.workspaceCount} ${other.workspaceCount === 1 ? "workspace" : "workspaces"}`,
          ),
        ),
      ),
    ]),
  );
}

/**
 * Whether the sync-unlock banner is up: the loaded extras answer for the
 * window's own account (a hub-to-hub navigation keeps the previous account's
 * extras until the refetch lands) and name it as locked.
 */
export function isUnlockBannerDue(
  extras: LandingExtras | null,
  extrasAccountId: string | null,
  windowAccountId: string,
): boolean {
  if (extras === null || extrasAccountId !== windowAccountId) return false;
  return extras.locked_account_emails.length > 0;
}

/** Which tab a share's card or icon is on (specs/home-screen-redesign, 4.3). */
export function homeTabOfShare(share: UiSharedWorkspace): HomeTab {
  if (isInvitation(share) || isDeclinedInvitation(share)) return "invitations";
  return isWorkspaceShare(share) ? "workspaces" : "apps";
}

/** The tabs the strip shows: Invitations only while any invitation waits or was declined. */
export function homeTabsFor(invitationCount: number, hasDeclined: boolean): TabStripTab[] {
  const tabs: TabStripTab[] = [
    { id: "workspaces", label: "Workspaces" },
    { id: "apps", label: "Apps" },
  ];
  if (invitationCount > 0 || hasDeclined) tabs.push({ id: "invitations", label: "Invitations", count: invitationCount });
  return tabs;
}

function loadExtras(state: LandingState): void {
  const accountId = getAppContext().shell.accountId;
  state.requestedExtrasAccountId = accountId;
  fetchLandingExtras(accountId)
    .then((extras) => {
      // An answer the window has navigated away from would overwrite the
      // newer request's: drop it and let that request land.
      if (state.requestedExtrasAccountId !== accountId) return;
      state.extras = extras;
      state.extrasAccountId = accountId;
      state.isExtrasFailed = false;
      m.redraw();
    })
    .catch((error: unknown) => {
      if (state.requestedExtrasAccountId !== accountId) return;
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
    extrasAccountId: null,
    requestedExtrasAccountId: null,
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
    bulkError: null,
    connectionModal: new CloudAccountsModalState(),
    formDefaults: null,
    sharedLinkDraft: "",
    sharedBusyKeys: new Set(),
    sharedError: "",
    scrolledSharedKey: null,
    deepLinkTabKey: null,
    tab: readRememberedHomeTab(),
    pendingDeclinedKeys: new Set(),
    isDeclinedOpen: false,
  };

  /** Run `open` with the create-form defaults the connection modal reads its backend and region tables from (fetched on first use). */
  function withFormDefaults(open: (defaults: CreateFormDefaults) => void): void {
    if (state.formDefaults !== null) {
      open(state.formDefaults);
      return;
    }
    fetchCreateFormDefaults(null, getAppContext().shell.accountId)
      .then((defaults) => {
        state.formDefaults = defaults;
        open(defaults);
      })
      .catch((error: unknown) => {
        console.error("Failed to load the connection form defaults", error);
        showErrorDialog(
          titledError(
            "Couldn't open the connection form",
            errorFromException("Couldn't open the connection form", error),
          ),
        );
      });
  }

  function openAddConnection(owner: ConnectionOwnerOption): void {
    const ownerOptions =
      getAppContext().stores.providers.connectionOwnerOptions();
    withFormDefaults((defaults) => {
      // The server lists the backends in its own order; the first is the modal's opening pick.
      const backend = defaults.connection_backends[0];
      if (backend === undefined) {
        showErrorDialog({
          title: "Couldn't add a connection",
          error: {
            // The form defaults are kept once fetched, so opening the form again finds the same empty list.
            message: fallbackErrorSentence(
              "Imbue Studio couldn't find a cloud provider to add one for",
              { isRetryable: false },
            ),
            details: "The create form's defaults list no connection backends.",
          },
        });
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
        showErrorDialog({
          title: `Couldn't edit ${entry.alias}`,
          error: {
            message:
              `This version of Imbue Studio can't edit ${entry.backend_label} connections. ` +
              "*Update Imbue Studio, then try again.*",
            details: `Unknown connection backend: ${entry.backend}`,
          },
          actions: updateAppActions(),
          isBugReportOffered: false,
        });
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
    void defaultFetchJson("/_chrome/workspaces/remove-record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host_id: hostId }),
    }).then((outcome) => {
      if (!outcome.ok) {
        state.removedHostIds.delete(hostId);
        const failure = removeRecordFailureFor(outcome);
        showErrorDialog({
          ...titledError(
            "Couldn't take this workspace off the list",
            failure.error,
          ),
          isBugReportOffered: failure.isBugReportOffered,
          actions: failure.isRestartAppOffered
            ? restartAppActions()
            : failure.isUpdateAppOffered
              ? updateAppActions()
              : [],
        });
      }
      m.redraw();
    });
  }

  /** Re-check a connection after the user fixed it outside the app: bounce discovery and wait for the next frame. */
  function recheckConnection(entry: UiConnectionEntry): void {
    if (state.recheckingConnectionInstances.has(entry.instance)) return;
    state.recheckingConnectionInstances.add(entry.instance);
    void defaultFetchJson(`/api/v1/desktop/cloud-accounts/${encodeURIComponent(entry.instance)}/recheck`, {
      method: "POST",
    }).then((outcome) => {
      state.recheckingConnectionInstances.delete(entry.instance);
      if (!outcome.ok) {
        console.error("Failed to re-check connection", entry.instance, outcome.status, outcome.body);
        showErrorDialog(
          titledError(
            `Couldn't check ${entry.alias} again`,
            errorFromResponse(`Couldn't check ${entry.alias} again`, outcome),
          ),
        );
      }
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
    void defaultFetchJson(`/api/v1/desktop/cloud-accounts/${encodeURIComponent(entry.instance)}`, {
      method: "DELETE",
    }).then((outcome) => {
      if (!outcome.ok) {
        state.removingConnectionInstances.delete(entry.instance);
        console.error("Failed to remove connection", entry.instance, outcome.status, outcome.body);
        showErrorDialog(connectionRemovalDialog(entry.alias, outcome, updateAppActions()));
      }
      m.redraw();
    });
  }

  function displayedLivenessOf(entry: UiWorkspaceEntry): MindLiveness {
    return state.tracker.displayedLiveness(entry.id, entry.liveness ?? "");
  }

  function rowClick(entry: UiWorkspaceEntry): void {
    getAppContext().shell.enterWorkspaceOrRecover(entry, displayedLivenessOf(entry));
  }

  /** Whether a click on the workspace goes anywhere (`rowClickActionFor`): a
   * cloud machine this device holds no key for, or a stop an operator holds,
   * is not clickable, and its chip or badge says why. */
  function isEntryOpenable(entry: UiWorkspaceEntry): boolean {
    const isHealthy = getAppContext().stores.health.statusFor(entry.id) === "healthy";
    return rowClickActionFor(entry, displayedLivenessOf(entry), isHealthy) !== "blocked";
  }

  function startMind(entry: UiWorkspaceEntry): void {
    void state.tracker.start(entry.id).then((result) => {
      if (!result.isOk) showLifecycleFailure("start", entry, result.outcome);
    });
  }

  function stopMind(entry: UiWorkspaceEntry): void {
    const isApplying = getAppContext().stores.updates.isApplying(entry.id);
    const question = lifecycleConfirmation("stop", entry.name, isApplying);
    if (question !== null && !window.confirm(question)) return;
    void state.tracker.stop(entry.id).then((result) => {
      if (!result.isOk) showLifecycleFailure("stop", entry, result.outcome);
    });
  }

  function showLifecycleFailure(action: "start" | "stop", entry: UiWorkspaceEntry, outcome: RequestOutcome): void {
    const failure = lifecycleFailureError(action, entry.name, entry.stop_kind ?? "", outcome);
    const retry = action === "start" ? () => startMind(entry) : () => stopMind(entry);
    showErrorDialog({
      // A hold is a refusal that stands, so its title says the workspace
      // can't be started rather than that an attempt failed.
      ...titledError(
        failure.isHold
          ? `Can't ${action} "${entry.name}"`
          : `Couldn't ${action} "${entry.name}"`,
        failure.error,
      ),
      // A hold is explained in full by its own sentence; there is nothing to
      // report, and trying again meets the same hold.
      actions: failure.isHold ? [] : [{ label: "Try again", onclick: retry }],
      isBugReportOffered: !failure.isHold,
      workspaceAgentId: entry.id,
    });
  }

  function restartMind(entry: UiWorkspaceEntry): void {
    const question = lifecycleConfirmation(
      "restart",
      entry.name,
      getAppContext().stores.updates.isApplying(entry.id),
    );
    if (question !== null && !window.confirm(question)) return;
    m.route.set(recoveryRoute(entry.id, `/goto/${entry.id}/`, "restart"));
  }

  function openInNewWindow(entry: UiWorkspaceEntry): void {
    if (electronBridge.isDesktop) {
      electronBridge.openWorkspaceInNewWindow(entry.id);
    } else {
      const forwardOrigin = getAppContext().shell.mngrForwardOrigin;
      window.open(`${forwardOrigin}/goto/${entry.id}/`, "_blank", "noopener");
    }
  }

  function openSettings(entry: UiWorkspaceEntry): void {
    getAppContext().shell.routeTo(`/workspace/${entry.id}/options?tab=settings`);
  }

  /** One status badge, toned by the label's rule (specs/home-screen-redesign, 4.13). */
  function badge(label: string, attrs: m.Attributes = {}): m.Children {
    return m(
      HomeBadge,
      { tone: badgeToneFor(label), isSpinning: isSpinningBadgeLabel(label), ...attrs },
      label,
    );
  }

  function livenessBadge(liveness: MindLiveness, stopKind: string): m.Children {
    if (liveness === "RUNNING") return null;
    const held = livenessBadgeFor(liveness, stopKind);
    return badge(held.label, {
      "data-badge": "liveness",
      extra: "landing-mind-state-badge",
      "data-tooltip": held.tooltip || undefined,
    });
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
    // Likewise while this device is offline or on a network that blocks the
    // connection: every workspace it has to reach over the network reads
    // unhealthy for this device's reason, which the page's notice names once.
    if (isOutageExplainedByDevice(healthStore.appEnvironmentCondition(), entry.is_network_dependent ?? true)) {
      return null;
    }
    const label = healthBadgeLabelFor(
      healthStore.statusFor(entry.id),
      healthStore.isRecoveryANoOpFor(entry.id),
      healthStore.recoveryKindFor(entry.id),
      entry.is_device_cannot_connect ?? false,
      healthStore.recoveryFailureCauseFor(entry.id),
    );
    if (label === null) return null;
    return badge(label, { "data-badge": "health", extra: "landing-health-badge" });
  }

  function rowUpdateBadge(agentId: string): UpdateBadge | null {
    const updates = getAppContext().stores.updates;
    return updateBadgeFor(
      updates.publishedFor(agentId),
      updates.isUpdating(agentId),
      updates.autoUpdateFor(agentId).isEnabled,
    );
  }

  /** The card's one update control, or null when the machine has nothing to
   * say: the Updates tab it opens, or a running update's chat, holds the actions. */
  function updateBadge(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    const published = updates.publishedFor(entry.id);
    const badgeInfo = rowUpdateBadge(entry.id);
    if (badgeInfo === null) return null;
    const isOpeningRunChat = isBadgeOpeningRunChat(published, updates.isUpdating(entry.id));
    const tone = badgeInfo.tone === "error" ? "alarm" : badgeInfo.tone;
    return m(
      "button",
      {
        type: "button",
        class: `${homeBadgeClass(tone, "border-0 landing-update-badge")} ${UPDATE_BADGE_CLASS}`,
        "data-update-state": badgeInfo.state,
        "data-badge-tone": tone,
        "data-tooltip": badgeInfo.tooltip,
        // The card behind is clickable, so the press stops here.
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (isOpeningRunChat)
            getAppContext().shell.openWorkspaceChat(
              entry.id,
              updateChatAgentIdFor(published, updates.isUpdating(entry.id)),
            );
          else getAppContext().shell.openUpdatesTab(entry.id);
        },
      },
      [
        badgeInfo.isSpinnerShown ? m(Spinner, { size: "sm" }) : null,
        m("span", badgeInfo.label),
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

  /** The dismissible "Updated to X" note, whether or not the run was watched:
   * the card's last row, since it is news, not state. */
  function updatedNote(entry: UiWorkspaceEntry): m.Children {
    const updates = getAppContext().stores.updates;
    // A card says one thing about its updates: while it has a badge, the badge.
    if (rowUpdateBadge(entry.id) !== null) return null;
    const update = updates.forAgent(entry.id);
    const version = update.success_note_version ?? "";
    const noteKey = `${entry.id}:${version}`;
    if (!version || state.dismissedNoteKeys.has(noteKey)) return null;
    // The note outlives its run: once a newer run starts, the row's chat is that run's.
    const chatAgentId = update.verdict === "UPDATED" ? (update.chat_agent_id ?? "") : "";
    return m(
      "div",
      {
        class: "flex items-center gap-2 type-helper text-secondary",
        onclick: (event: MouseEvent) => event.stopPropagation(),
      },
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
    state.bulkError = null;
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
        const { error, isReconnectNeeded, isBugReportOffered } = bulkUpdateRefusal(kind, result);
        const refusal = {
          error,
          actions: isReconnectNeeded ? restartAppActions() : [],
          isBugReportOffered,
        };
        // The line lives in the bulk strip, which is gone once fewer than two
        // workspaces are left to update.
        if (updates.updatableAgentIds().length < 2) alertWithDetailsInLog(refusal.error);
        else state.bulkError = refusal;
      }
      m.redraw();
    });
  }

  /** The bulk strip, only when more than one machine auto-update will not
   * handle has an update available: one machine has its own card action, and
   * auto-update arms the rest itself. */
  function bulkUpdateActions(): m.Children {
    const updates = getAppContext().stores.updates;
    const agentIds = updates.updatableAgentIds();
    if (agentIds.length < 2) return null;
    return m(Notice, { extra: "mb-4", id: "landing-bulk-updates" }, [
      m("div", { class: "flex items-center justify-between gap-3 flex-wrap" }, [
        m("span", `${agentIds.length} workspaces have an update available.`),
        m("div", { class: "flex items-center gap-2" }, [
          m(
            Button,
            {
              variant: variantBesideErrors(
                "primary",
                state.bulkError === null
                  ? []
                  : [
                      {
                        message: state.bulkError.error.message,
                        actions: state.bulkError.actions,
                      },
                    ],
              ),
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
                "Some of these workspaces have no backups. Their updates keep a history of your files and can be " +
                  "rolled back afterwards, but there's no full copy of those workspaces to restore from. Go ahead?",
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
      state.bulkError !== null
        ? m("div", { class: "pt-2" }, [
            m(ErrorNotice, {
              id: "landing-bulk-updates-error",
              error: state.bulkError.error,
              actions: state.bulkError.actions,
              isBugReportOffered: state.bulkError.isBugReportOffered,
            }),
          ])
        : null,
    ]);
  }

  /** A card's icon action: the card's text colour at 20% on hover, labelled by the tooltip chip. */
  function cardIconButton(
    label: string,
    icon: IconName,
    onclick: () => void,
    options: { isDisabled?: boolean; attrs?: m.Attributes } = {},
  ): m.Children {
    const isDisabled = options.isDisabled ?? false;
    return m(
      "button",
      {
        type: "button",
        "aria-label": label,
        "aria-disabled": isDisabled ? "true" : undefined,
        "data-tooltip": label,
        class: CARD_ICON_BUTTON_CLASS + (isDisabled ? " opacity-40 cursor-default hover:bg-transparent" : ""),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          if (!isDisabled) onclick();
        },
        ...(options.attrs ?? {}),
      },
      m(Icon16, { name: icon }),
    );
  }

  /** The menu's Backups entry for a row, or null when its backups page is not offered. */
  function backupsMenuItem(entry: UiWorkspaceEntry, liveness: MindLiveness | ""): CardMenuItem | null {
    const backups = backupsControlFor(entry, liveness);
    if (!backups.isShown) return null;
    return {
      icon: "box",
      label: "Backups",
      isDisabled: !backups.isEnabled,
      title: backups.isEnabled ? undefined : backups.tooltip,
      onSelect: () => m.route.set(`/workspace/${entry.id}/backups`),
    };
  }

  /** The colour a card's text sits on: the cached wallpaper's sampled tone, else the accent. */
  function cardTone(entry: UiWorkspaceEntry): { tone: string; wallpaper: string | null } {
    const wallpaper = wallpaperUrl(entry.id, entry.wallpaper_version ?? "");
    const sampled = wallpaper === null ? null : wallpaperToneFor(wallpaper, () => m.redraw());
    return { tone: sampled ?? entry.accent, wallpaper };
  }

  /**
   * A card's frame (specs/home-screen-redesign, 4.7): the wallpaper or the
   * accent as its ground, its text re-based on that colour, a padded column
   * over it. Not a <button>, since it holds buttons.
   */
  function cardFrame(
    entry: UiWorkspaceEntry,
    options: { onOpen: (() => void) | null; attrs?: m.Attributes; extra?: string },
    children: m.Children,
  ): m.Children {
    const { tone, wallpaper } = cardTone(entry);
    const isInteractive = options.onOpen !== null;
    return m(
      Card,
      {
        layout: "block",
        padding: "none",
        interactive: false,
        extra:
          `relative flex flex-col border-0 ${CARD_MIN_HEIGHT_CLASS} text-left titlebar-surface card-ground px-3 pt-1.5 pb-2 pr-1.5 ` +
          (isInteractive ? "cursor-pointer transition-shadow hover:shadow-raised " : "cursor-default ") +
          (options.extra ?? ""),
        role: isInteractive ? "button" : undefined,
        tabindex: isInteractive ? 0 : undefined,
        style: `--titlebar-bg: ${tone}; --workspace-accent: ${entry.accent}; background-color: ${tone};`,
        "data-agent-id": entry.id,
        onclick: isInteractive ? options.onOpen : undefined,
        onkeydown: isInteractive
          ? (event: KeyboardEvent) => {
              // Only the card's own key, not one bubbling up from a button it holds.
              if (event.target !== event.currentTarget) return;
              if (event.key !== "Enter" && event.key !== " ") return;
              event.preventDefault();
              options.onOpen?.();
            }
          : undefined,
        ...(options.attrs ?? {}),
      },
      [
        m("div", { "aria-hidden": "true", class: "absolute inset-0 overflow-hidden rounded-[inherit]" }, [
          wallpaper === null
            ? null
            : m("div", {
                class: "absolute inset-0",
                "data-wallpaper": wallpaper,
                style: `background-image: url("${wallpaper}"); background-size: cover; background-position: center;`,
              }),
          wallpaper === null
            ? null
            : m("div", {
                class: "absolute inset-0",
                style: `background: linear-gradient(to bottom, color-mix(in srgb, ${tone} 70%, transparent) 0%, color-mix(in srgb, ${tone} 35%, transparent) 30%, transparent 55%);`,
              }),
        ]),
        m("div", { class: "relative flex flex-1 flex-col gap-2" }, children),
      ],
    );
  }

  function cardName(name: string, extra = "text-primary"): m.Children {
    return m("span", { class: `text-base leading-[1.333] font-semibold truncate flex-1 ${extra}` }, name);
  }

  function cardTop(name: string, actions: m.Children, nameExtra?: string): m.Children {
    return m("div", { class: "flex items-center gap-2.5" }, [
      cardName(name, nameExtra),
      m("div", { class: "flex shrink-0 items-center" }, actions),
    ]);
  }

  function cardBadges(children: m.Children): m.Children {
    return m("div", { class: "mt-auto flex flex-wrap items-center gap-1.5" }, children);
  }

  /** A live owned workspace: its name on its wallpaper, one run control, Open in new window, the menu, every status. */
  function workspaceCard(entry: UiWorkspaceEntry): m.Children {
    const { stores } = getAppContext();
    const discoveryHealth = stores.health.discoveryHealth;
    // Nothing is arriving to correct a frozen reading, so the card reports
    // "Status unknown" rather than showing the last one as if it were current.
    const liveness =
      (entry.supports_shutdown ?? false) && isMachineStateKnown(discoveryHealth)
        ? displayedLivenessOf(entry)
        : ("UNKNOWN" as MindLiveness);
    const controls = mindControlsFor(entry, liveness, discoveryHealth);
    const keyChip = keyStateChipFor(entry.key_state ?? "");
    const isOpenable = isEntryOpenable(entry);
    const isMoving = liveness === "STARTING" || liveness === "STOPPING";
    const supportsShutdown = entry.supports_shutdown ?? false;
    // One run control (specs/home-screen-redesign, 4.7): Start, Stop, or Restart.
    const runControl: { label: string; icon: IconName; run: () => void; isDisabled: boolean } | null = !supportsShutdown
      ? { label: "Restart workspace", icon: "restart", run: () => restartMind(entry), isDisabled: false }
      : controls.isStopShown
        ? { label: "Stop workspace", icon: "pause", run: () => stopMind(entry), isDisabled: false }
        : controls.isStartShown || isMoving
          ? {
              label: liveness === "STOPPING" ? "Stopping…" : liveness === "STARTING" ? "Starting…" : "Start workspace",
              icon: "play",
              run: () => startMind(entry),
              isDisabled: isMoving,
            }
          : null;
    const backups = backupsMenuItem(entry, liveness);
    const facts: CardMenuFact[] = entry.provider_label ? [{ label: "Host:", value: entry.provider_label }] : [];
    const menuItems: CardMenuItem[] = [];
    if (isOpenable)
      menuItems.push({ icon: "arrow-up-right", label: "Open in new window", onSelect: () => openInNewWindow(entry) });
    if (runControl !== null)
      menuItems.push({
        icon: runControl.icon,
        label: runControl.label,
        onSelect: runControl.run,
        isDisabled: runControl.isDisabled,
      });
    if (backups !== null) menuItems.push(backups);
    menuItems.push({ icon: "settings", label: "Workspace settings", onSelect: () => openSettings(entry) });
    const note = updatedNote(entry);
    return cardFrame(
      entry,
      { onOpen: isOpenable ? () => rowClick(entry) : null, attrs: { "data-row-kind": "live" } },
      [
        cardTop(entry.name, [
          runControl === null
            ? null
            : cardIconButton(runControl.label, runControl.icon, runControl.run, {
                isDisabled: runControl.isDisabled,
                attrs: { "data-run-control": runControl.icon },
              }),
          isOpenable
            ? cardIconButton("Open in new window", "arrow-up-right", () => openInNewWindow(entry), {
                attrs: { "data-card-action": "new-window" },
              })
            : null,
          m(CardMenu, { facts, items: menuItems }),
        ]),
        cardBadges([
          keyChip === null
            ? null
            : badge(keyChip.label, { "data-tooltip": keyChip.tooltip, extra: "landing-key-state-chip" }),
          supportsShutdown ? livenessBadge(liveness, entry.stop_kind ?? "") : null,
          healthBadge(entry, liveness),
          updateBadge(entry),
        ]),
        note,
      ],
    );
  }

  function destroyCard(
    agentId: string,
    name: string,
    accent: string,
    destroyStatus: string,
  ): m.Children {
    const entry: UiWorkspaceEntry = { id: agentId, name, accent };
    return cardFrame(
      entry,
      { onOpen: () => m.route.set(`/destroying/${agentId}`), attrs: { "data-row-kind": "destroying" } },
      [
        cardTop(name, [], "text-secondary"),
        cardBadges([
          destroyStatus === "failed"
            ? badge("Removal failed")
            : badge(destroyStatus === "confirming" ? "Confirming removal…" : "Destroying…"),
        ]),
      ],
    );
  }

  function createAttemptCard(entry: UiWorkspaceEntry): m.Children {
    const label =
      entry.create_attempt_state === "creating"
        ? "Creating…"
        : entry.create_attempt_state === "interrupted"
          ? "Interrupted"
          : "Create failed";
    return cardFrame(
      entry,
      { onOpen: () => m.route.set(`/creating/${entry.id}`), attrs: { "data-row-kind": "create-attempt" } },
      [cardTop(entry.name, [], "text-secondary"), cardBadges([badge(label)])],
    );
  }

  /** The chip of a remote row whose access setup failed: a button, since the
   * failure's detail is more than a tooltip holds, opening its own dialog. */
  function remoteErrorChip(
    entry: UiWorkspaceEntry,
    chip: RemoteStateChip,
    error: UserFacingError,
    remoteState: string,
  ): m.Children {
    return m(
      "button",
      {
        type: "button",
        class: homeBadgeClass("alarm", "cursor-pointer border-0"),
        "data-badge-tone": "alarm",
        "data-tooltip": chip.tooltip,
        "data-remote-state": remoteState,
        onclick: () => {
          showErrorDialog({
            title: `Can't reach "${entry.name}" from here`,
            error,
            workspaceAgentId: entry.id,
          });
        },
      },
      chip.label,
    );
  }

  /** A workspace known only from a synced record: where it lives, its access state, and a way to drop it. */
  function remoteCard(entry: UiWorkspaceEntry): m.Children {
    if (state.removedHostIds.has(entry.host_id ?? "")) return null;
    const tile = getAppContext().stores.workspaces.remoteWorkspaceStates[entry.id] ?? { state: "" };
    const chip = remoteStateChipFor(tile);
    const backups = backupsMenuItem(entry, "");
    const items: CardMenuItem[] = [];
    if (backups !== null) items.push(backups);
    items.push({
      icon: "close",
      label: "Remove from this list",
      id: "remove",
      onSelect: () => removeRemoteRecord(entry),
    });
    return cardFrame(
      entry,
      { onOpen: null, attrs: { "data-row-kind": "remote" }, extra: "opacity-60" },
      [
        cardTop(entry.name, [
          cardIconButton("Remove from this list", "close", () => removeRemoteRecord(entry)),
          m(CardMenu, { items }),
        ]),
        cardBadges([
          badge(remoteLocationBadgeFor(entry)),
          chip === null
            ? null
            : chip.error !== null
              ? remoteErrorChip(entry, chip, chip.error, tile.state)
              : badge(chip.label, { "data-tooltip": chip.tooltip, "data-remote-state": tile.state }),
        ]),
      ],
    );
  }

  /** Run one shared-row action, holding the row's buttons while it is in flight and showing a refusal in place. */
  function runSharedAction(shareKey: string, action: () => Promise<unknown>): void {
    if (state.sharedBusyKeys.has(shareKey)) return;
    state.sharedBusyKeys.add(shareKey);
    state.sharedError = "";
    action()
      .catch((error: unknown) => {
        state.sharedError = sharedActionErrorMessage(error);
      })
      .finally(() => {
        state.sharedBusyKeys.delete(shareKey);
        m.redraw();
      });
  }

  /** Open a shared target through the shell; its refusal surfaces through sharedActionErrorMessage. */
  function openThroughShell(shareKey: string, app: string | null): Promise<void> {
    return getAppContext().shell.openSharedWorkspace(shareKey, app);
  }

  function openShared(entry: UiWorkspaceEntry, app: string | null): void {
    if (!isSharedEntry(entry)) return;
    runSharedAction(entry.share.share_key, () => openThroughShell(entry.share.share_key, app));
  }

  /** Retry after a verdict (specs/shared-with-me, 4.7): forget it, then open the shell target again. */
  function retryShared(entry: UiWorkspaceEntry): void {
    if (!isSharedEntry(entry)) return;
    const { shell } = getAppContext();
    const key = entry.share.share_key;
    runSharedAction(key, async () => {
      await sharedWithMeApi.retry(shell.accountId, key);
      await openThroughShell(key, null);
    });
  }

  function reportShared(entry: UiWorkspaceEntry): void {
    if (!isSharedEntry(entry)) return;
    if (!window.confirm(REPORT_CONFIRMATION)) return;
    const { shell } = getAppContext();
    runSharedAction(entry.share.share_key, () => sharedWithMeApi.report(shell.accountId, entry.share.share_key));
  }

  /** Decline the share; an invite card (`isHeldInPlace`) keeps its place, reading Declined with Undo, until the tab is left. */
  function declineShared(entry: UiWorkspaceEntry, isHeldInPlace: boolean): void {
    if (!isSharedEntry(entry)) return;
    const { shell } = getAppContext();
    const key = entry.share.share_key;
    if (isHeldInPlace) state.pendingDeclinedKeys.add(key);
    runSharedAction(key, () => sharedWithMeApi.decline(shell.accountId, key));
  }

  function undoDeclineShared(entry: UiWorkspaceEntry): void {
    if (!isSharedEntry(entry)) return;
    const { shell } = getAppContext();
    const key = entry.share.share_key;
    runSharedAction(key, () => sharedWithMeApi.undoDecline(shell.accountId, key));
  }

  function removeShared(entry: UiWorkspaceEntry): void {
    if (!isSharedEntry(entry)) return;
    const { shell } = getAppContext();
    runSharedAction(entry.share.share_key, () => sharedWithMeApi.remove(shell.accountId, entry.share.share_key));
  }

  function addSharedLink(): void {
    const link = state.sharedLinkDraft.trim();
    if (link === "") return;
    const { shell } = getAppContext();
    runSharedAction("add-link", async () => {
      await sharedWithMeApi.addLink(shell.accountId, link, "");
      state.sharedLinkDraft = "";
    });
  }

  /** The share key the deep link named (`?shared=`, specs/shared-with-me 4.11): its card is marked and scrolled into view once. */
  function selectedSharedKey(): string | null {
    return m.route.param("shared") || null;
  }

  function sharedRowElementId(shareKey: string): string {
    return `shared-with-me-row-${shareKey}`;
  }

  /** Bring the deep link's card into view the first time it renders; a later redraw leaves the scroll alone. */
  function scrollSelectedSharedRowIntoView(): void {
    const selected = selectedSharedKey();
    if (selected === null || selected === state.scrolledSharedKey) return;
    if (typeof document === "undefined") return;
    const row = document.getElementById(sharedRowElementId(selected));
    if (row === null) return;
    state.scrolledSharedKey = selected;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /** The deep link's share decides the tab once (specs/home-screen-redesign, 4.3). */
  function pickTabForDeepLink(): void {
    const selected = selectedSharedKey();
    if (selected === null || selected === state.deepLinkTabKey) return;
    const entry = getAppContext().stores.workspaces.sharedEntryByShareKey(selected);
    if (entry === null || !isSharedEntry(entry)) return;
    state.deepLinkTabKey = selected;
    state.tab = homeTabOfShare(entry.share);
    // A declined invitation files under the collapsed disclosure, whose cards
    // are not drawn while it is closed: open it so the card is there to land on.
    if (isDeclinedInvitation(entry.share)) state.isDeclinedOpen = true;
  }

  /** The shared-row attrs every card and icon of a share carries: its key, the deep link's mark, and its scroll id. */
  function sharedAttrs(share: UiSharedWorkspace): m.Attributes {
    return {
      id: sharedRowElementId(share.share_key),
      "data-share-key": share.share_key,
      "data-share-selected": selectedSharedKey() === share.share_key ? "true" : undefined,
    };
  }

  /** The owner's chip on a guest card: their face and "<owner>'s workspace", or "Pasted link". */
  function ownerChip(share: UiSharedWorkspace): m.Children {
    if (share.is_manual) return badge("Pasted link", { "data-badge": "status" });
    return m(
      HomeBadge,
      { tone: "neutral", "data-badge": "owner", extra: "min-w-0 pl-0.5" },
      [
        m(Avatar, { src: share.owner.profile_picture_url ?? null, label: ownerLabel(share), size: 14 }),
        m("span", { class: "truncate" }, `${ownerLabel(share)}'s workspace`),
      ],
    );
  }

  /** The menu items a share offers besides opening: retry, decline or undo, report, remove. An invite card's
   * (`isInviteCard`) decline holds the card in place. */
  function sharedMenuItems(
    entry: UiWorkspaceEntry,
    share: UiSharedWorkspace,
    isRetryShown: boolean,
    isInviteCard = false,
  ): CardMenuItem[] {
    const isBusy = state.sharedBusyKeys.has(share.share_key);
    const items: CardMenuItem[] = [];
    if (isRetryShown)
      items.push({
        icon: "restart",
        label: "Retry",
        id: "retry",
        isDisabled: isBusy || share.status === "declined",
        onSelect: () => retryShared(entry),
      });
    if (!share.is_manual) {
      if (share.status === "declined")
        items.push({ icon: "undo", label: "Undo decline", id: "undo-decline", isDisabled: isBusy, onSelect: () => undoDeclineShared(entry) });
      else
        items.push({
          icon: "close",
          label: "Decline",
          id: "decline",
          isDisabled: isBusy,
          onSelect: () => declineShared(entry, isInviteCard),
        });
      items.push({ icon: "bug", label: "Report as unwanted", id: "report", isDisabled: isBusy, onSelect: () => reportShared(entry) });
    }
    items.push({ icon: "close", label: "Remove from this list", id: "remove", isDisabled: isBusy, onSelect: () => removeShared(entry) });
    return items;
  }

  /** Someone else's workspace, as a guest sees it: whose it is and a way in, with its liveness and last verdict. */
  function guestCard(entry: UiWorkspaceEntry): m.Children {
    if (!isSharedEntry(entry)) return null;
    const share = entry.share;
    const isBusy = state.sharedBusyKeys.has(share.share_key);
    const liveness = isShareLive(share, Date.now());
    const verdict = verdictLabel(share);
    const openableTargets = share.targets.filter((target) => target.link);
    // A share whose tunnel reads as down is not opened on a click (specs/shared-with-me, 4.7): Retry is the override.
    const isOffline = liveness === false;
    const canOpen = openableTargets.length > 0 && !isBusy && !isOffline;
    const menuItems: CardMenuItem[] = openableTargets.map((target) => ({
      icon: "arrow-up-right" as const,
      label: target.app === "system_interface" ? "Open" : `Open ${target.display_name ?? target.app}`,
      id: `open:${target.app}`,
      isDisabled: isBusy || isOffline,
      onSelect: () => openShared(entry, openTargetApp(target)),
    }));
    return cardFrame(
      entry,
      {
        onOpen: canOpen ? () => openShared(entry, null) : null,
        attrs: { ...sharedAttrs(share), "data-row-kind": "shared" },
      },
      [
        cardTop(entry.name, [m(CardMenu, { items: [...menuItems, ...sharedMenuItems(entry, share, isRetryOffered(share, isOffline))] })]),
        cardBadges([
          ownerChip(share),
          isBusy ? badge(CONNECTING_LABEL, { "data-badge": "connecting" }) : null,
          liveness === null ? null : badge(liveness ? "Online" : "Offline", { "data-badge": "liveness" }),
          verdict === null ? null : badge(verdict, { "data-share-verdict": share.verdict }),
          openableTargets.length === 0
            ? m("span", { class: "type-helper text-secondary", "data-share-no-address": "true" }, NO_ADDRESS_MESSAGE)
            : null,
        ]),
      ],
    );
  }

  /** The 40px tile an invite card leads with: the workspace's ground, or the app's monogram, with the owner's face. */
  function inviteThumb(entry: UiWorkspaceEntry, share: UiSharedWorkspace, app: SharedApp | null): m.Children {
    const tile =
      app === null
        ? m("span", {
            "aria-hidden": "true",
            class: "block size-10 rounded-[25%] shadow-[0_1px_4px_0_rgb(0_0_0/0.2)]",
            style: `background-color: ${entry.accent};`,
          })
        : m(MonogramTile, { name: app.name, extra: "size-10 rounded-[32%] text-sm shadow-[0_1px_4px_0_rgb(0_0_0/0.2)]" });
    return m("span", { class: "relative block shrink-0 size-10" }, [
      tile,
      m(
        "span",
        { class: "absolute -right-1 -bottom-1 rounded-full bg-surface-primary p-px" },
        m(Avatar, { src: share.owner.profile_picture_url ?? null, label: ownerLabel(share), size: 16 }),
      ),
    ]);
  }

  /**
   * One invitation (specs/home-screen-redesign, 4.6): what it is, who from,
   * and its answer. Declined, it stays put reading Declined with Undo until the
   * tab is left.
   */
  function inviteCard(entry: UiWorkspaceEntry): m.Children {
    if (!isSharedEntry(entry)) return null;
    const share = entry.share;
    // A workspace invitation stays a workspace even when one of its apps is granted in its own right.
    const firstApp = isWorkspaceShare(share) ? null : (sharedAppsOf(entry)[0] ?? null);
    const isDeclined = share.status === "declined";
    const isBusy = state.sharedBusyKeys.has(share.share_key);
    const target = primaryTarget(share);
    const canJoin = target !== null && !!target.link && !isBusy;
    const name = firstApp === null ? entry.name : firstApp.name;
    const what = firstApp === null ? "a workspace" : "an app";
    const when = relativeTimeLabel(share.latest_grant_at ?? "", Date.now());
    const slot = "w-[72px]";
    return m(
      "div",
      {
        ...sharedAttrs(share),
        "data-invite-answer": isDeclined ? "declined" : undefined,
        class:
          "flex min-h-16 items-center gap-3 py-3 px-3 text-left rounded-lg border border-subtle bg-surface-primary card-arrive",
      },
      [
        m("span", { class: "shrink-0 " + (isDeclined ? "opacity-40" : "") }, inviteThumb(entry, share, firstApp)),
        m("div", { class: "min-w-0 flex-1" }, [
          m("div", { class: "truncate type-label " + (isDeclined ? "text-tertiary" : "text-primary") }, name),
          m(
            "div",
            { class: "truncate type-helper text-secondary" },
            isDeclined ? "Declined" : `${ownerLabel(share)} invited you to ${what}${when ? ` · ${when}` : ""}`,
          ),
        ]),
        m("div", { class: "flex shrink-0 items-center gap-1.5" }, [
          m("div", { class: slot }, [
            isDeclined
              ? null
              : m(
                  Button,
                  {
                    variant: "secondary",
                    block: true,
                    disabled: isBusy,
                    "data-invite-action": "decline",
                    onclick: () => declineShared(entry, true),
                  },
                  "Decline",
                ),
          ]),
          m("div", { class: slot }, [
            isDeclined
              ? m(
                  Button,
                  { variant: "ghost", block: true, disabled: isBusy, "data-invite-action": "undo", onclick: () => undoDeclineShared(entry) },
                  "Undo",
                )
              : m(
                  Button,
                  {
                    variant: "primary",
                    block: true,
                    disabled: !canJoin,
                    title: canJoin || isBusy ? undefined : NO_ADDRESS_MESSAGE,
                    "data-invite-action": "join",
                    onclick: () =>
                      openShared(entry, target === null ? null : openTargetApp(target)),
                  },
                  isBusy ? CONNECTING_LABEL : "Join",
                ),
          ]),
          m(CardMenu, { items: sharedMenuItems(entry, share, false, true), buttonExtra: "text-secondary" }),
        ]),
      ],
    );
  }

  /** Studio settings, in the first section head of the active tab (specs/home-screen-redesign, D9). */
  function studioSettingsButton(): m.Children {
    const { shell } = getAppContext();
    return m(
      TitlebarButton,
      {
        id: "landing-minds-settings",
        variant: "crumb",
        tone: "muted",
        extra: "gap-1.5",
        "aria-label": "Studio settings",
        onclick: () => m.route.set("/settings", shell.overlayQuery()),
      },
      [m(Icon16, { name: "settings", extra: "shrink-0" }), m("span", { class: "type-label" }, "Studio settings")],
    );
  }

  function sectionHead(title: string, options: { isFirst: boolean; actions?: m.Children } = { isFirst: false }): m.Children {
    return m("div", { class: "flex items-center justify-between mb-4 min-h-8 gap-2" }, [
      m("h2", { class: "type-heading text-primary" }, title),
      m("div", { class: "flex items-center gap-2" }, [options.actions ?? null, options.isFirst ? studioSettingsButton() : null]),
    ]);
  }

  function emptyBox(text: string, attrs: m.Attributes = {}): m.Children {
    return m(
      "div",
      { class: "rounded-lg border border-dashed border-subtle py-10 text-center type-body text-tertiary", ...attrs },
      text,
    );
  }

  /**
   * The grid's last card: Add workspace, a dashed card the size of the
   * others, or Sign in on a signed-out account's hub, where nothing new can be
   * made until it signs in again.
   */
  function createCard(): m.Children {
    const { shell } = getAppContext();
    const cardClass =
      `${CARD_MIN_HEIGHT_CLASS} flex cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-dashed ` +
      "border-[var(--c-text-secondary)] type-label text-tertiary no-underline transition-[color,border-color,box-shadow] " +
      "hover:border-[var(--c-text-primary)] hover:text-primary hover:shadow-raised bg-transparent";
    if (shell.isAccountSignedOut()) {
      return m(
        "button",
        {
          type: "button",
          id: "landing-sign-in",
          class: cardClass,
          disabled: webLogin.isOpen,
          onclick: () =>
            void webLogin.start(
              "Sign in again to create workspaces under this account.",
            ),
        },
        [m(Icon16, { name: "user" }), "Sign in"],
      );
    }
    return m(
      "a",
      { id: "landing-create", class: cardClass, ...routeLinkAttrs(shell.hubRoute("/create")) },
      [m(Icon16, { name: "plus" }), "Add workspace"],
    );
  }

  /** The input a link given out of band goes into; the Workspaces and Apps tabs each carry one, with their own ids.
   * Both add the same way: a pasted link is sorted onto its tab by its origin label (specs/home-screen-redesign, D7). */
  function pastedLinkInput(kind: "workspace" | "app"): m.Children {
    const isAddBusy = state.sharedBusyKeys.has("add-link");
    return m("div", { class: "flex items-center gap-2 mt-3" }, [
      m("input", {
        type: "url",
        id: kind === "workspace" ? "shared-with-me-link" : "shared-with-me-app-link",
        placeholder: kind === "workspace" ? "Paste a workspace link you were given" : "Paste an app link you were given",
        class: "h-[34px] px-3 rounded-full type-body bg-fill-subtle text-primary flex-1 min-w-0",
        value: state.sharedLinkDraft,
        oninput: (event: InputEvent) => {
          state.sharedLinkDraft = (event.target as HTMLInputElement).value;
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter") addSharedLink();
        },
      }),
      m(
        Button,
        {
          variant: "secondary",
          id: kind === "workspace" ? "shared-with-me-add-link" : "shared-with-me-add-app-link",
          disabled: isAddBusy || state.sharedLinkDraft.trim() === "",
          onclick: addSharedLink,
        },
        "Add link",
      ),
    ]);
  }

  function sharedErrorLine(): m.Children {
    return state.sharedError
      ? m("p", { class: "type-helper text-important mt-2", role: "alert", id: "shared-with-me-error" }, state.sharedError)
      : null;
  }

  /** "Workspaces shared with you": the workspace shares that are neither invitations nor declined, and the pasted-link input. */
  function sharedWorkspacesSection(workspaceShares: readonly UiWorkspaceEntry[]): m.Children {
    return m("section", { id: "landing-shared-with-me", "data-home-section": "shared-workspaces" }, [
      sectionHead("Workspaces shared with you"),
      workspaceShares.length > 0
        ? m("div", { class: CARD_GRID_CLASS }, workspaceShares.map((entry) => guestCard(entry)))
        : emptyBox("Nothing has been shared with you yet."),
      sharedErrorLine(),
      pastedLinkInput("workspace"),
    ]);
  }

  function ownAppsSection(liveEntries: readonly UiWorkspaceEntry[]): m.Children {
    const { shell } = getAppContext();
    const icons: m.Children[] = [];
    for (const entry of liveEntries) {
      // The icon follows the card: a workspace whose card is not clickable has no app to open either.
      const isOpenable = isEntryOpenable(entry);
      for (const app of entry.apps ?? []) {
        icons.push(
          m(HomeAppIcon, {
            key: `${entry.id}:${app.name}`,
            name: app.display_name,
            subtitle: entry.name,
            iconUrl: appIconUrl(entry.id, app),
            isDisabled: !isOpenable,
            title: isOpenable ? undefined : BLOCKED_WORKSPACE_MESSAGE,
            onOpen: () => shell.openWorkspaceApp(entry, app, displayedLivenessOf(entry)),
            "data-agent-id": entry.id,
            "data-app-name": app.name,
          }),
        );
      }
    }
    return m("section", { "data-home-section": "own-apps" }, [
      sectionHead("Your apps", { isFirst: true }),
      icons.length > 0
        ? m("div", { class: "flex flex-wrap gap-x-4 gap-y-6" }, icons)
        : emptyBox("No apps yet. Describe an app in a workspace's chat and it appears here.", { id: "landing-no-own-apps" }),
    ]);
  }

  function sharedAppIcon(app: SharedApp): m.Children {
    const { entry, share, target } = app;
    const hasLink = !!target.link;
    const liveness = isShareLive(share, Date.now());
    const isOffline = liveness === false;
    const isBusy = state.sharedBusyKeys.has(share.share_key);
    // A share with several targets is several icons; the row id the deep link scrolls to names its first.
    const attrs = sharedAttrs(share);
    return m(HomeAppIcon, {
      key: `${share.share_key}:${target.app}`,
      name: app.name,
      iconUrl: null,
      owner: share.is_manual ? undefined : { label: ownerLabel(share), pictureUrl: share.owner.profile_picture_url ?? null },
      isDisabled: !hasLink || isOffline || isBusy,
      title: isBusy ? CONNECTING_LABEL : !hasLink ? NO_ADDRESS_MESSAGE : isOffline ? "Could not reach the workspace" : undefined,
      onOpen: () => openShared(entry, openTargetApp(target)),
      corner: m(CardMenu, { items: sharedMenuItems(entry, share, isRetryOffered(share, isOffline)), buttonExtra: "size-6 text-secondary" }),
      ...attrs,
      id: share.targets[0] === target ? attrs.id : undefined,
      "data-app-name": target.app,
    });
  }

  /** "Apps shared with you": the apps of shares that are neither invitations nor declined (every target of an app
   * share, and the directly granted apps of a workspace share), and the pasted-link input, which stands in for an
   * empty state (an app link someone gave you is added here). */
  function sharedAppsSection(sharedApps: readonly SharedApp[]): m.Children {
    return m("section", { "data-home-section": "shared-apps" }, [
      sectionHead("Apps shared with you"),
      sharedApps.length > 0
        ? m("div", { class: "flex flex-wrap gap-x-4 gap-y-6" }, sharedApps.map((app) => sharedAppIcon(app)))
        : null,
      sharedErrorLine(),
      pastedLinkInput("app"),
    ]);
  }

  function invitationsSection(open: readonly UiWorkspaceEntry[], settledDeclined: readonly UiWorkspaceEntry[]): m.Children {
    return m("section", { id: "landing-invitations", "data-home-section": "invitations" }, [
      sectionHead("Invitations", { isFirst: true }),
      open.length > 0
        ? m("div", { class: "flex flex-col gap-2" }, open.map((entry) => inviteCard(entry)))
        : emptyBox("No invitations waiting.", { id: "landing-no-invitations" }),
      sharedErrorLine(),
      settledDeclined.length > 0
        ? m(
            "div",
            { class: "mt-6" },
            m(Disclosure, {
              id: "landing-declined-invitations",
              isOpen: state.isDeclinedOpen,
              onToggle: () => {
                state.isDeclinedOpen = !state.isDeclinedOpen;
              },
              summary: `Declined invitations (${settledDeclined.length})`,
            }, m("div", { class: "flex flex-col gap-2" }, settledDeclined.map((entry) => inviteCard(entry)))),
          )
        : null,
    ]);
  }

  function pickTab(next: HomeTab): void {
    // Answered invitations hold their place until the tab is left, then go.
    if (state.tab === "invitations" && next !== "invitations") state.pendingDeclinedKeys.clear();
    state.tab = next;
    rememberHomeTab(next);
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

  /** A row of the panel: its one-line header (name, chip, controls), and
   * under it, across the card's width, what the row's problem is with the raw
   * error behind See details. A failed check offers a report; a login to set
   * up is the user's step, confirmed by the notice's own Check again. */
  function panelRow(
    rowAttrs: Record<string, string>,
    header: m.Children[],
    problem: RowProblem | null,
    problemId: string,
    problemActions: ErrorAction[] = [],
  ): m.Children {
    return m(Card, { padding: "tight", ...rowAttrs }, [
      m("div", { class: "flex items-center gap-1.5" }, header),
      problem === null
        ? null
        : m(ErrorNotice, {
            id: problemId,
            error: problem,
            tone: problem.isFailedCheck ? "error" : "warn",
            isBugReportOffered: problem.isFailedCheck,
            actions: problemActions,
            extra: "mt-1",
          }),
    ]);
  }

  function providerRow(entry: UiProviderEntry, subject: "account" | "provider"): m.Children {
    return panelRow(
      { "data-provider-name": entry.name },
      [
        m("span", { class: "font-semibold text-primary" }, entry.name),
        entry.backend
          ? m("span", { class: "type-helper text-secondary" }, entry.backend)
          : null,
        statusChipBadge(providerStatusChipFor(entry.status)),
      ],
      providerRowProblemFor(entry, subject),
      `provider-problem-${entry.name}`,
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
    return panelRow(
      { "data-connection-instance": entry.instance },
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
        m("span", { class: "flex-1" }),
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
      connectionRowProblemFor(entry),
      `connection-problem-${entry.instance}`,
      entry.status === "PROFILE_MISSING" || entry.status === "ERROR"
        ? [
            {
              id: `connection-recheck-${entry.instance}`,
              label: "Check again",
              isBusy: isRechecking,
              isDisabled: isRemoving,
              tooltip: isRemoving
                ? "Removing this connection"
                : entry.status === "PROFILE_MISSING"
                  ? "Check again whether the login is set up on this computer"
                  : "Check this connection again",
              onclick: () => recheckConnection(entry),
            },
          ]
        : [],
    );
  }

  function accountSection(entry: UiAccountProvidersEntry): m.Children {
    const owner: ConnectionOwnerOption = {
      userId: entry.user_id,
      label: accountSectionTitleFor(entry),
    };
    const rows: m.Children[] = [];
    if (entry.imbue_cloud !== null) rows.push(providerRow(entry.imbue_cloud, "account"));
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
        entries.map((entry) => providerRow(entry, "provider")),
      ),
    ]);
  }

  function providersPanel(): m.Children {
    const providersStore = getAppContext().stores.providers;
    return m(
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
              providersSummaryFor(providersStore.rowCount(), providersStore.attentionCount()),
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
    );
  }

  // Every input (the extras landing, the workspace list, the onboarding copy)
  // arrives with a redraw, so the hook sees each change; view() stays free of
  // navigation.
  function redirectToStartFlowIfDue(): void {
    const workspaces = getAppContext().stores.workspaces;
    // Rows of the account's own under any account, or a workspace discovery
    // has not re-listed yet; someone else's shares are no workspace of this
    // install's.
    const hasWorkspacesOnInstall =
      ownedWorkspaceEntries(workspaces.workspaces).length > 0 ||
      Object.values(workspaces.workspaceCountByAccountId).some((count) => count > 0) ||
      workspaces.restorableWorkspaceIds.length > 0;
    if (
      shouldRedirectToStartFlow(
        onboardingProgress.isComplete,
        hasWorkspacesOnInstall,
        state.extras,
      )
    ) {
      m.route.set("/start");
    }
  }

  return {
    oninit() {
      loadExtras(state);
      state.unsubscribe = getAppContext().stores.workspaces.onChanged(() =>
        loadExtras(state),
      );
      pickTabForDeepLink();
    },
    oncreate() {
      scrollSelectedSharedRowIntoView();
    },
    onupdate() {
      // The component instance survives a hub-to-hub navigation (same route
      // pattern, new account segment), so the account change is caught here.
      if (getAppContext().shell.accountId !== state.requestedExtrasAccountId) {
        loadExtras(state);
      }
      redirectToStartFlowIfDue();
      scrollSelectedSharedRowIntoView();
    },
    onremove() {
      state.unsubscribe?.();
    },
    view() {
      const { stores, shell } = getAppContext();
      pickTabForDeepLink();
      const allEntries = stores.workspaces.workspaces;
      // Someone else's workspaces are listed in their own sections, never as
      // this account's rows (specs/shared-with-me, S12).
      const entries = ownedWorkspaceEntries(allEntries);
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
      const remoteEntries = remoteTileEntries(
        entries,
        orphanedFailedDestroys.map((orphan) => orphan.agent_id),
        (agentId) => stores.workspaces.isDestroying(agentId),
      );
      const hasRows =
        liveEntries.length +
          orphanedFailedDestroys.length +
          createEntries.length +
          remoteEntries.length >
        0;
      // A hub-to-hub navigation keeps the previous account's extras until the
      // refetch lands, and their restorable answer is that account's.
      const windowExtras =
        state.extrasAccountId === shell.accountId ? extras : null;
      const isDiscovering =
        !hasRows &&
        !state.isExtrasFailed &&
        (windowExtras === null ||
          !windowExtras.is_discovery_complete ||
          windowExtras.has_restorable_workspaces);
      const isSignedIn = stores.accounts.isSignedIn(shell.accountId);

      const sharedRows = homeSharedRows(allEntries, state.pendingDeclinedKeys);
      const hasDeclined = sharedRows.pendingDeclined.length + sharedRows.settledDeclined.length > 0;
      const tabs = homeTabsFor(sharedRows.invitations.length, hasDeclined);
      const tab: HomeTab = tabs.some((candidate) => candidate.id === state.tab) ? state.tab : "workspaces";

      // A destroy in flight replaces its live card's content and takes the
      // workspace's apps off the Apps tab: the live row's status is what the
      // extras name it.
      const destroyingStatus = extras?.destroying_status_by_agent_id ?? {};
      // Only the tab up is built: a card's wallpaper is fetched and sampled
      // as it is drawn, so a hidden tab's cards would load images for nothing.
      function workspacesTabSections(): m.Children[] {
        const liveCards = liveEntries.map((entry) => {
          const status = destroyingStatus[entry.id];
          return status === undefined ? workspaceCard(entry) : destroyCard(entry.id, entry.name, entry.accent, status);
        });
        const workspaceCards: m.Children[] = [
          liveCards,
          orphanedFailedDestroys.map((orphan) =>
            destroyCard(orphan.agent_id, orphan.name, orphan.accent, "failed"),
          ),
          createEntries.map((entry) => createAttemptCard(entry)),
          remoteEntries.map((entry) => remoteCard(entry)),
        ];
        const yourWorkspaces = m("section", { "data-home-section": "own-workspaces" }, [
          sectionHead("Your workspaces", { isFirst: true }),
          isDiscovering
            ? m("p", { class: "text-tertiary text-center py-6", id: "landing-discovering" }, "Discovering workspaces...")
            : null,
          m("div", { class: CARD_GRID_CLASS }, [workspaceCards, createCard()]),
          m(
            "div",
            { class: "mt-3" },
            m(
              "a",
              {
                id: "landing-destroyed",
                class:
                  "inline-flex items-center gap-2 type-helper text-tertiary hover:text-primary no-underline",
                ...routeLinkAttrs(shell.hubRoute("/workspaces/destroyed")),
              },
              m("span", "Recently destroyed workspaces"),
            ),
          ),
        ]);
        return [
          yourWorkspaces,
          isSignedIn ? sharedWorkspacesSection(sharedRows.workspaceShares) : null,
          providersPanel(),
        ];
      }
      function appsTabSections(): m.Children[] {
        const ownAppEntries = liveEntries.filter((entry) => destroyingStatus[entry.id] === undefined);
        return [ownAppsSection(ownAppEntries), isSignedIn ? sharedAppsSection(sharedRows.sharedApps) : null];
      }
      function invitationsTabSections(): m.Children[] {
        // A declined card keeps the place its grant time gave it until the tab is left.
        const openInvitations = [...sharedRows.invitations, ...sharedRows.pendingDeclined].sort(byNewestGrant);
        return [invitationsSection(openInvitations, sharedRows.settledDeclined)];
      }

      return m(PageContainer, [
        state.isExtrasFailed
          ? m(ErrorNotice, {
              id: "landing-extras-error",
              error: {
                message:
                  "Could not load workspace details. Some rows may be missing or incomplete.",
                details: "",
              },
              actions: [
                {
                  label: "Retry",
                  onclick: () => loadExtras(state),
                },
              ],
            })
          : null,
        bulkUpdateActions(),
        extras !== null &&
        isUnlockBannerDue(extras, state.extrasAccountId, shell.accountId)
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
        hasRows || isDiscovering ? null : otherAccountsNotice(otherAccountsOfWindow()),
        m("div", { class: "mb-8" }, m(TabStrip, { tabs, selected: tab, onSelect: (id) => pickTab(id as HomeTab), label: "Home" })),
        m(
          "div",
          { class: "flex flex-col gap-12", "data-home-tab-panel": tab },
          tab === "workspaces" ? workspacesTabSections() : tab === "apps" ? appsTabSections() : invitationsTabSections(),
        ),
        m(CloudAccountsModal, {
          state: state.connectionModal,
          regionOptionsByBackend:
            state.formDefaults?.region_options_by_launch_mode ?? {},
          backends: state.formDefaults?.connection_backends ?? [],
          ownerOptions: getAppContext().stores.providers.connectionOwnerOptions(),
          // The panel re-renders from the next providers frame (the materializer bounces discovery).
          onAccountAdded: () => m.redraw(),
        }),
        m("div", { class: "h-20", "aria-hidden": "true" }),
      ]);
    },
  };
};
