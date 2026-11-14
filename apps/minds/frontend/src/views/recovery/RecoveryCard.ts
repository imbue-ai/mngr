// The machine-recovery card: what is wrong with the machine, the restart that
// fixes it, a way to report it, and the troubleshooting nobody needs until
// they do. The restart is the card's own -- the app never bounces a machine on
// its own, it only starts one.
//
// One card, one state machine, two shells. It renders as a modal over a
// machine that is still on screen -- there is something worth keeping behind
// it -- and as the recovery page when there is not (a cold entry, or a click
// into a machine from somewhere else). The two shells differ in exactly one
// thing: the modal supplies an X, and the page supplies nothing. The page is
// only ever reached because the machine would not load, so a corner button
// back to it would name the one destination that is known not to work; the
// titlebar above the page is the way out.
//
// The card describes the machine's condition, not the history of what has
// already been tried. The app starts a wedged machine on its own, so an
// account of a failed recovery would usually be describing an event the user
// never caused and never saw.

import m from "mithril";
import { getAppContext } from "../../app-context";
import { Button } from "../components/Button";
import { CopyField } from "../components/Layout";
import { Collapsible } from "../components/Collapsible";
import { DialogCloseButton } from "../components/Modal";
import { Notice } from "../components/Notice";
import { machineVerdict } from "../components/MachineVerdict";
import { Spinner } from "../components/Spinner";
import type { RecoveryInfo, RecoveryModel } from "../../models/backups";
import { recoveryFailureSentence } from "../../models/backups";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import type { EnvironmentCondition, RecoveryKind } from "../../models/health";
import { electronBridge } from "../../electron-bridge";
import {
  ErrorDetailsToggle,
  ErrorNotice,
  restartAppActions,
} from "../components/ErrorDisplay";

/**
 * The card's heading, which is also its verdict.
 *
 * The unresponsive wording is reserved for the machine the app has actually
 * tried and failed to bring back (recovery_failed). Everything else is still
 * being worked out, and says so -- claiming a machine is broken while we are
 * still checking would be putting words in the classifier's mouth.
 *
 * A machine that is answering gets said so too. The card outlives the failure
 * that raised it -- the restart lands, or the machine comes back by itself --
 * and without this branch every such card fell through to "isn't responding
 * yet." over a machine that was.
 */
export function recoveryHeading(
  machineName: string,
  health: string,
  isHostOffline: boolean,
  recoveryKind: RecoveryKind | null = null,
): string {
  if (health === "recovering") {
    // What the in-flight state is allowed to claim depends on which recovery is
    // running. A machine whose host reads stopped is genuinely being brought
    // back up, and a "restart" only ever comes from the user's own click --
    // their action makes the claim honest. Everything else is a start against a
    // machine the app cannot reach, which may well no-op: it is reconnecting,
    // not restarting, and saying otherwise tells the user their work was
    // interrupted when it was not.
    if (isHostOffline) return `Bringing ${machineName} back online...`;
    if (recoveryKind === "restart") return `Restarting ${machineName}...`;
    return `Reconnecting to ${machineName}...`;
  }
  if (health === "recovery_failed") return `${machineName} unresponsive`;
  if (isHostOffline) return `${machineName} is stopped.`;
  if (health === "healthy") return `${machineName} is responding again.`;
  return `${machineName} isn't responding yet.`;
}

/**
 * What the disabled action button says while a recovery is in flight.
 *
 * Reads the same evidence as :func:`recoveryHeading` and for the same reason.
 * The button sits directly under the heading, so a fixed "Restarting..." there
 * contradicts a heading that has just declined to claim a restart -- one card
 * describing one episode two ways, which is the disagreement this whole
 * decomposition exists to end, at the shortest range it can happen.
 */
export function recoveryBusyActionLabel(
  isHostOffline: boolean,
  recoveryKind: RecoveryKind | null,
): string {
  if (isHostOffline) return "Starting...";
  if (recoveryKind === "restart") return "Restarting...";
  return "Reconnecting...";
}

/** Whether the card is saying nothing further is needed: the machine answers
 * and is not stopped. */
function isNothingFurtherNeeded(
  health: string,
  isHostOffline: boolean,
): boolean {
  return health === "healthy" && !isHostOffline;
}

/** What the heading's state means, and what the button below it costs. */
export function recoverySubheading(
  health: string,
  isHostOffline: boolean,
): string {
  if (health === "recovery_failed") {
    return (
      "This workspace stopped responding and needs to be restarted. " +
      "In progress work will be interrupted, but saved data will not be lost."
    );
  }
  if (isHostOffline)
    return "This workspace is stopped. Starting it again will bring your work back.";
  // The outcome, stated once. This is the whole report -- the heading names the
  // condition and this says there is nothing left to do about it -- rather than
  // a separate success notice repeating it beside a heading that has to be kept
  // in step with it by hand.
  if (isNothingFurtherNeeded(health, isHostOffline))
    return "This workspace is answering again. Nothing further is needed here.";
  // Still checking. The restart is offered all the same -- a surface the user
  // opened on purpose never withholds the action -- but the copy does not urge
  // it, because the machine may well come back without one.
  return (
    "Imbue Studio is still checking what's wrong. This workspace may come back on its own. " +
    "Restarting it will interrupt any work in progress."
  );
}

/**
 * What this device's own condition means for the machine, and what to do.
 *
 * Both states describe this device, and neither vouches for the machine. What
 * was measured is that nothing here can reach anything; the far side of that
 * connection was not observed and cannot be reported on, so telling the user
 * their machine is fine would be a guess dressed as a reading -- and a wrong one
 * for a machine that died just before the network did. What is honest, and is
 * most of the reassurance anyway, is that Imbue Studio is watching and will say so when
 * it can see again.
 *
 * They differ in what they ask of the user: nothing at all when the network is
 * simply down (it comes back, and the app is watching for it), and a different
 * network when this one blocks the connection Imbue Studio needs -- a wait that would
 * never end on its own. The blocked-network copy concedes that web pages still
 * load, because otherwise it reads as the app being wrong about a connection
 * the user can see is fine; the protocol it blocks is behind "See details".
 */
const OFFLINE_EXPLANATION = "This device has no network connection.";
const SSH_BLOCKED_SENTENCE =
  "This network blocks the connection Imbue Studio uses (SSH), even though web pages still load. " +
  "*Try a different network, such as a phone hotspot.*";

/**
 * What a connection this device could not make means, and what fixes it.
 *
 * The machine is not implicated: the failure happened before anything was sent
 * to it, so it may well be running fine and serving other devices. Restarting
 * the app is the fix for both causes that land here -- it rebuilds the
 * forwarding process along with its connection pool -- and restarting the
 * machine is not offered at all, because it would interrupt real work to
 * address a fault that is not the machine's.
 *
 * The remedy is named only where it can be carried out. Restarting the app is a
 * desktop affordance (the same rule the shell's notice band states); in a
 * browser there is no app to restart, so the copy stops at what happened rather
 * than promising a fix that has no button behind it.
 */
const DEVICE_CANNOT_CONNECT_CONDITION =
  "This workspace may be running normally — the connection failed on this device, before reaching it.";
const DEVICE_CANNOT_CONNECT_REMEDY =
  "Restarting Imbue Studio rebuilds the connection.";

/**
 * The heading both device-scoped verdicts carry.
 *
 * One connection this device could not make, or every connection it could not
 * make: the same verdict at two scales, so a reader who has seen one has been
 * told what the other means. Shared rather than spelled once per branch,
 * because reading alike is the point and two literals only happen to.
 */
function deviceScopedHeading(machineName: string): string {
  return `Can't connect to ${machineName} from this device`;
}

export interface RecoveryCardAttrs {
  model: RecoveryModel;
  /** Whether this surface dismisses itself once the machine is confirmed
   * reachable, which gives it a settling window a card that stays put has
   * none of (see ``isSettling`` below). */
  isSelfDismissing?: boolean;
  /** How to enter the machine, for a surface that is standing between the
   * reader and it. Null on the surfaces that are not: the modal has the
   * machine behind it and an X to get there, and no surface offers this over a
   * machine that is not answering -- a button onto a machine that will not load
   * names a destination known not to work, which is why the card withholds one
   * everywhere else. */
  onEnterMachine?: (() => void) | null;
  /** The corner dismissal, for a shell that has one, and the card's Close
   * button once nothing further is needed. The page has none: it is reached
   * only because the machine would not load, so it has nowhere of its own to
   * send anyone, and its titlebar is never covered. */
  onClose?: () => void;
}

/** A heading over a condition Imbue Studio waits out on its own: the spinner
 * says it is waiting, so no sentence has to. */
function cardHeading(text: string): m.Children {
  return m("div", { class: "type-heading pr-10" }, text);
}

/** The provider's own account of an unreachable backend. A fix the provider
 * named is what the user reads, with its error behind See details; without
 * one, the error itself is the sentence, since it may be the only clue to
 * what to do. A refused key is Imbue Studio's own sentence. */
export function backendUnreachableError(info: RecoveryInfo): UserFacingError {
  const remediation = info.unreachable_remediation ?? "";
  if (info.is_access_rejected || remediation === "") {
    const reason = info.unreachable_reason.trim();
    return {
      message:
        reason === ""
          ? "Imbue Studio can't reach the service that runs this workspace right now."
          : reason,
      details: "",
    };
  }
  return {
    message: `To fix it, *${remediation}*.`,
    details: info.unreachable_reason,
  };
}

/** Open the bug-report surface for this machine, so the report identifies the
 * right workspace. Assist is never offered from here: the machine the card
 * speaks for is the one that cannot answer.
 *
 * The ?workspace= that names the machine is also what the Shell reads to
 * decide what to paint behind the form, and over the recovery PAGE that would
 * be the machine's own surface -- the one that would not load. Asking the
 * shell to remember the page first keeps the card the reader is reporting on
 * behind the form instead. Over the modal there is nothing to remember: the
 * machine is already the surface underneath.
 */
function reportProblem(agentId: string): void {
  const { shell } = getAppContext();
  shell.rememberPageBehindOverlay();
  m.route.set("/help", shell.overlayQuery({ workspace: agentId, assist: "0" }));
}

interface RecoveryCardState {
  isSettling: boolean;
  isBusy: boolean;
  recoveryKind: RecoveryKind | null;
  /** This device's condition as the card reads it. */
  environment: EnvironmentCondition;
  /** The card says that condition in place of its own states. */
  isDeviceConditionShown: boolean;
}

function recoveryCardState(
  model: RecoveryModel,
  info: RecoveryInfo,
  isSelfDismissing: boolean,
): RecoveryCardState {
  // On a surface that dismisses itself, a finished recovery is still
  // waiting on the confirmation that dismisses it. Reading as idle in that
  // window would offer a Restart button for the recovery that just ran.
  const isSettling = isSelfDismissing && model.isRecoverySucceeded;
  // A recovery nobody started from this card counts too: the unattended
  // start, or the same machine's card in another window. Its progress is
  // what is actually happening to the machine, so the card must not offer a
  // Restart button beside it -- nor replace one with a waiting-for-network
  // line. The click flips isRecoveryRunning at once while info.health only
  // moves on the next poll, so reading health alone would drop a running
  // recovery's spinner and log lines for a poll interval.
  const isBusy =
    model.isRecoveryRunning || isSettling || info.health === "recovering";
  // A recovery dispatched from here knows which it is from the click,
  // before the tracker has caught up and can answer -- and that window is
  // exactly when the user is looking at the card they just clicked. One
  // merely attached to (the unattended start, another window's) does not,
  // and has to take the tracker's word: ``isRecoveryRunning`` covers both,
  // so it cannot stand in for the distinction.
  const recoveryKind = model.dispatchedRecoveryKind ?? info.recovery_kind;
  // The device's condition, and never over a restart the user asked for:
  // their own stop+start bounce is a recovery to narrate, and rendering the
  // block would swap its spinner for a wait that does not hold it. The
  // same holds while a finished recovery waits for the confirmation that
  // dismisses the card. The app's unattended start is neither -- it is
  // entered unasked within seconds of a network flap and lasts as long as
  // the network is down, which is when the device's condition is the
  // explanation the user needs, so it does not hide it. The route answers
  // NONE for a machine on this device, whose outage a dead network cannot
  // explain, and UNKNOWN while nothing has been measured.
  const isNarratingUserBounce =
    (isBusy && recoveryKind === "restart") || isSettling;
  const environment: EnvironmentCondition = isNarratingUserBounce
    ? "NONE"
    : info.device_environment;
  const isDeviceConditionShown =
    environment !== "NONE" &&
    environment !== "UNKNOWN" &&
    info.health !== "healthy";
  return {
    isSettling,
    isBusy,
    recoveryKind,
    environment,
    isDeviceConditionShown,
  };
}

/** Whether the card says this device's condition in place of its own
 * states, so a surface around it need not say it again. */
export function isDeviceConditionShownOnCard(model: RecoveryModel): boolean {
  const info = model.info;
  if (info === null) return false;
  return recoveryCardState(model, info, false).isDeviceConditionShown;
}

/** The card's body, assuming the model has loaded. The shells handle the
 * loading and load-error states, which differ between them. */
export function RecoveryCardBody(): m.Component<RecoveryCardAttrs> {
  // Open while the attempt is still running and has no error to carry it.
  let isRestartLogOpen = false;
  return {
    view(vnode) {
      const {
        model,
        isSelfDismissing = false,
        onEnterMachine = null,
        onClose,
      } = vnode.attrs;
      const info = model.info;
      if (info === null) return null;
      const { isBusy, recoveryKind, environment, isDeviceConditionShown } =
        recoveryCardState(model, info, isSelfDismissing);
      // This device having no usable network outranks everything below,
      // including the backend verdict, because it explains those too: a laptop
      // that cannot reach the network cannot reach the provider either, so the
      // provider's poll errors and the machine reads unreachable -- and naming
      // the backend there blames something that is working for a condition the
      // user can actually fix. No restart button, and not a disabled one: the
      // recovery would route over the same dead network, and there is nothing
      // here for the user to decide. The card returns to its normal states on
      // its own: the machine answering clears it, and so does connectivity
      // coming back -- which also runs the start that was withheld. On a dead
      // network only the second of those can happen.
      if (isDeviceConditionShown) {
        return m("div", { class: "flex flex-col gap-3" }, [
          cardHeading(deviceScopedHeading(info.workspace_name)),
          environment === "SSH_BLOCKED"
            ? m(ErrorNotice, {
                id: "recovery-environment-error",
                error: { message: SSH_BLOCKED_SENTENCE, details: "" },
                workspaceAgentId: info.agent_id,
              })
            : m(
                "p",
                { class: "type-helper text-tertiary" },
                OFFLINE_EXPLANATION,
              ),
        ]);
      }
      // The backend being unreachable outranks whatever else the machine's
      // health reads, because it explains it: a machine Imbue Studio cannot reach
      // through its provider reads stuck either way, and only one of the two
      // conditions can be acted on. Rendered without a restart button at all --
      // the recovery routes through the same backend.
      //
      // Except over a machine that is answering. A provider's poll can error
      // while its machines keep answering through the forward, and a machine
      // Imbue Studio is in contact with is not unreachable whatever that poll did --
      // so the band withholds this same verdict on a healthy machine, and the
      // card owes the user the ending it stayed up to deliver instead.
      //
      // And withheld while this device's own network is unmeasured. The
      // verdict blames something on the far side of that network; after a
      // wake the provider's poll errored because the laptop was asleep, and
      // naming the provider on the strength of no measurement is the wrong
      // headline. The card's ordinary states below still describe the wait.
      if (
        info.is_backend_unreachable &&
        info.health !== "healthy" &&
        environment !== "UNKNOWN"
      ) {
        // Both halves of the verdict: which machine is affected, and why. The
        // card can be opened from a list or a stale tab, so the machine it
        // speaks for is never assumed to be obvious.
        const heading = info.provider_label
          ? `${info.workspace_name} unreachable: Can't connect to ${info.provider_label}`
          : `Can't reach ${info.workspace_name} right now`;
        return m("div", { class: "flex flex-col gap-3" }, [
          // A refused key does not come back on its own; anything else
          // reconnects once the provider answers again.
          cardHeading(heading),
          m(ErrorNotice, {
            id: "recovery-provider-error",
            error: backendUnreachableError(info),
            // A refused key does not come back on its own; anything else
            // reconnects once the provider answers again, so nothing has
            // failed yet.
            isRetrying: !info.is_access_rejected,
            workspaceAgentId: info.agent_id,
          }),
        ]);
      }
      // The connection failed on this device, before the machine was ever
      // reached -- but on a network that works, which is what puts it last of
      // the three explanations. The two above it are larger facts when they
      // hold at the same time: a dead network takes this machine down along
      // with every other, and an unreachable provider takes down all of its.
      // All three outrank the machine's own health, and for the same reason --
      // the machine reads unhealthy *because* of them, and its recovery episode
      // is an effect rather than a cause. As above, withheld over a machine
      // that is answering: whatever failed earlier, it is not failing now. And
      // withheld, like the verdict above, while this device's network is
      // unmeasured: the copy's claim is a failure on a network that works, and
      // nothing has measured that yet -- the band withholds its line for the
      // same state.
      if (
        info.is_device_cannot_connect &&
        info.health !== "healthy" &&
        environment !== "UNKNOWN"
      ) {
        const isRestartAppAvailable = electronBridge.isDesktop;
        return m("div", { class: "flex flex-col gap-3" }, [
          cardHeading(deviceScopedHeading(info.workspace_name)),
          // No Restart workspace: bouncing a machine that is probably fine
          // would interrupt real work without touching the actual fault.
          m(ErrorNotice, {
            id: "recovery-device-error",
            error: {
              message: isRestartAppAvailable
                ? `${DEVICE_CANNOT_CONNECT_CONDITION} ${DEVICE_CANNOT_CONNECT_REMEDY}`
                : DEVICE_CANNOT_CONNECT_CONDITION,
              details: info.device_error_detail,
            },
            actions: restartAppActions(),
            workspaceAgentId: info.agent_id,
          }),
        ]);
      }
      const health = isBusy ? "recovering" : info.health;
      // The way onward from a card saying nothing further is needed: into the
      // machine on the page, and back to it on the modal, where it is already
      // behind the card. Restarting is no longer the thing to do, so it and the
      // report step back beside this.
      const onwardAction =
        onEnterMachine !== null
          ? { label: "Open workspace", onclick: onEnterMachine }
          : isNothingFurtherNeeded(health, info.is_host_offline) &&
              onClose !== undefined
            ? { label: "Close", onclick: onClose }
            : null;
      const errors = recoveryErrorsFor(model, isBusy);
      // Busy, the restart button is the disabled progress label, which only
      // the row can draw.
      const isRemedyInError = errors.length > 0 && !isBusy;
      const cardActions: ErrorAction[] = [
        ...(onwardAction === null ? [] : [onwardAction]),
        {
          label: "Restart workspace",
          onclick: () => void model.dispatchRecovery(),
        },
      ];
      return m("div", { class: "flex flex-col gap-4" }, [
        m("div", { class: "flex flex-col gap-2" }, [
          m("div", { class: "flex items-center gap-2 type-heading pr-10" }, [
            isBusy ? m(Spinner, { size: "sm" }) : null,
            m(
              "span",
              recoveryHeading(
                info.workspace_name,
                health,
                info.is_host_offline,
                recoveryKind,
              ),
            ),
          ]),
          isBusy || model.logLines.length > 0
            ? null
            : m(
                "p",
                { class: "type-helper text-tertiary" },
                recoverySubheading(health, info.is_host_offline),
              ),
        ]),
        // What the attempt went through, ahead of how it ended. The raw lines
        // go behind the error's own See details once there is one; until then
        // the steps carry their own.
        model.logLines.length > 0
          ? m("div", { class: "flex flex-col" }, [
              m(
                "ul",
                {
                  class:
                    "type-helper text-secondary list-none m-0 p-0 flex flex-col gap-0.5",
                },
                plainRecoveryProgress(model.logLines).map((line) =>
                  m("li", line),
                ),
              ),
              errors.length > 0
                ? null
                : m("div", { class: "type-helper self-start" }, [
                    m(ErrorDetailsToggle, {
                      id: "recovery-log-details",
                      isOpen: isRestartLogOpen,
                      onToggle: () => {
                        isRestartLogOpen = !isRestartLogOpen;
                      },
                    }),
                    isRestartLogOpen
                      ? m(
                          "div",
                          {
                            id: "recovery-log-details-panel",
                            role: "region",
                            "aria-labelledby": "recovery-log-details",
                          },
                          machineVerdict(model.logLines.join("\n")),
                        )
                      : null,
                  ]),
            ])
          : null,
        model.recoveryNotice !== null
          ? m(Notice, { variant: "info" }, model.recoveryNotice)
          : null,
        model.isRecoveryFollowLost
          ? m(ErrorNotice, {
              id: "recovery-follow-lost",
              tone: "warn",
              error: {
                message: recoveryFollowLostSentence(
                  model.followedRecoveryKind,
                ),
                details: "",
              },
              actions: [
                {
                  label: "Restart workspace",
                  onclick: () => void model.dispatchRecovery(),
                },
              ],
              isBugReportOffered: false,
              workspaceAgentId: info.agent_id,
            })
          : null,
        // A failure carries the card's buttons, so the remedy sits with the
        // error it answers; without one they stand in a row of their own.
        errors.map((error, index) =>
          m(ErrorNotice, {
            key: index,
            id: `recovery-error-${index}`,
            error: index === 0 ? withRestartLog(error, model.logLines) : error,
            actions: index === 0 && isRemedyInError ? cardActions : [],
            isBugReportOffered: index === 0 && isRemedyInError,
            workspaceAgentId: info.agent_id,
          }),
        ),
        isRemedyInError
          ? null
          : m("div", { class: "flex items-center gap-2" }, [
              onwardAction === null
                ? null
                : m(
                    Button,
                    { variant: "primary", onclick: onwardAction.onclick },
                    onwardAction.label,
                  ),
              m(
                Button,
                {
                  variant: onwardAction === null ? "primary" : "secondary",
                  disabled: isBusy,
                  onclick: () => void model.dispatchRecovery(),
                },
                isBusy
                  ? recoveryBusyActionLabel(info.is_host_offline, recoveryKind)
                  : "Restart workspace",
              ),
              m(
                Button,
                {
                  variant: "secondary",
                  onclick: () => reportProblem(info.agent_id),
                },
                "Report a problem",
              ),
            ]),
        m(RecoveryConnectionDetails, { model }),
      ]);
    },
  };
}

interface CollapsibleSectionAttrs {
  /** Unique on the page; ties this summary to the block it opens. */
  id: string;
  label: string;
  isOpen: boolean;
  onToggle: () => void;
}

/**
 * A collapsed section: a summary row that toggles its children.
 *
 * The connection details are for the rare reader who is actually
 * debugging; expanded, they would crowd the card's own actions.
 *
 * This card's own dress over `Collapsible`: a text
 * marker, and a summary at the weight of the headings around it.
 */
function collapsibleSection(
  attrs: CollapsibleSectionAttrs,
  children: m.Children,
): m.Children {
  const { id, label, isOpen, onToggle } = attrs;
  return m(
    Collapsible,
    {
      id,
      isOpen,
      onToggle,
      extra: "flex flex-col gap-2",
      triggerExtra:
        "flex items-center gap-1.5 w-full text-left type-label " +
        "text-secondary hover:text-primary bg-transparent border-0 p-0",
      // The panel carries the same gap as the row above it, so its children
      // sit apart the way they would as the row's own.
      panelExtra: "flex flex-col gap-2",
      summary: label,
    },
    children,
  );
}

/**
 * The errors to show above the card's buttons, newest first: this card's own
 * restart outcome, then the failure the workspace's state reports, worded by
 * the step that failed. One failure reported by both (every server-side failure
 * reaches the tracker and the operation alike) is shown once, in the tracker's
 * better-informed words.
 */
/** The error with the restart attempt's log under it, so the card opens the
 * verdict and the attempt together rather than offering two See details. */
function withRestartLog(
  error: UserFacingError,
  logLines: readonly string[],
): UserFacingError {
  if (logLines.length === 0) return error;
  const log = `Restart log:\n${logLines.join("\n")}`;
  return {
    ...error,
    details: error.details === "" ? log : `${error.details}\n\n${log}`,
  };
}

function recoveryErrorsFor(
  model: RecoveryModel,
  isBusy: boolean,
): UserFacingError[] {
  const info = model.info;
  const failure = info !== null && !isBusy ? recoveryFailureError(info) : null;
  const errors: UserFacingError[] = [];
  if (
    model.recoveryError !== null &&
    !isSameFailure(model.recoveryError, failure)
  ) {
    errors.push(model.recoveryError);
  }
  if (failure !== null) errors.push(failure);
  return errors;
}

/** Whether the card's own error reports the tracker's failure: a refused
 * dispatch's details wrap the tracker's reason in the response's status line. */
function isSameFailure(
  error: UserFacingError,
  failure: UserFacingError | null,
): boolean {
  if (failure === null) return false;
  return (
    error.details === failure.details ||
    (failure.details !== "" && error.details.includes(failure.details))
  );
}

/** The failure the workspace's state reports, or null for a workspace whose
 * recovery has not failed. */
export function recoveryFailureError(
  info: RecoveryInfo,
): UserFacingError | null {
  if (info.health !== "recovery_failed") return null;
  return {
    message: recoveryFailureSentence(
      info.recovery_failure_cause ?? null,
      info.recovery_kind,
    ),
    details: info.health_error,
  };
}

/** What the card says once it stopped following a recovery whose status it
 * could not read. Only the user's own restart is called a restart. */
export function recoveryFollowLostSentence(kind: RecoveryKind | null): string {
  const lost =
    kind === "restart"
      ? "Imbue Studio lost track of this restart."
      : "Imbue Studio lost track of its attempt to reconnect to this workspace.";
  return `${lost} The workspace may still be coming back -- its status above keeps updating. You can restart it if it doesn't.`;
}

// The restart log's steps, said plainly. The log itself carries the internal
// names a bug report needs; it stays behind "See details".
const PLAIN_RECOVERY_STEPS: Record<string, string> = {
  "Stopping the system-services agent.": "Stopping the workspace.",
  "Starting the system-services agent.": "Starting the workspace.",
  "The workspace was already running; it was not restarted.":
    "The workspace was already running, so it wasn't restarted.",
  "Waiting for the system interface to respond.":
    "Waiting for the workspace to answer.",
  "The system interface is responding again.":
    "The workspace is answering again.",
};

/** The restart's progress as a short plain list: the steps a user would
 * recognise, skipping the bookkeeping lines. */
export function plainRecoveryProgress(logLines: readonly string[]): string[] {
  return logLines.flatMap((line) => {
    const plain = PLAIN_RECOVERY_STEPS[line];
    return plain === undefined ? [] : [plain];
  });
}

/**
 * The connection details for reaching the workspace's machine directly:
 * collapsed, for the developer or support person who knows what to do with
 * them. Nothing on the card asks a user to run it.
 */
export function RecoveryConnectionDetails(): m.Component<{
  model: RecoveryModel;
}> {
  let isOpen = false;
  return {
    view(vnode) {
      const info = vnode.attrs.model.info;
      if (info === null || !info.ssh_command) return null;
      return collapsibleSection(
        {
          id: "recovery-ssh",
          label: "Connection details",
          isOpen,
          onToggle: () => {
            isOpen = !isOpen;
          },
        },
        [
          m(
            "p",
            { class: "type-helper text-tertiary" },
            "To connect to this workspace's machine from a terminal:",
          ),
          m(CopyField, { value: info.ssh_command }),
        ],
      );
    },
  };
}

export interface RecoveryPanelAttrs extends RecoveryCardAttrs {
  /** DOM id for the panel, so each shell stays addressable in tests + styling. */
  panelId: string;
  extraClass?: string;
}

/**
 * The 560px card both shells render, with the modal's X in the corner when
 * there is a modal around it.
 *
 * Shared wholesale rather than by convention -- the two surfaces drifted apart
 * once already, when the page kept its own layout.
 */
export function RecoveryPanel(): m.Component<RecoveryPanelAttrs> {
  return {
    view(vnode) {
      const { panelId, extraClass = "", onClose } = vnode.attrs;
      return m(
        `div#${panelId}`,
        {
          class:
            "relative w-[560px] max-w-full max-h-full min-h-0 flex flex-col gap-4 " +
            "rounded-[12px] border border-subtle bg-surface-primary " +
            "shadow-overlay overflow-y-auto px-6 py-5 " +
            extraClass,
        },
        [
          onClose === undefined ? null : m(DialogCloseButton, { onClose }),
          m(RecoveryCardBody, vnode.attrs),
        ],
      );
    },
  };
}
