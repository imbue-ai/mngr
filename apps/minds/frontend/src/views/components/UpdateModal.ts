// The one place a machine's update situation is explained and acted on. Thin
// on content by design: this owes the reader the two versions, that the run
// costs an agent's time and credits, and every state that needs more than a
// badge.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { UiWorkspaceUpdate } from "../../channel/messages";
import {
  RUNNING_CHATS_PAUSE_LINE,
  displayVersion,
  isArmedByAutoUpdate,
  isRecreationRequired,
  isUpdateDispatchable,
  plainScheduleTargetRef,
  recordedDispatchFailure,
  scheduleSkipLine,
  scheduledLine,
  skipConfirmationLine,
  skippedLine,
  skippedVersionText,
  updateActivityNotice,
  updateChatAgentIdFor,
  updateModalSituation,
} from "../../models/updates";
import type { UpdateActionResult } from "../../models/updates";
import { Button } from "./Button";
import { Modal, DialogCloseButton } from "./Modal";
import { machineVerdict } from "./MachineVerdict";
import { Notice } from "./Notice";
import { Spinner } from "./Spinner";

export interface UpdateModalAttrs {
  /** The machine the modal speaks for (agent-scoped id). */
  agentId: string;
  /** Its display name, for copy that names it. */
  workspaceName: string;
  /** Opens on the skip confirmation. */
  isSkipConfirmOpen?: boolean;
  /** A press made outside the modal that failed, which it opened to explain. */
  openingFailure?: UpdateActionResult | null;
  onClose: () => void;
}

interface UpdateModalState {
  /** The action in flight, so its button can say so and the rest can't be pressed. */
  pendingAction: "now" | "schedule" | "cancel" | "skip" | "sign-in" | "dismiss" | null;
  error: string;
  /** What the machine, mngr, or the host start said, shown under `error` when any said something. */
  errorDetail: string;
  /** Which press is held for the go-ahead-without-backups confirmation. */
  noBackupConfirm: "now" | "schedule" | null;
  /** Whether the skip confirmation stands in for the action row. */
  isSkipConfirmOpen: boolean;
}

/** Where to go when a run ended without landing: its own chat has the details,
 * and whatever it found, the next step is a conversation with it. */
const CHECK_IN_LINE = "Check in with the update agent in the update chat.";

/** What the verdict of a finished run says, in the reader's terms. */
function verdictMessage(update: UiWorkspaceUpdate): string {
  // The ref the run reported landing, which the row note names too. Detection
  // is only the fallback: its next sweep is an exec into a machine whose
  // services are still coming back from the apply, so for a while after a
  // verdict `current_version` still reads the version this run moved off.
  const landed = displayVersion(update.success_note_version || update.current_version);
  switch (update.verdict) {
    case "UPDATED":
      return `This workspace was updated${landed ? ` to ${landed}` : ""}.`;
    case "UPDATED_WITH_REBUILD_ITEMS":
      // Not a failure: the machine is on the new version. The agent's own line
      // (drawn under this) says what it left for the reader.
      return (
        `This workspace was updated${landed ? ` to ${landed}` : ""}. ` +
        `The update agent left a note for you${update.verdict_detail ? ":" : " in the update chat."}`
      );
    case "ALREADY_CURRENT":
      return `This workspace is already up to date${update.current_version ? ` on ${displayVersion(update.current_version)}` : ""}. Nothing was changed.`;
    case "NEEDS_RECREATION":
      return `The update agent found that this update can't be applied to this workspace in place. ${CHECK_IN_LINE}`;
    case "STUCK":
      return `The update ran into a problem it couldn't recover from. ${CHECK_IN_LINE}`;
    case "NEEDS_DECISION":
      return "The update stopped short of applying: it can't keep something you built in this workspace. Decide how to go on in its chat.";
    case "REFUSED":
      return update.in_place_compatible_ref
        ? `The update wasn't applied. ${displayVersion(update.in_place_compatible_ref)} can still be applied to this workspace. ${CHECK_IN_LINE}`
        : `The update wasn't applied. ${CHECK_IN_LINE}`;
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
  if (update.availability === "APP_BEHIND") return `${workspaceName} is ahead of Imbue Studio`;
  if (isRecreationRequired(update)) return `${workspaceName} needs a new workspace`;
  return `Update ${workspaceName}`;
}

/** The slash command the new machine's agent runs to bring this machine's work across. */
export function migrateCommandFor(workspaceName: string): string {
  return `/migrate-workspace from ${workspaceName}`;
}

/** One "<label>  <version>" line, shared with the machine's Updates settings group. */
export function updateVersionRow(label: string, value: string): m.Children {
  return m("div", { class: "flex items-baseline justify-between gap-4" }, [
    m("span", { class: "type-helper text-secondary shrink-0" }, label),
    m("span", { class: "type-body text-primary font-mono min-w-0 text-right wrap-anywhere" }, displayVersion(value) || "unknown"),
  ]);
}

/** The go-ahead-without-backups confirmation, drawn in place of the press it
 * holds. Shared with the machine's Updates settings group. */
export function noBackupConfirmPrompt(handlers: { onConfirm: () => void; onCancel: () => void }): m.Children {
  return m("div", { class: "flex flex-col gap-2" }, [
    m(
      "p",
      { class: "type-helper text-primary" },
      "This workspace has no backups. The update still keeps every version in git and " +
        "offers a rollback afterwards, but there's no full workspace restore to fall back on. Go ahead?",
    ),
    m("div", { class: "flex flex-wrap items-center gap-2" }, [
      m(Button, { variant: "primary", onclick: handlers.onConfirm }, "Go ahead without backups"),
      m(Button, { variant: "secondary", onclick: handlers.onCancel }, "Cancel"),
    ]),
  ]);
}

/** What a stopped machine's update surfaces say, its update state being held back until it starts. */
export const STOPPED_UPDATES_LINE = "Start this workspace to see its updates.";

/** The skip confirmation, drawn in place of the controls it was asked from.
 * Shared with the machine's Updates settings group. */
export function skipConfirmPrompt(
  update: UiWorkspaceUpdate,
  isAutoUpdateEnabled: boolean,
  handlers: { isSkipping: boolean; onConfirm: () => void; onCancel: () => void },
): m.Children {
  return m("div", { class: "flex flex-col gap-2" }, [
    m("p", { class: "type-helper text-primary" }, skipConfirmationLine(update, isAutoUpdateEnabled)),
    m("div", { class: "flex flex-wrap items-center gap-2" }, [
      m(
        Button,
        { variant: "primary", id: "update-confirm-skip", disabled: handlers.isSkipping, onclick: handlers.onConfirm },
        handlers.isSkipping ? m(Spinner, { size: "sm" }) : `Skip ${skippedVersionText(update)}`,
      ),
      m(Button, { variant: "secondary", disabled: handlers.isSkipping, onclick: handlers.onCancel }, "Cancel"),
    ]),
  ]);
}

export function UpdateModal(): m.Component<UpdateModalAttrs> {
  const state: UpdateModalState = {
    pendingAction: null,
    error: "",
    errorDetail: "",
    noBackupConfirm: null,
    isSkipConfirmOpen: false,
  };

  function run(
    action: NonNullable<UpdateModalState["pendingAction"]>,
    call: () => Promise<UpdateActionResult>,
    onOk: () => void,
  ): void {
    state.pendingAction = action;
    state.error = "";
    state.errorDetail = "";
    void call().then((result) => {
      state.pendingAction = null;
      if (result.isOk) {
        onOk();
      } else {
        state.error = result.error;
        state.errorDetail = result.detail;
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
        "Imbue Studio can't detect the version of this workspace.",
      ),
      m(
        "p",
        { class: "type-helper text-tertiary" },
        "An agent inside the workspace can tell you what it's running, and update it if it should be. " +
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
        "This build of Imbue Studio tracks a branch rather than a released version, so it has nothing to " +
          "compare workspaces against. Every workspace reads as unknown here, whatever version it is on.",
      ),
      m("div", { class: "flex flex-col gap-1" }, [
        updateVersionRow("This workspace", update.current_version),
        updateVersionRow("This build of Imbue Studio", update.supported_version),
      ]),
      m(
        "p",
        { class: "type-helper text-tertiary" },
        "A released build of Imbue Studio compares each workspace against the template version it ships with. You can still " +
          "run the update: the agent inside the workspace reads its own upstream, and may find there's nothing to do.",
      ),
      // Dev-loop instructions in product copy: a released build is pinned to a
      // `minds-v*` tag, so only the dev loop can reach this body.
      m("p", { class: "type-helper text-tertiary" }, [
        m("code", { class: "font-mono" }, "just minds-start"),
        " pins this build to your template worktree's branch. To compare against a release instead, launch with ",
        m("code", { class: "font-mono" }, "just minds-start-cloud"),
        " or ",
        m("code", { class: "font-mono" }, "just minds-start <minds-vX.Y.Z>"),
        " — both also point newly created workspaces at that release rather than at your local worktree.",
      ]),
    ];
  }

  /** The too-old explainer: the versions, why no update is offered, and the
   * two steps that replace it. The app does neither step itself; the second
   * is a conversation in the new machine. */
  function needsRecreationBody(update: UiWorkspaceUpdate, workspaceName: string): m.Children {
    return [
      m("div", { class: "flex flex-col gap-1" }, [
        updateVersionRow("This workspace", update.current_version),
        updateVersionRow("Oldest updatable in place", "minds-v0.3.10"),
      ]),
      m(
        "p",
        { class: "type-body text-secondary" },
        "This workspace is running a version of Imbue Studio too old to update in place. " +
          "To get it up to date, create a new workspace and move your work across.",
      ),
      m("ol", { class: "type-helper text-secondary list-decimal pl-5 flex flex-col gap-1" }, [
        m("li", "Create a new workspace."),
        m("li", [
          "Open the new workspace and ask its agent to run ",
          m("code", { class: "font-mono" }, migrateCommandFor(workspaceName)),
          ". It brings this workspace's work and settings across.",
        ]),
      ]),
    ];
  }

  function appBehindBody(update: UiWorkspaceUpdate): m.Children {
    return m(
      "p",
      { class: "type-body text-secondary" },
      `This workspace (${displayVersion(update.current_version)}) is newer than this copy of Imbue Studio ` +
        `(${displayVersion(update.supported_version)}). Update the app to catch up — there's nothing to run here.`,
    );
  }

  return {
    oninit(vnode) {
      state.isSkipConfirmOpen = vnode.attrs.isSkipConfirmOpen ?? false;
      state.error = vnode.attrs.openingFailure?.error ?? "";
      state.errorDetail = vnode.attrs.openingFailure?.detail ?? "";
    },
    view(vnode) {
      const { agentId, workspaceName, onClose } = vnode.attrs;
      const { stores, shell } = getAppContext();
      const updates = stores.updates;
      const update = updates.forAgent(agentId);
      const isBusy = state.pendingAction !== null;
      const isUpdating = updates.isUpdating(agentId);
      const isAutoUpdateEnabled = updates.isAutoUpdateEnabled;
      const chatAgentId = updateChatAgentIdFor(update, isUpdating);
      const situation = updates.isQuietWhileStopped(agentId) ? "stopped" : updateModalSituation(update, isUpdating);

      const body: m.Children[] = [];
      const actions: m.Children[] = [];
      let error = { message: state.error, detail: state.errorDetail };

      const versionRows = (): m.Children =>
        m("div", { class: "flex flex-col gap-1" }, [
          updateVersionRow("This workspace", update.current_version),
          updateVersionRow("Supported by Imbue Studio", update.supported_version),
        ]);
      const pushOutcome = (variant: "success" | "warn" | "error", message: string): void => {
        body.push(m(Notice, { variant }, message));
        if (update.verdict_detail) body.push(m("p", { class: "type-helper text-secondary" }, update.verdict_detail));
      };
      const pushOpenChat = (): void => {
        if (!chatAgentId) return;
        actions.push(
          m(
            Button,
            {
              variant: "primary",
              id: "update-modal-open-chat",
              disabled: isBusy,
              onclick: () => {
                onClose();
                shell.openWorkspaceChat(agentId, chatAgentId);
              },
            },
            "Open chat",
          ),
        );
      };
      const pushSkipButton = (label: string): void => {
        actions.push(
          m(
            Button,
            {
              variant: "secondary",
              id: "update-modal-skip",
              disabled: isBusy,
              onclick: () => {
                state.isSkipConfirmOpen = true;
              },
            },
            label,
          ),
        );
      };
      // Enter the machine BEFORE dispatching: the machine's interface opens the
      // run's chat tab only for clients connected when the agent appears. The
      // modal survives this one navigation (see handleRouteChanged). A plain
      // enterWorkspace: the dispatch starts a stopped machine itself, and the
      // recovery route's start intent would start it twice.
      const runNow = (): void => {
        shell.enterWorkspace(agentId);
        run("now", () => updates.updateNow(agentId), onClose);
      };
      const scheduleTargetRef = plainScheduleTargetRef(update);
      const runSchedule = (): void =>
        run("schedule", () => updates.scheduleUpdate(agentId, scheduleTargetRef ?? ""), () => undefined);
      const pressButton = (press: "now" | "schedule", label: string, variant: "primary" | "secondary"): m.Children =>
        m(
          Button,
          {
            variant,
            id: `update-modal-${press}`,
            disabled: isBusy,
            onclick: () => {
              if (updates.needsNoBackupConfirmation(agentId)) {
                state.noBackupConfirm = press;
              } else if (press === "now") {
                runNow();
              } else {
                runSchedule();
              }
            },
          },
          state.pendingAction === press ? m(Spinner, { size: "sm" }) : label,
        );
      // The offer's facts, stated once rather than as warnings on one button, and
      // the way to name a different version, which lives in the machine's
      // settings (?override=1 lands there with the field open). Scheduling is the
      // default: the apply takes services down, and the update window is when
      // nobody is in the machine.
      const pushOffer = (): void => {
        body.push(
          m(
            "p",
            { class: "type-helper text-tertiary" },
            `Updating runs an agent inside this workspace, which uses credits. ${RUNNING_CHATS_PAUSE_LINE}`,
          ),
          m(
            "button",
            {
              type: "button",
              id: "update-modal-choose-version",
              class: "type-helper text-secondary underline hover:text-primary cursor-pointer self-start text-left",
              onclick: () => {
                onClose();
                shell.routeTo(`/workspace/${agentId}/options`, { tab: "settings", group: "updates", override: "1" });
              },
            },
            "Update to a different version…",
          ),
        );
        actions.push(pressButton("schedule", "Schedule update", "primary"));
        actions.push(pressButton("now", "Update now", "secondary"));
      };
      const isSkipOffered =
        situation === "needs-decision" ||
        (situation === "scheduled" && isArmedByAutoUpdate(update, isAutoUpdateEnabled));
      // A question about a skip no longer offered is dropped, so it cannot come back unasked.
      // Not while its skip is out: the skip cancels the schedule before it answers.
      if (!isSkipOffered && state.pendingAction !== "skip") state.isSkipConfirmOpen = false;
      if (situation === "stopped") state.noBackupConfirm = null;

      switch (situation) {
        case "needs-recreation":
          body.push(needsRecreationBody(update, workspaceName));
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
              "Create a new workspace",
            ),
          );
          break;
        case "app-behind":
          body.push(appBehindBody(update));
          break;
        case "in-progress": {
          const activity = updateActivityNotice(update, isUpdating);
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
          // A run the cron started opens no window while it runs, so its chat is reached from here.
          if (update.activity !== "STARTING") pushOpenChat();
          break;
        }
        case "not-started":
          if (!error.message) error = recordedDispatchFailure(update, isUpdating);
          if (isUpdateDispatchable(update)) actions.push(pressButton("now", "Try again", "primary"));
          actions.push(dismissOutcomeButton(agentId, isBusy, onClose));
          break;
        case "needs-decision":
          pushOutcome("warn", verdictMessage(update));
          pushOpenChat();
          // Dismissing it is a skip: a fresh run would set the kept work aside.
          pushSkipButton("Dismiss");
          break;
        case "failed":
          pushOutcome(
            "error",
            update.activity === "STALLED" ? updateActivityNotice(update).message : verdictMessage(update),
          );
          pushOpenChat();
          actions.push(dismissOutcomeButton(agentId, isBusy, onClose));
          break;
        case "updated-with-note":
          pushOutcome("success", verdictMessage(update));
          pushOpenChat();
          actions.push(dismissOutcomeButton(agentId, isBusy, onClose));
          break;
        case "needs-sign-in": {
          // A press the workspace just refused for want of a sign-in already says so in its own words.
          if (!error.message) {
            body.push(
              m(
                Notice,
                { variant: "warn" },
                isAutoUpdateEnabled
                  ? "No AI account is signed in to this workspace, so Studio can't update it automatically. " +
                      "Sign in, and Studio schedules its update again."
                  : "No AI account is signed in to this workspace, so its update can't start. Sign in, then try again.",
              ),
            );
          }
          const signInFailure = updates.signInFailureFor(agentId);
          if (!error.message && signInFailure !== null) {
            error = { message: signInFailure.error, detail: signInFailure.detail };
          }
          actions.push(
            m(
              Button,
              {
                variant: "primary",
                id: "update-modal-sign-in",
                disabled: isBusy,
                onclick: () => run("sign-in", () => shell.signInForUpdate(agentId), onClose),
              },
              state.pendingAction === "sign-in" ? m(Spinner, { size: "sm" }) : "Sign in",
            ),
          );
          break;
        }
        case "scheduled": {
          const skipLine = scheduleSkipLine(update);
          body.push(
            versionRows(),
            m("div", { class: "flex flex-col gap-1" }, [
              m(
                "p",
                { class: "type-helper text-secondary" },
                scheduledLine(update, updates.updateWindow, isAutoUpdateEnabled),
              ),
              skipLine ? m("p", { class: "type-helper text-tertiary" }, skipLine) : null,
            ]),
          );
          actions.push(pressButton("now", "Update now instead", "primary"));
          if (scheduleTargetRef !== null) actions.push(pressButton("schedule", "Schedule update", "secondary"));
          if (isArmedByAutoUpdate(update, isAutoUpdateEnabled)) {
            // Cancelling would only be re-armed by the next sweep; skipping is the
            // way to say "not this release".
            pushSkipButton(`Skip ${skippedVersionText(update)}`);
          } else {
            actions.push(
              m(
                Button,
                {
                  variant: "secondary",
                  id: "update-modal-cancel-schedule",
                  disabled: isBusy,
                  onclick: () => run("cancel", () => updates.cancelSchedule(agentId), () => undefined),
                },
                state.pendingAction === "cancel" ? m(Spinner, { size: "sm" }) : "Cancel schedule",
              ),
            );
          }
          break;
        }
        case "skipped":
          body.push(
            versionRows(),
            m("p", { class: "type-helper text-secondary" }, skippedLine(update, isAutoUpdateEnabled)),
          );
          actions.push(pressButton("schedule", "Schedule update", "primary"));
          actions.push(pressButton("now", "Update now", "secondary"));
          break;
        case "offered":
          body.push(versionRows());
          pushOffer();
          break;
        case "updated":
          body.push(versionRows());
          pushOutcome("success", verdictMessage(update));
          // The row hides its note, and the note's chat, behind any badge, so this is that chat's way in.
          if (update.verdict === "UPDATED") pushOpenChat();
          actions.push(dismissOutcomeButton(agentId, isBusy, onClose));
          break;
        case "unknown":
          body.push(update.unknown_reason === "NO_APP_VERSION" ? noAppVersionBody(update) : unreadableMachineBody());
          pushOffer();
          break;
        case "up-to-date":
          body.push(versionRows());
          break;
        case "stopped":
          body.push(m("p", { id: "update-modal-stopped", class: "type-body text-secondary" }, STOPPED_UPDATES_LINE));
          break;
      }

      if (error.message) body.push(m(Notice, { variant: "error" }, [error.message, machineVerdict(error.detail)]));

      // A held question replaces the action row so there is one way to answer it.
      if (state.noBackupConfirm !== null) {
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
        actions.length = 0;
      } else if (state.isSkipConfirmOpen) {
        body.push(
          skipConfirmPrompt(update, isAutoUpdateEnabled, {
            isSkipping: state.pendingAction === "skip",
            onConfirm: () => run("skip", () => updates.skipUpdate(agentId), onClose),
            onCancel: () => {
              state.isSkipConfirmOpen = false;
            },
          }),
        );
        actions.length = 0;
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
