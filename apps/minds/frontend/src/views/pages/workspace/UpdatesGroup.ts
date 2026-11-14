// The workspace Settings -> Updates tab: the one place a workspace's update is
// explained and acted on. What each state says is decided in
// models/updateSurfaces.ts; this draws it and runs the presses.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type {
  UiWorkspaceUpdate,
  WorkspaceAutoUpdateMode,
} from "../../../channel/messages";
import {
  KERNEL_FIX_DETAILS,
  KERNEL_FIX_RESTART_LINE,
  SECURITY_FIX_DETAILS,
  SECURITY_FIX_TITLE,
  UPDATE_COST_NOTES,
  isSituationSettledByLeaving,
  isSkipOffered,
  isSpecificVersionOffered,
  kernelCardFor,
  updateTabActionsFor,
  updateTabStatusFor,
} from "../../../models/updateSurfaces";
import type {
  KernelCard,
  UpdateStatusIcon,
  UpdateStatusTone,
  UpdateTabAction,
  UpdateTabStatus,
} from "../../../models/updateSurfaces";
import {
  devBuildTargetRef,
  displayVersion,
  formatClockMinute,
  recordedDispatchFailure,
  skipConfirmationLine,
  skippedVersionText,
  updateActionError,
  updateChatAgentIdFor,
} from "../../../models/updates";
import type {
  UpdateActionResult,
  UpdateDispatchError,
  UpdateSituation,
  WorkspaceAutoUpdate,
} from "../../../models/updates";
import { Button } from "../../components/Button";
import { Collapsible } from "../../components/Collapsible";
import { TextInput } from "../../components/FormControls";
import { SectionHeader } from "../../components/Layout";
import { Spinner } from "../../components/Spinner";
import {
  updateDispatchErrorNotice,
  updateDispatchShownError,
} from "../../components/UpdateDispatchErrorNotice";
import type { UpdateDispatchErrorOptions } from "../../components/UpdateDispatchErrorNotice";
import { variantBesideErrors } from "../../components/variantBesideErrors";
import type { ErrorAction } from "../../../models/errors";
import { AskAgentModel } from "../../../models/assistChat";
import { recoveryRoute } from "../../../models/create";
import { renderUpdateWindowPicker } from "../../components/UpdateWindowPicker";
import { settingsFailureLine } from "../../components/SettingsFailureLine";

export interface UpdatesGroupAttrs {
  agentId: string;
}

type Press = "now" | "schedule" | "cancel-schedule" | "skip";

/** Everything the tab holds between redraws for one machine. Kept per machine:
 * a route change between machines keeps this component instance, and a press
 * can still be out on the machine the reader just left. */
interface UpdatesGroupState {
  /** The press waiting on its answer, and the ref it named; its button spins. */
  pending: { press: Press; targetRef: string } | null;
  /** The press held for the go-ahead-without-backups question. */
  noBackupHeld: { press: "now" | "schedule"; targetRef: string } | null;
  isSkipConfirmOpen: boolean;
  /** Why the last press failed, in the app's words and the machine's. */
  error: UpdateDispatchError | null;
  /** An automatic-updates write is out; apart from `pending`, so neither can clear the other's. */
  isAutoUpdateWriting: boolean;
  /** Why the last automatic-updates write failed, said beside the choices
   * rather than the update, and the write its Try again sends again. */
  autoUpdateError: { error: UpdateDispatchError; retry: () => void } | null;
  isSecurityDetailsOpen: boolean;
  isSpecificVersionOpen: boolean;
  specificVersion: string;
  /** Changes on each arrival from another surface, so the status block flashes once. */
  flashKey: number;
  /** The kernel card's question: the first press's confirmation, or the go-ahead past the chats it named. */
  kernelPrompt: { kind: "confirm" } | { kind: "force"; message: string } | null;
  /** The kernel restart press waiting on its answer; its button spins. */
  isKernelPending: boolean;
  kernelError: UpdateDispatchError | null;
  isKernelDetailsOpen: boolean;
}

function freshState(): UpdatesGroupState {
  return {
    pending: null,
    noBackupHeld: null,
    isSkipConfirmOpen: false,
    error: null,
    isAutoUpdateWriting: false,
    autoUpdateError: null,
    isSecurityDetailsOpen: false,
    isSpecificVersionOpen: false,
    specificVersion: "",
    flashKey: 0,
    kernelPrompt: null,
    isKernelPending: false,
    kernelError: null,
    isKernelDetailsOpen: false,
  };
}

/** The colour the status block's arrival ring flashes in, and its icon's colour, by tone. */
const TONE_COLOR: Record<UpdateStatusTone, string> = {
  neutral: "var(--c-border-strong)",
  info: "var(--c-info)",
  warn: "var(--c-warning)",
  error: "var(--c-important)",
  success: "var(--c-success)",
};
const TONE_TEXT: Record<UpdateStatusTone, string> = {
  neutral: "text-secondary",
  info: "text-info",
  warn: "text-warning",
  error: "text-important",
  success: "text-success",
};

/** The glyph knocked out of each filled status icon, as stroke paths on a 24px grid. */
const STATUS_GLYPHS: Record<
  Exclude<UpdateStatusIcon, "spinner">,
  m.Children
> = {
  check: m("path", { d: "m8.5 12 2.5 2.5 4.5-5" }),
  "arrow-up": [
    m("path", { d: "m16 12-4-4-4 4" }),
    m("path", { d: "M12 16V8" }),
  ],
  clock: m("path", { d: "M12 7v5l3 2" }),
  question: [
    m("path", { d: "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" }),
    m("path", { d: "M12 17h.01" }),
  ],
  alert: [m("path", { d: "M12 7.5v5" }), m("path", { d: "M12 16.5h.01" })],
  user: [
    m("circle", { cx: "12", cy: "10", r: "3" }),
    m("path", { d: "M7 19.5V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v.5" }),
  ],
  slash: m("path", { d: "m8 16 8-8" }),
  power: [
    m("path", { d: "M12 7v5" }),
    m("path", { d: "M15.5 9a5 5 0 1 1-7 0" }),
  ],
};

/** A filled status icon: a disc in the tone's colour with the glyph knocked out in white. */
function statusIcon(icon: UpdateStatusIcon): m.Children {
  if (icon === "spinner") return m(Spinner, { size: "sm" });
  return m(
    "svg",
    { class: "w-5 h-5", viewBox: "0 0 24 24", "aria-hidden": "true" },
    m("circle", { cx: "12", cy: "12", r: "10", fill: "currentColor" }),
    m(
      "g",
      {
        fill: "none",
        stroke: "white",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      },
      STATUS_GLYPHS[icon],
    ),
  );
}

const SHIELD_ALERT_ICON = m(
  "svg",
  {
    class: "w-4 h-4 shrink-0",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  },
  [
    m("path", {
      d: "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z",
    }),
    m("path", { d: "M12 8v4" }),
    m("path", { d: "M12 16h.01" }),
  ],
);

/** This workspace's version and the app's, or the one version marked latest when they match. */
function versionJump(versions: UpdateTabStatus["versions"]): m.Children {
  if (versions === null) return null;
  const current = displayVersion(versions.current) || "unknown";
  const supported = displayVersion(versions.supported) || "unknown";
  if (current === supported) {
    return m(
      "span",
      {
        id: "ws-update-versions",
        class: "inline-flex items-center gap-1.5 type-helper min-w-0 max-w-[45%]",
        "data-tooltip": supported,
      },
      [
        m("span", { class: "font-mono text-primary truncate" }, supported),
        m(
          "span",
          { class: "rounded-full bg-fill-subtle px-1.5 text-tertiary" },
          "latest",
        ),
      ],
    );
  }
  return m(
    "span",
    {
      id: "ws-update-versions",
      // A branch or ref can be far longer than a release tag, so the workspace's side truncates, and so does the
      // supported side when it is a dev build's branch (a release tag stays whole); the tooltip has both whole.
      class: "inline-flex items-center gap-1 type-helper font-mono min-w-0 max-w-[45%]",
      "data-tooltip": `This workspace: ${current} · Supported by Imbue Studio: ${supported}`,
    },
    [
      m("span", { class: "text-tertiary truncate" }, current),
      m("span", { class: "text-tertiary shrink-0" }, "→"),
      m(
        "span",
        {
          id: "ws-update-versions-supported",
          class: `text-primary ${devBuildTargetRef(versions.supported) === "" ? "shrink-0" : "truncate"}`,
        },
        supported,
      ),
    ],
  );
}

/** The slash command the new machine's agent runs to bring this machine's work across. */
function migrateCommandFor(workspaceName: string): string {
  return `/migrate-workspace from ${workspaceName}`;
}

function noBackupConfirmPrompt(
  onConfirm: () => void,
  onCancel: () => void,
): m.Children {
  return m(
    "div",
    { id: "ws-update-no-backups", class: "flex flex-col gap-2" },
    [
      m(
        "p",
        { class: "type-helper text-primary" },
        "This workspace has no backups. The update keeps a history of your files and can be rolled back " +
          "afterwards, but there's no full copy of the workspace to restore from. Go ahead?",
      ),
      m("div", { class: "flex flex-wrap items-center gap-2" }, [
        m(
          Button,
          { variant: "primary", onclick: onConfirm },
          "Go ahead without backups",
        ),
        m(Button, { variant: "secondary", onclick: onCancel }, "Cancel"),
      ]),
    ],
  );
}

function skipConfirmPrompt(
  update: UiWorkspaceUpdate,
  isAutoUpdateEnabled: boolean,
  isSkipping: boolean,
  onConfirm: () => void,
  onCancel: () => void,
): m.Children {
  return m(
    "div",
    { id: "ws-update-skip-confirm", class: "flex flex-col gap-2" },
    [
      m(
        "p",
        { class: "type-helper text-primary" },
        skipConfirmationLine(update, isAutoUpdateEnabled),
      ),
      m("div", { class: "flex flex-wrap items-center gap-2" }, [
        m(
          Button,
          {
            variant: "primary",
            id: "update-confirm-skip",
            disabled: isSkipping,
            onclick: onConfirm,
          },
          isSkipping
            ? m(Spinner, { size: "sm" })
            : `Skip ${skippedVersionText(update)}`,
        ),
        m(
          Button,
          { variant: "secondary", disabled: isSkipping, onclick: onCancel },
          "Cancel",
        ),
      ]),
    ],
  );
}

/** What failed, per press, for a refusal that carries no sentence of its own. */
const PRESS_FAILURE: Record<Press, string> = {
  now: "Couldn't start the update",
  schedule: "Couldn't schedule the update",
  "cancel-schedule": "Couldn't cancel the scheduled update",
  skip: "Couldn't skip this update",
};
const SIGN_IN_FAILURE = "Couldn't open a chat in this workspace to sign in";
const AUTO_UPDATE_FAILURE = "Couldn't save this workspace's automatic updates";
const KERNEL_FIX_FAILURE = "Couldn't restart this machine onto the fixed kernel";

/** "Turn on auto-updates": green rather than the inverse primary, since yes is the good outcome. */
const GREEN_BUTTON_CLASS =
  "inline-flex items-center justify-center h-[34px] px-3 rounded-md type-label cursor-pointer " +
  "border border-transparent bg-success text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-default";

export function UpdatesGroup(): m.Component<UpdatesGroupAttrs> {
  const stateByAgentId = new Map<string, UpdatesGroupState>();
  /** The machine last drawn, and the situation the reader saw there. */
  let shown: { agentId: string; situation: UpdateSituation } | null = null;
  // "Ask its agent", for a workspace too old to update itself.
  const askAgent = new AskAgentModel({
    onStarted: (workspaceAgentId) =>
      getAppContext().shell.enterWorkspace(workspaceAgentId),
  });

  /** Leaving settles how a run ended: once read here, it has said all it had
   * to. Not if a press on the way out started a new run. */
  function settleShown(): void {
    if (shown === null || !isSituationSettledByLeaving(shown.situation)) return;
    const { agentId } = shown;
    const updates = getAppContext().stores.updates;
    if (updates.isUpdating(agentId)) return;
    void updates.dismissOutcome(agentId).then((result) => {
      if (!result.isOk) console.warn("The update's outcome could not be dismissed:", result.error, result.detail);
    });
  }

  function stateFor(agentId: string): UpdatesGroupState {
    let local = stateByAgentId.get(agentId);
    if (local === undefined) {
      local = freshState();
      stateByAgentId.set(agentId, local);
    }
    return local;
  }

  /** Take a request from another surface, once: flash, and say why a press made elsewhere failed. */
  function consumeArrival(agentId: string, local: UpdatesGroupState): void {
    const arrival = getAppContext().shell.takeUpdatesTabArrival(agentId);
    if (arrival === null) return;
    local.flashKey = arrival.serial;
    local.noBackupHeld = null;
    local.error =
      arrival.failure === null
        ? null
        : updateActionError(arrival.failure, PRESS_FAILURE.now);
  }

  function run(
    local: UpdatesGroupState,
    press: Press,
    targetRef: string,
    call: () => Promise<UpdateActionResult>,
    onOk: () => void = () => undefined,
  ): void {
    local.pending = { press, targetRef };
    local.error = null;
    void call().then((result) => {
      if (local.pending?.press === press) local.pending = null;
      if (result.isOk) onOk();
      else local.error = updateActionError(result, PRESS_FAILURE[press]);
      m.redraw();
    });
  }

  return {
    onbeforeupdate(vnode, old) {
      if (vnode.attrs.agentId !== old.attrs.agentId) settleShown();
      return true;
    },
    onremove() {
      settleShown();
    },
    view(vnode) {
      const { agentId } = vnode.attrs;
      const local = stateFor(agentId);
      consumeArrival(agentId, local);
      const { stores, shell } = getAppContext();
      const updates = stores.updates;
      const update = updates.forAgent(agentId);
      const isUpdating = updates.isUpdating(agentId);
      const autoUpdate = updates.autoUpdateFor(agentId);
      const status = updateTabStatusFor(
        update,
        isUpdating,
        updates.isQuietWhileStopped(agentId),
        autoUpdate.isEnabled,
        autoUpdate.window,
      );
      shown = { agentId, situation: status.situation };
      const chatAgentId = updateChatAgentIdFor(update, isUpdating);
      const actions = updateTabActionsFor(
        update,
        status,
        autoUpdate.isEnabled,
        chatAgentId,
      );
      // A question about a skip no longer offered is dropped, so it cannot come back unasked.
      // Not while its skip is out: the skip cancels the schedule before it answers.
      if (!isSkipOffered(actions) && local.pending?.press !== "skip")
        local.isSkipConfirmOpen = false;
      if (status.situation === "stopped") local.noBackupHeld = null;
      const isBusy = local.pending !== null;

      // The workspace's interface opens the run's chat tab only for clients
      // connected when the agent appears. A running workspace's frame stays
      // connected under this panel, so the run goes out from here and a refusal
      // stays here to be read; once it is out, the reader is taken to it. A
      // stopped one is entered first, as the dispatch starts it, and a refusal
      // comes back here. A plain enterWorkspace: the recovery route's start
      // intent would start it twice.
      const dispatch = (targetRef: string): void => {
        if (!updates.isStopped(agentId)) {
          run(
            local,
            "now",
            targetRef,
            () => updates.updateNow(agentId, targetRef),
            () => shell.enterWorkspace(agentId),
          );
          return;
        }
        shell.enterWorkspace(agentId);
        void updates.updateNow(agentId, targetRef).then((result) => {
          if (!result.isOk) shell.openUpdatesTab(agentId, { failure: result });
        });
      };
      const schedule = (targetRef: string): void =>
        run(local, "schedule", targetRef, () =>
          updates.scheduleUpdate(agentId, targetRef),
        );
      const press = (kind: "now" | "schedule", targetRef: string): void => {
        local.error = null;
        if (updates.needsNoBackupConfirmation(agentId))
          local.noBackupHeld = { press: kind, targetRef };
        else if (kind === "now") dispatch(targetRef);
        else schedule(targetRef);
      };
      const onAction = (action: UpdateTabAction): void => {
        switch (action.kind) {
          case "schedule":
            press("schedule", action.targetRef ?? "");
            return;
          case "now":
            press("now", "");
            return;
          case "cancel-schedule":
            run(local, "cancel-schedule", "", () =>
              updates.cancelSchedule(agentId),
            );
            return;
          case "skip":
            local.isSkipConfirmOpen = true;
            return;
          case "sign-in":
            // The sign-in chat opens in the workspace, so the press leaves this
            // tab; one that could not open comes back here, which says why.
            void shell.signInForUpdate(agentId).then((result) => {
              if (!result.isOk) shell.openUpdatesTab(agentId);
            });
            return;
          case "open-chat":
            shell.openWorkspaceChat(agentId, chatAgentId);
            return;
          case "create-workspace":
            shell.routeToHub("/create");
            return;
        }
      };
      const isPendingFor = (action: UpdateTabAction): boolean =>
        local.pending !== null &&
        local.pending.press === action.kind &&
        // The specific-version field's presses spin on their own buttons.
        local.pending.targetRef ===
          (action.kind === "schedule" ? (action.targetRef ?? "") : "");
      const workspaceName =
        stores.workspaces.entryByAnyId(agentId)?.name ?? "this workspace";
      const tooltipFor = (action: UpdateTabAction): string | undefined =>
        action.kind === "create-workspace"
          ? migrateCommandFor(workspaceName)
          : undefined;
      // The tab's button as one of an error's actions, for a row it shares with one.
      const tabErrorAction = (action: UpdateTabAction): ErrorAction => ({
        id: `ws-update-${action.kind}`,
        label: action.label,
        isDisabled: isBusy,
        isBusy: isPendingFor(action),
        tooltip: tooltipFor(action),
        onclick: () => onAction(action),
      });

      const recorded = recordedDispatchFailure(update, isUpdating);
      const signInFailure =
        status.situation === "needs-sign-in"
          ? updates.signInFailureFor(agentId)
          : null;
      const error =
        local.error ??
        (signInFailure !== null
          ? updateActionError(signInFailure, SIGN_IN_FAILURE)
          : null);
      // A press the workspace just refused for want of a sign-in already says so in its own words.
      const line =
        status.situation === "needs-sign-in" && local.error !== null
          ? local.error.message
          : status.line;
      const isNotStartedShown = status.situation === "not-started" && recorded.message !== "";
      // A refused dispatch is recorded on the row too, where the not-started
      // line and its follow-up already say it.
      const isErrorRecorded =
        isNotStartedShown && error !== null && error.message === recorded.message && error.code === recorded.code;
      const shownError = line === status.line && !isErrorRecorded ? error : null;
      // The status heading leads with the error's own mark, so a notice drawn
      // flush under it, in place of the status line, is not marked again.
      const isHeadingMarked = status.tone === "error" && status.icon === "alert";
      const errorOptions = (id: string, shownFailure: UpdateDispatchError): UpdateDispatchErrorOptions => ({
        id,
        agentId,
        error: shownFailure,
        askAgent,
        onCreateWorkspace: () => shell.routeToHub("/create"),
        // Recovery starts it and comes back to this tab.
        onStartWorkspace: updates.isStopped(agentId)
          ? () => m.route.set(recoveryRoute(agentId, m.route.get() ?? "", "start"))
          : undefined,
      });
      const isPromptOpen = local.noBackupHeld !== null || local.isSkipConfirmOpen;
      // An update that didn't start draws the tab's buttons (Try again) in its
      // follow-up's row, beside the error's own.
      const areActionsInFollowUp = isNotStartedShown && !isPromptOpen;
      const notStartedOptions = isNotStartedShown ? errorOptions("ws-update-not-started", recorded) : null;
      const notStartedSurfaceActions = areActionsInFollowUp ? actions.map(tabErrorAction) : [];
      const signInRefusalOptions =
        line !== status.line && local.error !== null ? errorOptions("ws-update-sign-in-refusal", local.error) : null;
      const shownErrorOptions = shownError === null ? null : errorOptions("ws-update-error", shownError);
      // A failure the status line would say is said by its error instead, so
      // the tab never carries a sentence outside the block that explains it.
      const lineErrors = [
        notStartedOptions === null
          ? null
          : updateDispatchErrorNotice(
              { ...notStartedOptions, isTitleMarked: isHeadingMarked },
              notStartedSurfaceActions,
            ),
        signInRefusalOptions === null
          ? null
          : updateDispatchErrorNotice({ ...signInRefusalOptions, isTitleMarked: isHeadingMarked }),
      ];
      const isLineInError = notStartedOptions !== null || signInRefusalOptions !== null;
      const shownErrors = [
        ...(notStartedOptions === null ? [] : [updateDispatchShownError(notStartedOptions, notStartedSurfaceActions)]),
        ...(signInRefusalOptions === null ? [] : [updateDispatchShownError(signInRefusalOptions)]),
        ...(shownErrorOptions === null ? [] : [updateDispatchShownError(shownErrorOptions)]),
      ];
      const actionButton = (action: UpdateTabAction): m.Children =>
        m(
          Button,
          {
            variant: variantBesideErrors(action.variant, shownErrors),
            id: `ws-update-${action.kind}`,
            disabled: isBusy,
            "data-tooltip": tooltipFor(action),
            onclick: () => onAction(action),
          },
          isPendingFor(action) ? m(Spinner, { size: "sm" }) : action.label,
        );

      const controls =
        local.noBackupHeld !== null
          ? noBackupConfirmPrompt(
              () => {
                const held = local.noBackupHeld;
                local.noBackupHeld = null;
                if (held === null) return;
                if (held.press === "now") dispatch(held.targetRef);
                else schedule(held.targetRef);
              },
              () => (local.noBackupHeld = null),
            )
          : local.isSkipConfirmOpen
            ? skipConfirmPrompt(
                update,
                autoUpdate.isEnabled,
                local.pending?.press === "skip",
                () =>
                  run(
                    local,
                    "skip",
                    "",
                    () => updates.skipUpdate(agentId),
                    () => (local.isSkipConfirmOpen = false),
                  ),
                () => (local.isSkipConfirmOpen = false),
              )
            : actions.length > 0 && !areActionsInFollowUp
              ? m(
                  "div",
                  { class: "flex flex-wrap items-center gap-2" },
                  actions.map(actionButton),
                )
              : null;

      return m("section", { id: "ws-updates-group", class: "max-w-[560px]" }, [
        // Its own fragment: the key re-creates the block on each arrival, which restarts the flash.
        [
          m(
            "div",
            {
              key: local.flashKey,
              id: "update-status",
              "data-situation": status.situation,
              "data-tone": status.tone,
              style: `--flash: ${TONE_COLOR[status.tone]}`,
              class:
                "-m-1 p-1 rounded-xl" +
                (local.flashKey > 0 ? " update-status-arrive" : ""),
            },
            [
              ...statusBlockBody(
                status,
                isLineInError ? null : line,
                lineErrors,
                shownErrorOptions === null
                  ? null
                  : m("div", { class: "mt-3" }, updateDispatchErrorNotice(shownErrorOptions)),
                local,
              ),
              controls !== null ? m("div", { class: "mt-4" }, controls) : null,
              isSpecificVersionOffered(update, status)
                ? specificVersionField(local, update, isUpdating, press)
                : null,
            ],
          ),
        ],
        kernelCardView(agentId, update, local),
        m("div", { class: "mt-8" }, [
          m(SectionHeader, "Automatic updates"),
          automaticUpdatesSection(agentId, autoUpdate, local, askAgent),
        ]),
      ]);
    },
  };
}

/** The colour the kernel card's title takes, by what it says. */
const KERNEL_CARD_TEXT: Record<KernelCard["state"], string> = {
  offered: "text-warning",
  failed: "text-important",
  "in-progress": "text-info",
  landed: "text-success",
};

/**
 * The machine's kernel security card: the restart on offer, its progress, why
 * it did not land, or that it did. A press asks once, since the restart ends
 * every chat; a refusal naming chats still at work asks again before going
 * ahead past them.
 */
function kernelCardView(agentId: string, update: UiWorkspaceUpdate, local: UpdatesGroupState): m.Children {
  const card = kernelCardFor(update);
  if (card === null) {
    // A card shown later is a new situation; a press's leftovers are not its.
    local.kernelPrompt = null;
    local.kernelError = null;
    local.isKernelDetailsOpen = false;
    return null;
  }
  const updates = getAppContext().stores.updates;
  const press = (isForced: boolean): void => {
    local.isKernelPending = true;
    local.kernelError = null;
    local.kernelPrompt = null;
    void updates.applyKernelFix(agentId, isForced).then((result) => {
      local.isKernelPending = false;
      if (!result.isOk) {
        if (result.isForceOffered ?? false) {
          const chats = result.runningChats ?? [];
          const named = chats.length > 0 ? ` (${chats.join(", ")})` : "";
          local.kernelPrompt = { kind: "force", message: `${result.error}${named} Restart anyway?` };
        } else {
          local.kernelError = updateActionError(result, KERNEL_FIX_FAILURE);
        }
      }
      m.redraw();
    });
  };
  const isDetailsOffered = card.state === "offered" || card.state === "failed";
  const prompt = local.kernelPrompt;
  const errorOptions: UpdateDispatchErrorOptions | null =
    local.kernelError === null ? null : { id: "ws-kernel-error", agentId, error: local.kernelError };
  const controls: m.Children =
    prompt !== null
      ? m("div", { id: "ws-kernel-confirm", class: "flex flex-col gap-2" }, [
          m(
            "p",
            { class: "type-helper text-primary" },
            prompt.kind === "confirm" ? KERNEL_FIX_RESTART_LINE : prompt.message,
          ),
          m("div", { class: "flex flex-wrap items-center gap-2" }, [
            m(
              Button,
              {
                variant: "primary",
                id: prompt.kind === "confirm" ? "ws-kernel-restart-confirm" : "ws-kernel-restart-force",
                disabled: local.isKernelPending,
                onclick: () => press(prompt.kind === "force"),
              },
              local.isKernelPending ? m(Spinner, { size: "sm" }) : prompt.kind === "confirm" ? "Restart now" : "Restart anyway",
            ),
            m(
              Button,
              { variant: "secondary", disabled: local.isKernelPending, onclick: () => (local.kernelPrompt = null) },
              "Cancel",
            ),
          ]),
        ])
      : card.actionLabel !== null
        ? m(
            Button,
            {
              variant: variantBesideErrors("primary", errorOptions === null ? [] : [updateDispatchShownError(errorOptions)]),
              id: "ws-kernel-restart",
              disabled: local.isKernelPending,
              onclick: () => (local.kernelPrompt = { kind: "confirm" }),
            },
            local.isKernelPending ? m(Spinner, { size: "sm" }) : card.actionLabel,
          )
        : null;
  return m(
    "div",
    {
      id: "ws-kernel-card",
      "data-state": card.state,
      class: "mt-4 rounded-lg border border-default px-3 py-2.5",
    },
    [
      m(
        "p",
        { id: "ws-kernel-title", class: `flex items-center gap-1.5 type-body font-medium ${KERNEL_CARD_TEXT[card.state]}` },
        [card.state === "in-progress" ? m(Spinner, { size: "sm" }) : SHIELD_ALERT_ICON, card.title],
      ),
      m("p", { id: "ws-kernel-line", class: "mt-1 type-body text-primary" }, card.line),
      isDetailsOffered
        ? m(
            Collapsible,
            {
              id: "kernel-fix-details",
              isOpen: local.isKernelDetailsOpen,
              onToggle: () => (local.isKernelDetailsOpen = !local.isKernelDetailsOpen),
              extra: "mt-1",
              triggerExtra: "inline-flex items-center gap-1 type-helper text-secondary hover:text-primary",
              panelExtra: "mt-1 type-body text-primary",
              summary: m("span", "See details"),
            },
            m("p", KERNEL_FIX_DETAILS),
          )
        : null,
      isDetailsOffered && prompt === null
        ? m("p", { class: "mt-2 type-helper text-secondary" }, KERNEL_FIX_RESTART_LINE)
        : null,
      errorOptions !== null ? m("div", { class: "mt-3" }, updateDispatchErrorNotice(errorOptions)) : null,
      controls !== null ? m("div", { class: "mt-3" }, controls) : null,
    ],
  );
}

/** The status block's text: its title and versions, its line (dropped where
 * an error below says the same thing) with the machine's own words under it,
 * then the security card, the costs, the update agent's quote, and any
 * error. */
function statusBlockBody(
  status: UpdateTabStatus,
  line: string | null,
  lineErrors: m.Children[],
  errorNotice: m.Children,
  local: UpdatesGroupState,
): m.Children[] {
  return [
    m("div", { class: "flex items-center justify-between gap-3" }, [
      m(
        "h3",
        {
          class:
            "flex flex-1 items-center gap-1.5 type-heading text-primary min-w-0",
        },
        [
          m(
            "span",
            {
              class: `inline-flex shrink-0 ${TONE_TEXT[status.tone]}`,
            },
            statusIcon(status.icon),
          ),
          m("span", { id: "update-status-title", class: "min-w-0 break-words" }, status.title),
        ],
      ),
      versionJump(status.versions),
    ]),
    line === null
      ? null
      : m(
          "p",
          {
            id: "update-status-line",
            class: "mt-3 type-body text-primary",
          },
          line,
        ),
    ...lineErrors,
    status.secondaryLine
      ? m(
          "p",
          { class: "mt-1 type-body text-primary" },
          status.secondaryLine,
        )
      : null,
    status.isSecurityShown
      ? m(
          "div",
          {
            id: "ws-update-security",
            class:
              "mt-3 rounded-lg border border-default px-3 py-2.5",
          },
          [
            m(
              "p",
              {
                class:
                  "flex items-center gap-1.5 type-body font-medium text-warning",
              },
              [SHIELD_ALERT_ICON, SECURITY_FIX_TITLE],
            ),
            m(
              Collapsible,
              {
                id: "security-fix-details",
                isOpen: local.isSecurityDetailsOpen,
                onToggle: () =>
                  (local.isSecurityDetailsOpen =
                    !local.isSecurityDetailsOpen),
                extra: "mt-1",
                triggerExtra:
                  "inline-flex items-center gap-1 type-helper text-secondary hover:text-primary",
                panelExtra: "mt-1 type-body text-primary",
                summary: m("span", "See details"),
              },
              m("p", SECURITY_FIX_DETAILS),
            ),
          ],
        )
      : null,
    status.isCostShown
      ? m(
          "ul",
          {
            id: "ws-update-costs",
            class:
              "mt-2 flex flex-col gap-0.5 list-disc pl-4 type-helper text-secondary",
          },
          UPDATE_COST_NOTES.map((note) => m("li", note)),
        )
      : null,
    status.quote
      ? m(
          "blockquote",
          {
            id: "ws-update-quote",
            class:
              "mt-2 border-l-2 border-default pl-3 type-helper text-primary",
          },
          status.quote,
        )
      : null,
    errorNotice,
  ];
}

/** "Update to a specific version", collapsed: name a release, branch or ref, then update to it now or schedule it. */
function specificVersionField(
  local: UpdatesGroupState,
  update: UiWorkspaceUpdate,
  isUpdating: boolean,
  press: (kind: "now" | "schedule", targetRef: string) => void,
): m.Children {
  const ref = local.specificVersion.trim();
  // A pending decision is answered in the run's chat, not by another run.
  const isBlocked =
    local.pending !== null ||
    isUpdating ||
    (update.is_decision_pending ?? false) ||
    ref === "";
  return m(
    Collapsible,
    {
      id: "update-override",
      isOpen: local.isSpecificVersionOpen,
      onToggle: () =>
        (local.isSpecificVersionOpen = !local.isSpecificVersionOpen),
      extra: "mt-3",
      triggerExtra:
        "inline-flex items-center gap-1 type-helper text-secondary hover:text-primary",
      panelExtra: "mt-2",
      summary: m("span", "Update to a specific version"),
    },
    m("div", { class: "flex items-center gap-2" }, [
      m(TextInput, {
        id: "update-override-input",
        name: "update_override_ref",
        value: local.specificVersion,
        placeholder: "minds-v0.4.2, main, or a git ref",
        spellcheck: "false",
        autocomplete: "off",
        "aria-label": "Version to update to",
        extra: "flex-1",
        oninput: (event: InputEvent) => {
          local.specificVersion = (event.target as HTMLInputElement).value;
        },
      }),
      m(
        Button,
        {
          variant: "secondary",
          id: "update-override-btn",
          disabled: isBlocked,
          onclick: () => press("now", ref),
        },
        local.pending?.press === "now" &&
          local.pending.targetRef === ref &&
          ref !== ""
          ? m(Spinner, { size: "sm" })
          : "Update to this version",
      ),
      m(
        Button,
        {
          variant: "secondary",
          id: "update-override-schedule-btn",
          disabled: isBlocked,
          onclick: () => press("schedule", ref),
        },
        local.pending?.press === "schedule" &&
          local.pending.targetRef === ref &&
          ref !== ""
          ? m(Spinner, { size: "sm" })
          : "Schedule",
      ),
    ]),
  );
}

/** Which of the three automatic-update choices a workspace is on. */
type AutoUpdateChoice = "studio" | WorkspaceAutoUpdateMode;

/**
 * The workspace's Automatic updates section: the one-time offer's pitch while
 * that offer is unanswered, the app-wide switch is off and the workspace
 * follows it, otherwise the three choices -- follow Studio settings, a custom
 * window, or manual.
 */
function automaticUpdatesSection(
  agentId: string,
  autoUpdate: WorkspaceAutoUpdate,
  local: UpdatesGroupState,
  askAgent: AskAgentModel,
): m.Children {
  const { stores, shell } = getAppContext();
  const updates = stores.updates;
  const prompt = shell.autoUpdatePrompt;
  // Until the first frame the switch reads off whatever it is, so neither the pitch nor the choices can be drawn.
  if (!updates.hasReceivedUpdates) return null;
  // A workspace with its own setting shows it: the pitch is about Studio's, which it does not follow.
  if (
    prompt.isAnswered() === false &&
    !updates.isAutoUpdateEnabled &&
    autoUpdate.override === null
  ) {
    return m(
      "div",
      { id: "ws-auto-update-pitch", class: "flex flex-col gap-2" },
      [
        m(
          "p",
          { class: "type-body text-primary" },
          "Studio can keep your workspaces current, updating any that fall behind overnight:",
        ),
        renderUpdateWindowPicker({
          idSuffix: "pitch",
          startHour: prompt.startHour,
          endHour: prompt.endHour,
          isSentenceEnd: true,
          onChange: (start, end) => prompt.setWindow(start, end),
        }),
        m(
          "ul",
          {
            class:
              "flex flex-col gap-0.5 list-disc pl-4 type-helper text-secondary",
          },
          [
            m("li", "Uses tokens from your plan or API usage"),
            m("li", "Workspaces need to be running during this window"),
            m(
              "li",
              "Applies to every workspace; you can change this anytime in Studio settings",
            ),
          ],
        ),
        settingsFailureLine("ws-auto-update-pitch-error", prompt.failure),
        m("div", { class: "mt-2 flex flex-wrap items-center gap-2" }, [
          m(
            "button",
            {
              type: "button",
              id: "ws-auto-update-pitch-accept",
              class: GREEN_BUTTON_CLASS,
              disabled: prompt.isBusy,
              onclick: () => void prompt.choose("accepted", shell.accountId),
            },
            prompt.isBusy ? m(Spinner, { size: "sm" }) : "Turn on auto-updates",
          ),
          m(
            Button,
            {
              variant: "secondary",
              id: "ws-auto-update-pitch-decline",
              disabled: prompt.isBusy,
              onclick: () => void prompt.choose("declined", shell.accountId),
            },
            "Not now",
          ),
        ]),
      ],
    );
  }
  // Studio's own window is what "Use Studio settings" names and where a custom one starts from.
  const studioHours = updates.updateWindowHours;
  const override = autoUpdate.override;
  const choice: AutoUpdateChoice = override === null ? "studio" : override.mode;
  const isBusy = local.isAutoUpdateWriting;
  const write = (next: AutoUpdateChoice, start: number, end: number): void => {
    local.isAutoUpdateWriting = true;
    local.autoUpdateError = null;
    void updates
      .setAutoUpdateOverride(
        agentId,
        next === "studio"
          ? null
          : next === "manual"
            ? { mode: "manual" }
            : {
                mode: "custom_window",
                window_start_hour: start,
                window_end_hour: end,
              },
      )
      .then((result) => {
        local.isAutoUpdateWriting = false;
        if (!result.isOk)
          local.autoUpdateError = {
            error: updateActionError(result, AUTO_UPDATE_FAILURE),
            retry: () => write(next, start, end),
          };
        m.redraw();
      });
  };
  const customStart = override?.window_start_hour ?? studioHours.start;
  const customEnd = override?.window_end_hour ?? studioHours.end;
  const change = m(
    "button",
    {
      type: "button",
      id: "ws-auto-update-change",
      class:
        "text-primary underline underline-offset-2 hover:opacity-80 cursor-pointer",
      onclick: () =>
        m.route.set("/settings", {
          ...shell.overlayQuery(),
          section: "updates",
        }),
    },
    "Change",
  );
  const studioTag = m(
    "span",
    {
      class:
        "mr-1.5 rounded-full px-1.5 type-helper font-medium " +
        (updates.isAutoUpdateEnabled
          ? "bg-[var(--c-success-surface)] text-success"
          : "bg-fill-subtle text-secondary"),
    },
    updates.isAutoUpdateEnabled ? "On" : "Off",
  );
  const option = (
    value: AutoUpdateChoice,
    title: string,
    description: m.Children,
    extra: m.Children = null,
  ): m.Children =>
    m(
      "label",
      {
        class: "flex items-start gap-3 py-2 cursor-pointer",
        "data-auto-update-choice": value,
      },
      [
        m("input", {
          type: "radio",
          name: `workspace-auto-update-${agentId}`,
          class: "mt-1 shrink-0",
          checked: choice === value,
          disabled: isBusy,
          onchange: () => write(value, customStart, customEnd),
        }),
        m("span", { class: "min-w-0" }, [
          m(
            "span",
            {
              class:
                "flex items-center gap-2 type-body text-primary font-medium",
            },
            title,
          ),
          m(
            "span",
            { class: "mt-0.5 block type-body text-secondary" },
            description,
          ),
          extra,
        ]),
      ],
    );
  return [
    m(
      "div",
      {
        id: "ws-auto-update-choices",
        role: "radiogroup",
        class: "flex flex-col",
      },
      [
        option(
          "studio",
          "Use Studio settings",
          updates.isAutoUpdateEnabled
            ? [
                studioTag,
                `Updates between ${formatClockMinute(studioHours.start * 60)} and ${formatClockMinute(studioHours.end * 60)}. `,
                change,
              ]
            : [
                studioTag,
                "No automatic updates; you'll be asked when one is available. ",
                change,
              ],
        ),
        option(
          "custom_window",
          "Custom window",
          "Update this workspace at a different time.",
          choice === "custom_window"
            ? renderUpdateWindowPicker({
                idSuffix: "workspace",
                startHour: customStart,
                endHour: customEnd,
                onChange: (start, end) => write("custom_window", start, end),
                class: "mt-2",
              })
            : null,
        ),
        option(
          "manual",
          "Update this workspace manually",
          "Other workspaces still follow Studio settings.",
        ),
      ],
    ),
    local.autoUpdateError !== null
      ? m(
          "div",
          { class: "mt-2" },
          updateDispatchErrorNotice({
            id: "ws-auto-update-error",
            agentId,
            error: local.autoUpdateError.error,
            askAgent,
            onCreateWorkspace: () => shell.routeToHub("/create"),
            onRetry: local.autoUpdateError.retry,
          }),
        )
      : null,
  ];
}
