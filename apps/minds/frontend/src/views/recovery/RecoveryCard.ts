// The machine-recovery card: what is wrong with the machine, the restart that
// fixes it, a way to report it, and the troubleshooting nobody needs until
// they do.
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
// already been tried. The app restarts a wedged machine on its own, so an
// account of a failed restart would usually be describing an event the user
// never caused and never saw.

import m from "mithril";
import { Button } from "../components/Button";
import { CopyField } from "../components/Layout";
import { DialogCloseButton } from "../components/Modal";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import type { RecoveryModel } from "../../models/backups";

/**
 * The card's heading, which is also its verdict.
 *
 * The unresponsive wording is reserved for the machine the app has actually
 * tried and failed to bring back (restart_failed). Everything else is still
 * being worked out, and says so -- claiming a machine is broken while we are
 * still checking would be putting words in the classifier's mouth.
 *
 * A machine that is answering gets said so too. The card outlives the failure
 * that raised it -- the restart lands, or the machine comes back by itself --
 * and without this branch every such card fell through to "isn't responding
 * yet." over a machine that was.
 */
export function recoveryHeading(machineName: string, health: string, isHostOffline: boolean): string {
  if (health === "restarting") {
    return isHostOffline ? `Bringing ${machineName} back online...` : `Restarting ${machineName}...`;
  }
  if (health === "restart_failed") return `${machineName} unresponsive`;
  if (isHostOffline) return `${machineName} is stopped.`;
  if (health === "healthy") return `${machineName} is responding again.`;
  return `${machineName} isn't responding yet.`;
}

/** What the heading's state means, and what the button below it costs. */
export function recoverySubheading(health: string, isHostOffline: boolean): string {
  if (health === "restart_failed") {
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

export interface RecoveryCardAttrs {
  model: RecoveryModel;
  /** Whether this surface dismisses itself once the machine is confirmed
   * reachable, which gives it a settling window a card that stays put has
   * none of (see ``isSettling`` below). */
  isSelfDismissing?: boolean;
}

/** Open the bug-report surface for this machine, so the report identifies the
 * right workspace. Assist is never offered from here: the machine the card
 * speaks for is the one that cannot answer. */
function reportProblem(agentId: string): void {
  m.route.set("/help", { workspace: agentId, assist: "0" });
}

/** The card's body, assuming the model has loaded. The shells handle the
 * loading and load-error states, which differ between them. */
export function RecoveryCardBody(): m.Component<RecoveryCardAttrs> {
  return {
    view(vnode) {
      const { model, isSelfDismissing = false } = vnode.attrs;
      const info = model.info;
      if (info === null) return null;
      // The backend being unreachable outranks whatever else the machine's
      // health reads, because it explains it: a machine minds cannot reach
      // through its provider reads stuck either way, and only one of the two
      // conditions can be acted on. Rendered without a restart button at all --
      // the restart routes through the same backend.
      //
      // Except over a machine that is answering. A provider's poll can error
      // while its machines keep answering through the forward, and a machine
      // minds is in contact with is not unreachable whatever that poll did --
      // so the band withholds this same verdict on a healthy machine, and the
      // card owes the user the ending it stayed up to deliver instead.
      if (info.is_backend_unreachable && info.health !== "healthy") {
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
      // On a surface that dismisses itself, a finished restart is still waiting
      // on the confirmation that dismisses it. Reading as idle in that window
      // would offer a Restart button for the restart that just ran.
      const isSettling = isSelfDismissing && model.isRestartSucceeded;
      // A restart nobody started from this card counts too: the unattended one,
      // or the same machine's card in another window. Its progress is what is
      // actually happening to the machine, so the card must not offer a Restart
      // button beside it.
      const isBusy = model.isRestartRunning || isSettling || info.health === "restarting";
      const health = isBusy ? "restarting" : info.health;
      return m("div", { class: "flex flex-col gap-4" }, [
        m("div", { class: "flex flex-col gap-2" }, [
          m("div", { class: "flex items-center gap-2 type-heading pr-10" }, [
            isBusy ? m(Spinner, { size: "sm" }) : null,
            m("span", recoveryHeading(info.workspace_name, health, info.is_host_offline)),
          ]),
          isBusy
            ? null
            : m("p", { class: "type-helper text-tertiary" }, recoverySubheading(health, info.is_host_offline)),
        ]),
        m("div", { class: "flex items-center gap-2" }, [
          m(
            Button,
            {
              variant: "primary",
              disabled: isBusy,
              onclick: () => void model.dispatchRestart(),
            },
            isBusy ? "Restarting..." : "Restart Machine",
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
 * Error details carries the restart error the tracker is holding and whatever
 * this card's own dispatch reported, and nothing else. There is no diagnostics
 * probe behind it anymore: the classifier that used to fill those rows is
 * reached only by its own ``/api/v1`` route now, which nothing in the app
 * calls, and its observations go to the log rather than to a list of questions
 * nobody could act on.
 *
 * The two sources are deduped on the string itself, because usually they carry
 * one: every server-side restart failure hands the same message to the tracker
 * and to the operation record, and printing it twice reads as two faults. They
 * are still both consulted, since either can be the only one there -- a
 * dispatch this card never got to start reports client-side only, and an
 * unattended restart that failed before this card opened is the tracker's
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
        ...new Set([model.restartError, info.health_error].filter((error): error is string => Boolean(error))),
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
