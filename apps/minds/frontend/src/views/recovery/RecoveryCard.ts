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
import { DialogCloseButton } from "../components/Modal";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import type { RecoveryModel } from "../../models/backups";
import type { EnvironmentCondition, RecoveryKind } from "../../models/health";
import { electronBridge } from "../../electron-bridge";

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
export function recoveryBusyActionLabel(isHostOffline: boolean, recoveryKind: RecoveryKind | null): string {
  if (isHostOffline) return "Starting...";
  if (recoveryKind === "restart") return "Restarting...";
  return "Reconnecting...";
}

/** What the heading's state means, and what the button below it costs. */
export function recoverySubheading(health: string, isHostOffline: boolean): string {
  if (health === "recovery_failed") {
    return (
      "This machine stopped responding and needs to be restarted. " +
      "In progress work will be interrupted, but saved data will not be lost."
    );
  }
  if (isHostOffline) return "This machine is stopped. Starting it again will bring your work back.";
  // The outcome, stated once. This is the whole report -- the heading names the
  // condition and this says there is nothing left to do about it -- rather than
  // a separate success notice repeating it beside a heading that has to be kept
  // in step with it by hand.
  if (health === "healthy") return "This machine is answering again. Nothing further is needed here.";
  // Still checking. The restart is offered all the same -- a surface the user
  // opened on purpose never withholds the action -- but the copy does not urge
  // it, because the machine may well come back without one.
  return (
    "Minds is still checking what's wrong. This machine may come back on its own. " +
    "Restarting it will interrupt any work in progress."
  );
}

/**
 * What an unreachable backend means for the machine, and what happens next.
 *
 * Deliberately provider-agnostic -- no "check your internet", since a local
 * docker daemon is independent of the network. The actual cause comes from the
 * provider itself and is shown verbatim below this, so minds never has to
 * hand-author a sentence per provider failure mode.
 */
const BACKEND_UNREACHABLE_EXPLANATION =
  "This issue may be transient. Minds will reconnect you to your machine as soon as it can be reached again.";

/**
 * What this device's own condition means for the machine, and what to do.
 *
 * Both states describe this device, and neither vouches for the machine. What
 * was measured is that nothing here can reach anything; the far side of that
 * connection was not observed and cannot be reported on, so telling the user
 * their machine is fine would be a guess dressed as a reading -- and a wrong one
 * for a machine that died just before the network did. What is honest, and is
 * most of the reassurance anyway, is that minds is watching and will say so when
 * it can see again.
 *
 * They differ in what they ask of the user: nothing at all when the network is
 * simply down (it comes back, and the app is watching for it), and a different
 * network when this one blocks the connection minds needs -- a wait that would
 * never end on its own. The SSH copy names the protocol and concedes the browser
 * works, because otherwise it reads as the app being wrong about a connection
 * the user can see is fine.
 */
const ENVIRONMENT_BLOCKED_EXPLANATION: Record<Exclude<EnvironmentCondition, "NONE" | "UNKNOWN">, string> = {
  OFFLINE: "This device has no network connection. Minds will reconnect to your machine as soon as it does.",
  SSH_BLOCKED:
    "This network blocks the connection Minds uses to reach your machines (SSH). " +
    "Your browser works, but Minds can't get through. Try another network or a VPN.",
};

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
  "This machine may be running normally — the connection failed on this device, before reaching it.";
const DEVICE_CANNOT_CONNECT_REMEDY = "Restarting Minds rebuilds the connection.";

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
  getAppContext().shell.rememberPageBehindOverlay();
  m.route.set("/help", { workspace: agentId, assist: "0" });
}

/** The card's body, assuming the model has loaded. The shells handle the
 * loading and load-error states, which differ between them. */
export function RecoveryCardBody(): m.Component<RecoveryCardAttrs> {
  // The device-side card's verbatim error is collapsed by default: it is what
  // makes a broken install diagnosable, and unreadable to everyone else.
  let isDeviceErrorOpen = false;
  return {
    view(vnode) {
      const { model, isSelfDismissing = false, onEnterMachine = null } = vnode.attrs;
      const info = model.info;
      if (info === null) return null;
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
      const isBusy = model.isRecoveryRunning || isSettling || info.health === "recovering";
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
      const isNarratingUserBounce = (isBusy && recoveryKind === "restart") || isSettling;
      const environment: EnvironmentCondition = isNarratingUserBounce ? "NONE" : info.device_environment;
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
      if (environment !== "NONE" && environment !== "UNKNOWN" && info.health !== "healthy") {
        return m("div", { class: "flex flex-col gap-3" }, [
          m("div", { class: "type-heading pr-10" }, deviceScopedHeading(info.workspace_name)),
          m("p", { class: "type-helper text-tertiary" }, ENVIRONMENT_BLOCKED_EXPLANATION[environment]),
          m("div", { class: "flex items-center gap-2" }, [
            m("span", { class: "type-label text-secondary" }, "Waiting for network…"),
            m(Button, { variant: "secondary", onclick: () => reportProblem(info.agent_id) }, "Report a problem"),
          ]),
        ]);
      }
      // The backend being unreachable outranks whatever else the machine's
      // health reads, because it explains it: a machine minds cannot reach
      // through its provider reads stuck either way, and only one of the two
      // conditions can be acted on. Rendered without a restart button at all --
      // the recovery routes through the same backend.
      //
      // Except over a machine that is answering. A provider's poll can error
      // while its machines keep answering through the forward, and a machine
      // minds is in contact with is not unreachable whatever that poll did --
      // so the band withholds this same verdict on a healthy machine, and the
      // card owes the user the ending it stayed up to deliver instead.
      //
      // And withheld while this device's own network is unmeasured. The
      // verdict blames something on the far side of that network; after a
      // wake the provider's poll errored because the laptop was asleep, and
      // naming the provider on the strength of no measurement is the wrong
      // headline. The card's ordinary states below still describe the wait.
      if (info.is_backend_unreachable && info.health !== "healthy" && environment !== "UNKNOWN") {
        return m("div", { class: "flex flex-col gap-3" }, [
          // Both halves of the verdict: which machine is affected, and why. The
          // card can be opened from a list or a stale tab, so the machine it
          // speaks for is never assumed to be obvious.
          m(
            "div",
            { class: "type-heading pr-10" },
            `${info.workspace_name} unreachable: Can't connect to ${info.provider_label}`,
          ),
          m("p", { class: "type-helper text-tertiary" }, BACKEND_UNREACHABLE_EXPLANATION),
          info.unreachable_reason ? m(Notice, { variant: "error" }, info.unreachable_reason) : null,
          m(
            "div",
            { class: "flex items-center gap-2" },
            m(Button, { variant: "secondary", onclick: () => reportProblem(info.agent_id) }, "Report a problem"),
          ),
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
      if (info.is_device_cannot_connect && info.health !== "healthy" && environment !== "UNKNOWN") {
        const isRestartAppAvailable = electronBridge.isDesktop;
        return m("div", { class: "flex flex-col gap-3" }, [
          m("div", { class: "type-heading pr-10" }, deviceScopedHeading(info.workspace_name)),
          m(
            "p",
            { class: "type-helper text-tertiary" },
            isRestartAppAvailable
              ? `${DEVICE_CANNOT_CONNECT_CONDITION} ${DEVICE_CANNOT_CONNECT_REMEDY}`
              : DEVICE_CANNOT_CONNECT_CONDITION,
          ),
          m(
            "div",
            { class: "flex items-center gap-2" },
            // No Restart Machine: bouncing a machine that is probably fine
            // would interrupt real work without touching the actual fault.
            isRestartAppAvailable
              ? m(Button, { variant: "primary", onclick: () => electronBridge.restartApp() }, "Restart Minds")
              : null,
            m(Button, { variant: "secondary", onclick: () => reportProblem(info.agent_id) }, "Report a problem"),
          ),
          info.device_error_detail
            ? m(
                Disclosure,
                {
                  label: "Error details",
                  isOpen: isDeviceErrorOpen,
                  onToggle: () => {
                    isDeviceErrorOpen = !isDeviceErrorOpen;
                  },
                },
                m(Notice, { variant: "error" }, info.device_error_detail),
              )
            : null,
        ]);
      }
      const health = isBusy ? "recovering" : info.health;
      return m("div", { class: "flex flex-col gap-4" }, [
        m("div", { class: "flex flex-col gap-2" }, [
          m("div", { class: "flex items-center gap-2 type-heading pr-10" }, [
            isBusy ? m(Spinner, { size: "sm" }) : null,
            m("span", recoveryHeading(info.workspace_name, health, info.is_host_offline, recoveryKind)),
          ]),
          isBusy
            ? null
            : m("p", { class: "type-helper text-tertiary" }, recoverySubheading(health, info.is_host_offline)),
        ]),
        m("div", { class: "flex items-center gap-2" }, [
          // Offered only over a machine that is answering, and first when it
          // is: the reader came here because they wanted the machine, and on a
          // card saying nothing further is needed, restarting it is no longer
          // the thing to do.
          onEnterMachine === null ? null : m(Button, { variant: "primary", onclick: onEnterMachine }, "Open machine"),
          m(
            Button,
            {
              variant: onEnterMachine === null ? "primary" : "secondary",
              disabled: isBusy,
              onclick: () => void model.dispatchRecovery(),
            },
            isBusy ? recoveryBusyActionLabel(info.is_host_offline, recoveryKind) : "Restart Machine",
          ),
          m(Button, { variant: "secondary", onclick: () => reportProblem(info.agent_id) }, "Report a problem"),
        ]),
        model.logLines.length > 0
          ? m(
              "pre",
              {
                class:
                  "type-helper font-mono bg-fill-hover rounded-md p-3 max-h-64 overflow-y-auto whitespace-pre-wrap break-words",
                onupdate(preVnode: m.VnodeDOM) {
                  const el = preVnode.dom;
                  el.scrollTop = el.scrollHeight;
                },
              },
              model.logLines.join("\n"),
            )
          : null,
        m(RecoveryTroubleshooting, { model }),
      ]);
    },
  };
}

interface DisclosureAttrs {
  label: string;
  isOpen: boolean;
  onToggle: () => void;
}

/**
 * A collapsed section: a summary row that toggles its children.
 *
 * The troubleshooting blocks are for the rare reader who is actually
 * debugging; expanded, they push the restart button -- the thing almost
 * everyone came for -- off the bottom of the panel.
 */
function Disclosure(): m.Component<DisclosureAttrs> {
  return {
    view(vnode) {
      const { label, isOpen, onToggle } = vnode.attrs;
      return m("div", { class: "flex flex-col gap-2" }, [
        m(
          "button",
          {
            type: "button",
            class:
              "flex items-center gap-1.5 w-full text-left type-label text-secondary hover:text-primary " +
              "bg-transparent border-0 p-0 cursor-pointer",
            "aria-expanded": String(isOpen),
            onclick: onToggle,
          },
          [
            m("span", { class: "inline-block w-3 text-tertiary", "aria-hidden": "true" }, isOpen ? "⌄" : "›"),
            label,
          ],
        ),
        isOpen ? vnode.children : null,
      ]);
    },
  };
}

/**
 * The bordered Troubleshooting block: the errors this episode produced, and a
 * shell into the machine's host.
 *
 * Error details carries the recovery error the tracker is holding and whatever
 * this card's own dispatch reported, and nothing else. There is no diagnostics
 * probe behind it anymore: the probe and the route that reached it are both
 * gone, and what the background health tracker observes goes to the log rather
 * than to a list of questions nobody could act on.
 *
 * The two sources are deduped on the string itself, because usually they carry
 * one: every server-side recovery failure hands the same message to the tracker
 * and to the operation record, and printing it twice reads as two faults. They
 * are still both consulted, since either can be the only one there -- a
 * dispatch this card never got to start reports client-side only, and an
 * unattended start that failed before this card opened is the tracker's
 * alone.
 */
export function RecoveryTroubleshooting(): m.Component<{ model: RecoveryModel }> {
  let openSection: "errors" | "ssh" | null = null;
  return {
    view(vnode) {
      const { model } = vnode.attrs;
      const info = model.info;
      if (info === null) return null;
      const errors = [
        ...new Set([model.recoveryError, info.health_error].filter((error): error is string => Boolean(error))),
      ];
      const isSshOffered = Boolean(info.ssh_command);
      if (errors.length === 0 && !isSshOffered) return null;
      return m(
        "div",
        { class: "flex flex-col gap-3 rounded-md border border-subtle p-3" },
        [
          m("div", { class: "type-label text-secondary" }, "Troubleshooting"),
          errors.length > 0
            ? m(
                Disclosure,
                {
                  label: "Error details",
                  isOpen: openSection === "errors",
                  onToggle: () => {
                    openSection = openSection === "errors" ? null : "errors";
                  },
                },
                errors.map((error) => m(Notice, { variant: "error" }, error)),
              )
            : null,
          isSshOffered
            ? m(
                Disclosure,
                {
                  label: "Connect over SSH",
                  isOpen: openSection === "ssh",
                  onToggle: () => {
                    openSection = openSection === "ssh" ? null : "ssh";
                  },
                },
                [
                  m(
                    "p",
                    { class: "type-helper text-tertiary" },
                    "For direct debugging, connect to the machine's host from a terminal:",
                  ),
                  m(CopyField, { value: info.ssh_command }),
                ],
              )
            : null,
        ],
      );
    },
  };
}

export interface RecoveryPanelAttrs extends RecoveryCardAttrs {
  /** DOM id for the panel, so each shell stays addressable in tests + styling. */
  panelId: string;
  /** The corner dismissal, for a shell that has one. The page does not: it is
   * reached only because the machine would not load, so it has nowhere of its
   * own to send anyone, and its titlebar is never covered. */
  onClose?: () => void;
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
