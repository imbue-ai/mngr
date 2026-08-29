// The one place a machine's update situation is explained and acted on. Thin
// on content by design: this owes the reader the two versions, that the run
// costs an agent's time and credits, and every state that needs more than a
// badge (unknown-version explainers, app-behind, schedule, verdict).

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { UiWorkspaceUpdate } from "../../channel/messages";
import {
  isFailureVerdict,
  isRecreationRequired,
  isUpdateDispatchable,
  labelVersionNote,
  updateActivityNotice,
} from "../../models/updates";
import type { UpdateActionResult } from "../../models/updates";
import { Button } from "./Button";
import { Modal, DialogCloseButton } from "./Modal";
import { Notice } from "./Notice";
import { Spinner } from "./Spinner";

export interface UpdateModalAttrs {
  /** The machine the modal speaks for (agent-scoped id). */
  agentId: string;
  /** Its display name, for copy that names it. */
  workspaceName: string;
  onClose: () => void;
}

interface UpdateModalState {
  /** The action in flight, so its button can say so and the rest can't be pressed. */
  pendingAction: "now" | "schedule" | "cancel" | "dismiss" | null;
  error: string;
  /** Which press is held for the go-ahead-without-backups confirmation. */
  noBackupConfirm: "now" | "schedule" | null;
}

/** Where to go when a run ended without landing: its own chat has the details,
 * and whatever it found, the next step is a conversation with it. */
function checkInLine(update: UiWorkspaceUpdate): string {
  return update.chat_agent_name
    ? `Check in with the update agent in the ${update.chat_agent_name} tab inside the workspace.`
    : "Check in with the update agent in its chat tab inside the workspace.";
}

/** What the verdict of a finished run says, in the reader's terms. */
function verdictMessage(update: UiWorkspaceUpdate): string {
  // The ref the run reported landing, which the row note names too. Detection
  // is only the fallback: its next sweep is an exec into a machine whose
  // services are still coming back from the apply, so for a while after a
  // verdict `current_version` still reads the version this run moved off.
  const landed = update.success_note_version || update.current_version;
  switch (update.verdict) {
    case "UPDATED":
      return `This machine was updated${landed ? ` to ${landed}` : ""}.`;
    case "UPDATED_WITH_REBUILD_ITEMS":
      // Not a failure: the machine is on the new version. The agent's own line
      // (drawn under this) says what it left for the reader.
      return (
        `This machine was updated${landed ? ` to ${landed}` : ""}. ` +
        `The update agent left a note for you${update.verdict_detail ? ":" : " in the update chat."}`
      );
    case "ALREADY_CURRENT":
      return `This machine is already up to date${update.current_version ? ` on ${update.current_version}` : ""}. Nothing was changed.`;
    case "NEEDS_RECREATION":
      return `The update agent found that this update can't be applied to this machine in place. ${checkInLine(update)}`;
    case "STUCK":
      return `The update couldn't finish and couldn't clean up after itself. ${checkInLine(update)}`;
    case "REFUSED":
      return update.in_place_compatible_ref
        ? `The update didn't run. ${update.in_place_compatible_ref} can still be applied to this machine. ${checkInLine(update)}`
        : `The update didn't run. ${checkInLine(update)}`;
    default:
      return "";
  }
}

function modalTitle(update: UiWorkspaceUpdate, workspaceName: string): string {
  if (update.availability === "UNKNOWN") {
    // When this build is the side with no version the answer is the same for
    // every machine; titling it after this one invites a hunt for a fault in it.
    return update.unknown_reason === "NO_APP_VERSION" ? "This build can't compare versions" : `${workspaceName}'s version`;
  }
  if (update.availability === "APP_BEHIND") return `${workspaceName} is ahead of Minds`;
  if (isRecreationRequired(update)) return `${workspaceName} needs a new machine`;
  return `Update ${workspaceName}`;
}

/** The slash command the new machine's agent runs to bring this machine's work across. */
export function migrateCommandFor(workspaceName: string): string {
  return `/migrate-workspace from ${workspaceName}`;
}

/** What an armed schedule says, shared with the machine's Updates settings
 * group. Window-relative rather than "tonight": the window is configurable to
 * any hours. */
export function scheduledLine(update: UiWorkspaceUpdate, updateWindow: string): string {
  const target = update.scheduled_target_ref ? ` to ${update.scheduled_target_ref}` : "";
  return `Scheduled to update${target} in the next update window (${updateWindow}).`;
}

/** One "<label>  <version>" line, shared with the machine's Updates settings group. */
export function updateVersionRow(label: string, value: string): m.Children {
  return m("div", { class: "flex items-baseline justify-between gap-4" }, [
    m("span", { class: "type-helper text-secondary shrink-0" }, label),
    m("span", { class: "type-body text-primary font-mono min-w-0 text-right wrap-anywhere" }, value || "unknown"),
  ]);
}

/** The go-ahead-without-backups confirmation, drawn in place of the press it
 * holds. Shared with the machine's Updates settings group. */
export function noBackupConfirmPrompt(handlers: { onConfirm: () => void; onCancel: () => void }): m.Children {
  return m("div", { class: "flex flex-col gap-2" }, [
    m(
      "p",
      { class: "type-helper text-primary" },
      "This machine has no backups. The update still keeps every version in git and " +
        "offers a rollback afterwards, but there's no full-machine restore to fall back on. Go ahead?",
    ),
    m("div", { class: "flex flex-wrap items-center gap-2" }, [
      m(Button, { variant: "primary", onclick: handlers.onConfirm }, "Go ahead without backups"),
      m(Button, { variant: "secondary", onclick: handlers.onCancel }, "Cancel"),
    ]),
  ]);
}

export function UpdateModal(): m.Component<UpdateModalAttrs> {
  const state: UpdateModalState = {
    pendingAction: null,
    error: "",
    noBackupConfirm: null,
  };

  function run(
    action: NonNullable<UpdateModalState["pendingAction"]>,
    call: () => Promise<UpdateActionResult>,
    onOk: () => void,
  ): void {
    state.pendingAction = action;
    state.error = "";
    void call().then((result) => {
      state.pendingAction = null;
      if (result.isOk) {
        onOk();
      } else {
        state.error = result.error;
      }
      m.redraw();
    });
  }

  /** Clears the run's outcome server-side, so the row's badge goes with it. */
  function dismissOutcomeButton(agentId: string, isBusy: boolean, onClose: () => void): m.Children {
    return m(
      Button,
      {
        variant: "secondary",
        disabled: isBusy,
        onclick: () => run("dismiss", () => getAppContext().stores.updates.dismissRunOutcome(agentId), onClose),
      },
      state.pendingAction === "dismiss" ? m(Spinner, { size: "sm" }) : "Dismiss",
    );
  }

  /** The unknown explainer for the machine being the side with no version. Not
   * a dead end: the machine's own agent can read it, so the offer is the check. */
  function unreadableMachineBody(): m.Children {
    return [
      m(
        "p",
        { class: "type-body text-secondary" },
        "Minds can't detect the version of this machine.",
      ),
      m(
        "p",
        { class: "type-helper text-tertiary" },
        "An agent inside the machine can tell you what it's running, and update it if it should be. " +
          "Running the update starts that agent: it may find there's nothing to do.",
      ),
    ];
  }

  /** The unknown explainer for THIS BUILD being the side with no version: a
   * build pinned to a branch has nothing to compare against, so every machine
   * reads unknown. Showing both refs is the whole explanation. */
  function noAppVersionBody(update: UiWorkspaceUpdate): m.Children {
    return [
      m(
        "p",
        { class: "type-body text-secondary" },
        "This build of Minds tracks a branch rather than a released version, so it has nothing to " +
          "compare machines against. Every machine reads as unknown here, whatever version it is on.",
      ),
      m("div", { class: "flex flex-col gap-1" }, [
        updateVersionRow("This machine", update.current_version),
        updateVersionRow("This build of Minds", update.supported_version),
      ]),
      m(
        "p",
        { class: "type-helper text-tertiary" },
        "A released Minds compares each machine against the template version it ships with. You can still " +
          "run the update: the agent inside the machine reads its own upstream, and may find there's nothing to do.",
      ),
      // Dev-loop instructions in product copy: a released build is pinned to a
      // `minds-v*` tag, so only the dev loop can reach this body.
      m("p", { class: "type-helper text-tertiary" }, [
        m("code", { class: "font-mono" }, "just minds-start"),
        " pins this build to your template worktree's branch. To compare against a release instead, launch with ",
        m("code", { class: "font-mono" }, "just minds-start-cloud"),
        " or ",
        m("code", { class: "font-mono" }, "just minds-start <minds-vX.Y.Z>"),
        " — both also point newly created machines at that release rather than at your local worktree.",
      ]),
    ];
  }

  /** The too-old explainer: the versions, why no update is offered, and the
   * two steps that replace it. The app does neither step itself; the second
   * is a conversation in the new machine. */
  function needsRecreationBody(update: UiWorkspaceUpdate, workspaceName: string): m.Children {
    return [
      m("div", { class: "flex flex-col gap-1" }, [
        updateVersionRow("This machine", update.current_version),
        updateVersionRow("Oldest updatable in place", "minds-v0.3.10"),
      ]),
      m(
        "p",
        { class: "type-body text-secondary" },
        "This machine is running a version of Minds too old to update in place. " +
          "To get it up to date, create a new machine and move your work across.",
      ),
      m("ol", { class: "type-helper text-secondary list-decimal pl-5 flex flex-col gap-1" }, [
        m("li", "Create a new machine."),
        m("li", [
          "Open the new machine and ask its agent to run ",
          m("code", { class: "font-mono" }, migrateCommandFor(workspaceName)),
          ". It brings this machine's work and settings across.",
        ]),
      ]),
    ];
  }

  function appBehindBody(update: UiWorkspaceUpdate): m.Children {
    return m(
      "p",
      { class: "type-body text-secondary" },
      `This machine (${update.current_version}) is newer than this copy of Minds ` +
        `(${update.supported_version}). Update the app to catch up — there's nothing to run here.`,
    );
  }

  return {
    view(vnode) {
      const { agentId, workspaceName, onClose } = vnode.attrs;
      const { stores, shell } = getAppContext();
      const updates = stores.updates;
      const update = updates.forAgent(agentId);
      const isBusy = state.pendingAction !== null;
      const isUpdating = updates.isUpdating(agentId);

      const body: m.Children[] = [];
      if (update.availability === "UNKNOWN") {
        body.push(update.unknown_reason === "NO_APP_VERSION" ? noAppVersionBody(update) : unreadableMachineBody());
      } else if (update.availability === "APP_BEHIND") {
        body.push(appBehindBody(update));
      } else if (isRecreationRequired(update)) {
        body.push(needsRecreationBody(update, workspaceName));
      } else {
        body.push(
          m("div", { class: "flex flex-col gap-1" }, [
            updateVersionRow("This machine", update.current_version),
            updateVersionRow("Supported by Minds", update.supported_version),
          ]),
        );
        const labelNote = update.is_version_from_label
          ? labelVersionNote(stores.workspaces.entryByAnyId(agentId)?.liveness)
          : null;
        if (labelNote !== null) {
          body.push(m("p", { class: "type-helper text-tertiary" }, labelNote));
        }
      }

      const activity = updateActivityNotice(update, isUpdating);
      if (activity.message) {
        body.push(
          m(
            Notice,
            { variant: "info" },
            m("div", { class: "flex items-center gap-2" }, [
              activity.isWaiting ? m(Spinner, { size: "sm" }) : null,
              m("span", activity.message),
            ]),
          ),
        );
      }

      const verdict = verdictMessage(update);
      if (verdict) {
        body.push(m(Notice, { variant: isFailureVerdict(update.verdict) ? "error" : "success" }, verdict));
      }
      if (update.verdict_detail) {
        body.push(m("p", { class: "type-helper text-secondary" }, update.verdict_detail));
      }

      if (update.is_scheduled) {
        body.push(
          m("div", { class: "flex flex-col gap-1" }, [
            m("p", { class: "type-helper text-secondary" }, scheduledLine(update, updates.updateWindow)),
            update.last_skip_reason
              ? m("p", { class: "type-helper text-tertiary" }, `Last attempt: ${update.last_skip_reason}`)
              : null,
          ]),
        );
      }

      const isDispatchable = isUpdateDispatchable(update) && !isUpdating;
      if (isDispatchable) {
        // Stated once: a fact about what an update is, not a warning on one button.
        body.push(
          m(
            "p",
            { class: "type-helper text-tertiary" },
            "Updating runs an agent inside this machine, which uses credits.",
          ),
        );
        // Naming a different version lives in the machine's settings;
        // ?override=1 lands there with the field open.
        body.push(
          m(
            "button",
            {
              type: "button",
              id: "update-modal-choose-version",
              class: "type-helper text-secondary underline hover:text-primary cursor-pointer self-start text-left",
              onclick: () => {
                onClose();
                m.route.set(`/workspace/${agentId}/options`, { tab: "settings", group: "updates", override: "1" });
              },
            },
            "Update to a different version…",
          ),
        );
      }

      if (state.error) body.push(m(Notice, { variant: "error" }, state.error));

      const actions: m.Children[] = [];
      if (isRecreationRequired(update)) {
        // The first of the two steps; the second happens inside the new machine.
        actions.push(
          m(
            Button,
            {
              variant: "primary",
              id: "update-modal-create-machine",
              onclick: () => {
                onClose();
                m.route.set("/create");
              },
            },
            "Create a new machine",
          ),
        );
      } else if (isDispatchable) {
        // Enter the machine BEFORE dispatching: the machine's interface opens
        // the run's chat tab only for clients connected when the agent appears.
        // The modal survives this one navigation (see handleRouteChanged). A
        // plain enterWorkspace: the dispatch starts a stopped machine itself,
        // and the recovery route's start intent would start it twice.
        const runNow = (): void => {
          shell.enterWorkspace(agentId);
          run("now", () => updates.updateNow(agentId), onClose);
        };
        const runSchedule = (): void => run("schedule", () => updates.scheduleUpdate(agentId), () => undefined);
        if (state.noBackupConfirm !== null) {
          // The question replaces the action row so there is one way to answer it.
          const heldPress = state.noBackupConfirm === "now" ? runNow : runSchedule;
          body.push(
            noBackupConfirmPrompt({
              onConfirm: () => {
                state.noBackupConfirm = null;
                heldPress();
              },
              onCancel: () => {
                state.noBackupConfirm = null;
              },
            }),
          );
        } else {
          // Scheduling is the default: the apply takes services down, and the
          // update window is when nobody is in the machine.
          actions.push(
            m(
              Button,
              {
                variant: "primary",
                disabled: isBusy,
                onclick: () => {
                  if (updates.needsNoBackupConfirmation(agentId)) {
                    state.noBackupConfirm = "schedule";
                  } else {
                    runSchedule();
                  }
                },
              },
              state.pendingAction === "schedule" ? m(Spinner, { size: "sm" }) : "Schedule update",
            ),
          );
          actions.push(
            m(
              Button,
              {
                variant: "secondary",
                disabled: isBusy,
                onclick: () => {
                  if (updates.needsNoBackupConfirmation(agentId)) {
                    state.noBackupConfirm = "now";
                  } else {
                    runNow();
                  }
                },
              },
              state.pendingAction === "now" ? m(Spinner, { size: "sm" }) : "Update now",
            ),
          );
        }
      }
      // A run's outcome outlives it and the row wears the badge until it is
      // cleared; a stall draws the same badge, so it gets the same Dismiss.
      const isOutcomeShowing = Boolean(verdict) || update.activity === "STALLED";
      if (isOutcomeShowing) actions.push(dismissOutcomeButton(agentId, isBusy, onClose));
      if (update.is_scheduled) {
        actions.push(
          m(
            Button,
            {
              variant: "secondary",
              disabled: isBusy,
              onclick: () => run("cancel", () => updates.cancelSchedule(agentId), () => undefined),
            },
            state.pendingAction === "cancel" ? m(Spinner, { size: "sm" }) : "Cancel schedule",
          ),
        );
      }

      return m(
        Modal,
        { isOpen: true, onClose, size: "xl", cardExtra: "relative" },
        m("div", { class: "flex flex-col gap-4" }, [
          m(DialogCloseButton, { onClose }),
          // "Update <name>" claims there is one, which is true only where a
          // version was read on both sides.
          m("h2", { class: "type-heading text-primary pr-8" }, modalTitle(update, workspaceName)),
          ...body,
          actions.length > 0 ? m("div", { class: "flex flex-wrap items-center gap-2 pt-1" }, actions) : null,
        ]),
      );
    },
  };
}
