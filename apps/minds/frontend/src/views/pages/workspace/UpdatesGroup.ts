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
  SECURITY_FIX_DETAILS,
  SECURITY_FIX_TITLE,
  UPDATE_COST_NOTES,
  isSituationSettledByLeaving,
  isSkipOffered,
  isSpecificVersionOffered,
  updateTabActionsFor,
  updateTabStatusFor,
} from "../../../models/updateSurfaces";
import type {
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
  updateChatAgentIdFor,
} from "../../../models/updates";
import type {
  UpdateActionResult,
  UpdateSituation,
  WorkspaceAutoUpdate,
} from "../../../models/updates";
import { Button } from "../../components/Button";
import { Collapsible } from "../../components/Collapsible";
import { TextInput } from "../../components/FormControls";
import { SectionHeader } from "../../components/Layout";
import { machineVerdict } from "../../components/MachineVerdict";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";
import { renderUpdateWindowPicker } from "../../components/UpdateWindowPicker";

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
  error: { message: string; detail: string } | null;
  /** An automatic-updates write is out; apart from `pending`, so neither can clear the other's. */
  isAutoUpdateWriting: boolean;
  /** Why the last automatic-updates write failed, said beside the choices rather than the update. */
  autoUpdateError: { message: string; detail: string } | null;
  isSecurityDetailsOpen: boolean;
  isSpecificVersionOpen: boolean;
  specificVersion: string;
  /** Changes on each arrival from another surface, so the status block flashes once. */
  flashKey: number;
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
        "This workspace has no backups. The update still keeps every version in git and offers a rollback " +
          "afterwards, but there's no full workspace restore to fall back on. Go ahead?",
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

/** "Turn on auto-updates": green rather than the inverse primary, since yes is the good outcome. */
const GREEN_BUTTON_CLASS =
  "inline-flex items-center justify-center h-[34px] px-3 rounded-md type-label cursor-pointer " +
  "border border-transparent bg-success text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-default";

export function UpdatesGroup(): m.Component<UpdatesGroupAttrs> {
  const stateByAgentId = new Map<string, UpdatesGroupState>();
  /** The machine last drawn, and the situation the reader saw there. */
  let shown: { agentId: string; situation: UpdateSituation } | null = null;

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
        : { message: arrival.failure.error, detail: arrival.failure.detail };
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
      else local.error = { message: result.error, detail: result.detail };
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
      const actionButton = (action: UpdateTabAction): m.Children =>
        m(
          Button,
          {
            variant: action.variant,
            id: `ws-update-${action.kind}`,
            disabled: isBusy,
            "data-tooltip":
              action.kind === "create-workspace"
                ? migrateCommandFor(workspaceName)
                : undefined,
            onclick: () => onAction(action),
          },
          isPendingFor(action) ? m(Spinner, { size: "sm" }) : action.label,
        );

      const recorded = recordedDispatchFailure(update, isUpdating);
      const signInFailure =
        status.situation === "needs-sign-in"
          ? updates.signInFailureFor(agentId)
          : null;
      const error =
        local.error ??
        (signInFailure !== null
          ? { message: signInFailure.error, detail: signInFailure.detail }
          : null);
      // A press the workspace just refused for want of a sign-in already says so in its own words.
      const line =
        status.situation === "needs-sign-in" && local.error !== null
          ? local.error.message
          : status.line;
      const shownError = line === status.line ? error : null;
      const lineVerdicts = [
        status.situation === "not-started"
          ? machineVerdict(recorded.detail)
          : null,
        line !== status.line ? machineVerdict(local.error?.detail ?? "") : null,
      ];

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
            : actions.length > 0
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
              ...statusBlockBody(status, line, lineVerdicts, shownError, local),
              controls !== null ? m("div", { class: "mt-4" }, controls) : null,
              isSpecificVersionOffered(update, status)
                ? specificVersionField(local, update, isUpdating, press)
                : null,
            ],
          ),
        ],
        m("div", { class: "mt-8" }, [
          m(SectionHeader, "Automatic updates"),
          automaticUpdatesSection(agentId, autoUpdate, local),
        ]),
      ]);
    },
  };
}

/** The status block's text: its title and versions, its line with the
 * machine's own words under it, then the security card, the costs, the
 * update agent's quote, and any error. */
function statusBlockBody(
  status: UpdateTabStatus,
  line: string,
  lineVerdicts: m.Children[],
  shownError: { message: string; detail: string } | null,
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
    m(
      "p",
      {
        id: "update-status-line",
        class: "mt-3 type-body text-primary",
      },
      line,
    ),
    ...lineVerdicts,
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
    shownError !== null
      ? m(
          Notice,
          { variant: "error", extra: "mt-3", id: "ws-update-error" },
          [shownError.message, machineVerdict(shownError.detail)],
        )
      : null,
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
        prompt.error ? m(Notice, { variant: "error" }, prompt.error) : null,
        m("div", { class: "mt-2 flex flex-wrap items-center gap-2" }, [
          m(
            "button",
            {
              type: "button",
              id: "ws-auto-update-pitch-accept",
              class: GREEN_BUTTON_CLASS,
              disabled: prompt.isBusy,
              onclick: () => void prompt.choose("accepted"),
            },
            prompt.isBusy ? m(Spinner, { size: "sm" }) : "Turn on auto-updates",
          ),
          m(
            Button,
            {
              variant: "secondary",
              id: "ws-auto-update-pitch-decline",
              disabled: prompt.isBusy,
              onclick: () => void prompt.choose("declined"),
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
          local.autoUpdateError = { message: result.error, detail: result.detail };
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
          Notice,
          { variant: "error", extra: "mt-2", id: "ws-auto-update-error" },
          [
            local.autoUpdateError.message,
            machineVerdict(local.autoUpdateError.detail),
          ],
        )
      : null,
  ];
}
