// The creation page (/creating/<create_attempt_id>): a create attempt's
// progress, told as the tail of a conversation. A user turn restates the
// settings the attempt was submitted with, the agent says it is setting the
// workspace up, and a loading box tracks the real attempt (status polling +
// the op-log SSE). Reached from the start flow in
// the same session, the flow's transcript renders above; reached any other
// way (the create form, a reload), the page holds only these turns.
//
// When the attempt is ready the wash carries the app into the workspace; a
// failure or an interrupted record becomes an agent turn with Retry and
// Dismiss / Discard, the retry reopening the create form as a modal prefilled
// from the attempt's record.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { CreateAttemptDetail, CreateAttemptRequestSummary, LiveCreateAttemptDetail } from "../../models/create";
import { CreateAttemptWatcher, fetchCreateAttemptDetail, progressForElapsed } from "../../models/create";
import {
  INTERRUPTED_LINE,
  READY_LINE,
  SETUP_LINE,
  SETUP_SECTIONS,
  failureLine,
  summaryLines,
} from "../../models/creationTranscript";
import { FLOW_OPTIONS_GAP_MS, FLOW_THINK_MS, startFlow, streamDurationMs } from "../../models/startFlow";
import { wash } from "../../models/wash";
import { Button } from "../components/Button";
import { Link } from "../components/Link";
import { Notice } from "../components/Notice";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import { createFormModal } from "./CreatePage";
import { TRANSCRIPT_COLUMN_CLASS, agentTurn, answerRow, disclosureList, scrollAnchor, userTurn } from "./start/transcript";
import { transcriptTurns } from "./StartPage";

const DEFAULT_EXPECTED_DURATION_SECONDS = 60;
/** How long the ready line is left alone before the wash starts. */
const READY_HOLD_MS = 900;

interface CreatingState {
  createAttemptId: string;
  detail: CreateAttemptDetail | null;
  watcher: CreateAttemptWatcher | null;
  startedAtMs: number;
  isDone: boolean;
  isFailed: boolean;
  redirectUrl: string;
  errorText: string;
  errorKind: string;
  stageText: string;
  logLines: string[];
  isLogOpen: boolean;
  /** The reading material's sections the reader has opened. */
  openSectionIds: Set<string>;
  isActionPending: boolean;
  isRetryFormOpen: boolean;
  progressTimer: ReturnType<typeof setInterval> | null;
  washTimer: ReturnType<typeof setTimeout> | null;
  /** The start-flow transcript this create came out of, if this window's flow submitted it. */
  isFromStartFlow: boolean;
}

export function enterWorkspaceFromRedirect(redirectUrl: string): void {
  // redirect_url is the /goto/<workspace-id>/ URL; the shell resolves either
  // coordinate, so extract the id and enter in-app.
  const match = redirectUrl.match(/\/goto\/((?:agent|host)-[a-f0-9]+)\//i);
  if (match) {
    getAppContext().shell.enterWorkspace(match[1]);
  } else {
    window.location.href = redirectUrl;
  }
}

function isReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * The agent turn that reports a failed attempt. It keeps the ids the Electron
 * e2e workspace runner (desktop_client/e2e_workspace_runner.py) polls to fail
 * a create fast and to read the cause: `#failure-view` and `#error-message`.
 */
export function failureTurn(workspaceName: string, error: string, isInstant: boolean): m.Children {
  return agentTurn({
    key: "creation-failed",
    id: "failure-view",
    textId: "error-message",
    text: failureLine(workspaceName, error),
    startAtMs: FLOW_THINK_MS,
    isInstant,
  });
}

/** The recognized-error guidance under a failure, when the error kind has some. */
export function failureGuidance(errorKind: string): m.Children {
  if (errorKind === "GITHUB_AUTH_REQUIRED") {
    return m(Notice, { key: "creation-guidance", id: "github-auth-help", extra: "max-w-[calc(100%-100px)]" }, [
      "This repository looks private. Install the GitHub app or use a repository URL that includes ",
      "credentials, then retry.",
    ]);
  }
  if (errorKind === "GIT_AUTH_REQUIRED") {
    return m(Notice, { key: "creation-guidance", id: "git-auth-help", extra: "max-w-[calc(100%-100px)]" }, [
      "This git host rejected anonymous access. Use a repository URL that includes credentials, then retry.",
    ]);
  }
  return null;
}

function freshState(createAttemptId: string): CreatingState {
  return {
    createAttemptId,
    detail: null,
    watcher: null,
    startedAtMs: Date.now(),
    isDone: false,
    isFailed: false,
    redirectUrl: "",
    errorText: "",
    errorKind: "",
    stageText: "",
    logLines: [],
    isLogOpen: false,
    openSectionIds: new Set<string>(),
    isActionPending: false,
    isRetryFormOpen: false,
    progressTimer: null,
    washTimer: null,
    isFromStartFlow: startFlow.submittedCreateAttemptId === createAttemptId,
  };
}

function attemptIdOf(vnode: m.Vnode): string {
  return (vnode.attrs as { agentId?: string }).agentId ?? "";
}

export const CreatingPage: m.ClosureComponent = () => {
  // Reassigned per attempt: the router keeps this instance across
  // /creating/<a> -> /creating/<b> (the retry's route change), so every helper
  // reads the variable rather than a captured object.
  let state: CreatingState = freshState("");

  // Where the wash grows from: the loading box's accent blob, measured at the
  // moment the wash starts (the transcript scrolls as it grows, so an earlier
  // measurement would name where the box was).
  function washOrigin(): { x: number; y: number } {
    const blob = document.getElementById("creating-blob");
    if (blob === null) return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    const rect = blob.getBoundingClientRect();
    return {
      x: Math.min(Math.max(rect.left + rect.width / 2, 0), window.innerWidth),
      y: Math.min(Math.max(rect.top + rect.height / 2, 0), window.innerHeight),
    };
  }

  function enter(): void {
    if (state.redirectUrl) enterWorkspaceFromRedirect(state.redirectUrl);
  }

  // The ready line gets its moment, then the workspace's color takes the
  // window and the shell enters the workspace while the cover is whole.
  function scheduleEntry(): void {
    const readyDelayMs = FLOW_THINK_MS + streamDurationMs(READY_LINE) + READY_HOLD_MS;
    state.washTimer = setTimeout(() => {
      state.washTimer = null;
      if (isReducedMotion()) {
        enter();
        return;
      }
      const accent = accentForAttempt();
      wash.start(accent, washOrigin(), { width: window.innerWidth, height: window.innerHeight }, enter);
    }, readyDelayMs);
  }

  function accentForAttempt(): string {
    const cached = getAppContext().stores.workspaces.accentEntry(state.createAttemptId);
    return cached?.accent ?? "#000000";
  }

  function startWatching(): void {
    state.watcher = new CreateAttemptWatcher(state.createAttemptId, {
      onDone(redirectUrl) {
        // A poll already in flight when the interval was cleared can report
        // the same DONE again; the ready turn and the wash happen once.
        if (state.isDone) return;
        state.isDone = true;
        state.redirectUrl = redirectUrl;
        scheduleEntry();
      },
      onFailed(error, errorKind) {
        state.isFailed = true;
        state.errorText = error;
        state.errorKind = errorKind;
        state.stageText = "";
      },
      onStageText(text) {
        if (!state.isFailed) state.stageText = text;
      },
      onLogLines(lines) {
        state.logLines.push(...lines);
      },
    });
    state.watcher.start();
    // Drive the time-eased progress bar (the poll only redraws every 2s).
    state.progressTimer = setInterval(() => m.redraw(), 250);
  }

  function dismissAttempt(): void {
    state.isActionPending = true;
    fetch(`/api/v1/workspaces/create-attempts/${encodeURIComponent(state.createAttemptId)}`, {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then(() => m.route.set("/"))
      .catch(() => {
        state.isActionPending = false;
        m.redraw();
      });
  }

  function discardAttempt(): void {
    state.isActionPending = true;
    fetch(`/api/v1/workspaces/create-attempts/${encodeURIComponent(state.createAttemptId)}/discard`, {
      method: "POST",
      credentials: "same-origin",
    })
      .then(() => m.route.set("/"))
      .catch(() => {
        state.isActionPending = false;
        m.redraw();
      });
  }

  /**
   * The user turn restating the settings; the reload path rebuilds it from the
   * record. The start flow's Imbue Cloud answer restates only itself, which is
   * provenance the record cannot carry -- so a create being watched in the
   * session that submitted it reads as that answer, and one picked up after a
   * reload falls back to the settings, the way the rest of this page already
   * degrades to a record view.
   */
  function summaryTurn(request: CreateAttemptRequestSummary, isInstant: boolean): m.Children {
    const isCloudPreset = state.isFromStartFlow && startFlow.isSubmittedCreateCloudPreset;
    return userTurn({
      key: "creation-summary",
      delayMs: 0,
      isInstant,
      text: summaryLines(request, isCloudPreset).join("\n"),
    });
  }

  function loadingBox(workspaceName: string, live: LiveCreateAttemptDetail | null, arriveAtMs: number): m.Children {
    const elapsedSeconds = (Date.now() - state.startedAtMs) / 1000;
    const expectedDurationSeconds = live?.expected_duration_seconds ?? DEFAULT_EXPECTED_DURATION_SECONDS;
    const percent = state.isDone ? 100 : Math.min(99.5, progressForElapsed(elapsedSeconds, expectedDurationSeconds));
    return m(
      "div",
      {
        key: "creation-box",
        id: "creating-box",
        class: "start-chat-in mt-5 w-full rounded-lg border border-default bg-surface-primary p-4",
        style: `--start-chat-delay: ${arriveAtMs}ms; --start-chat-fade: 280ms;`,
      },
      [
        m("div", { class: "type-section text-tertiary" }, "Setting up"),
        m("div", { class: "mt-2 flex items-center gap-2" }, [
          m("span", {
            id: "creating-blob",
            class: "creating-blob inline-block h-5 w-5 shrink-0",
            style: `background-color: ${accentForAttempt()};`,
            "aria-hidden": "true",
          }),
          m("span", { class: "font-semibold" }, workspaceName || "your workspace"),
        ]),
        m(
          "div",
          { class: "mt-3 h-1.5 bg-fill-subtle rounded-full overflow-hidden" },
          m("div", {
            id: "bar-fill",
            class: "h-full rounded-full transition-[width] duration-300 ease-out",
            style: `width: ${percent.toFixed(1)}%`,
          }),
        ),
        m("p", { id: "stage", class: "mt-2 type-helper text-secondary min-h-5" }, state.stageText),
        m(
          "div",
          { class: "mt-1" },
          m(
            Button,
            {
              variant: "ghost",
              extra: "!p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent hover:underline",
              onclick: () => {
                state.isLogOpen = !state.isLogOpen;
              },
            },
            state.isLogOpen ? "Hide details" : "Show details",
          ),
        ),
        state.isLogOpen
          ? m(
              "pre",
              {
                id: "logs",
                class: "mt-3 type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-[22vh] overflow-y-auto",
                onupdate: (vnode) => {
                  const element = vnode.dom as HTMLElement;
                  element.scrollTop = element.scrollHeight;
                },
              },
              state.logLines.join("\n"),
            )
          : null,
      ],
    );
  }

  function retryForm(): m.Children {
    if (!state.isRetryFormOpen) return null;
    return createFormModal({
      key: "creating-retry-form",
      id: "creating-retry-form",
      onClose: () => {
        state.isRetryFormOpen = false;
      },
      retryId: state.createAttemptId,
      onSubmitted: (operationId: string) => {
        state.isRetryFormOpen = false;
        m.route.set(`/creating/${operationId}`);
      },
    });
  }

  /** Retry / Dismiss (a failed attempt) or Retry / Discard (an interrupted one). */
  function recoveryButtons(secondLabel: string, onSecond: () => void): m.Children {
    return answerRow({
      key: "creation-recovery",
      delayMs: FLOW_THINK_MS,
      buttons: [
        {
          id: "recover-second",
          label: secondLabel,
          isEmphasized: false,
          onPress: () => {
            if (!state.isActionPending) onSecond();
          },
        },
        {
          id: "retry",
          label: "Retry",
          isEmphasized: true,
          onPress: () => {
            state.isRetryFormOpen = true;
          },
        },
      ],
    });
  }

  /** The failure turn, followed by the recognized-error guidance when the error kind has some. */
  function failureTurns(workspaceName: string, error: string, errorKind: string, isInstant: boolean): m.Children[] {
    const guidance = failureGuidance(errorKind);
    return [failureTurn(workspaceName, error, isInstant), ...(guidance !== null ? [guidance] : [])];
  }

  /** A record-backed attempt with no live thread: the failure with its log tail, or the interrupted notice. */
  function recordTurns(record: NonNullable<CreateAttemptDetail["record"]>): m.Children[] {
    if (record.state === "failed") {
      const turns = failureTurns(record.workspace_name, record.error ?? "unknown error", record.error_kind ?? "", true);
      if (record.log_tail.length > 0) {
        turns.push(
          m(
            "pre",
            { key: "creation-log-tail", class: "mt-3 type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-64 overflow-y-auto max-w-[calc(100%-100px)]" },
            record.log_tail.join("\n"),
          ),
        );
      }
      turns.push(recoveryButtons("Dismiss", dismissAttempt));
      return turns;
    }
    return [
      agentTurn({ key: "creation-interrupted", text: INTERRUPTED_LINE, startAtMs: FLOW_THINK_MS, isInstant: true }),
      recoveryButtons("Discard", discardAttempt),
    ];
  }

  /** The reading material under the setup line: each section opens on a chevron and ends in a link out. */
  function setupGuide(arriveAtMs: number, isInstant: boolean): m.Children {
    return disclosureList({
      key: "creation-guide",
      startAtMs: 0,
      isInstant,
      arriveAtMs: isInstant ? undefined : arriveAtMs,
      points: SETUP_SECTIONS,
      openIds: state.openSectionIds,
      onToggle: (id) => {
        if (state.openSectionIds.has(id)) state.openSectionIds.delete(id);
        else state.openSectionIds.add(id);
      },
      detailFor: (section) => [
        m("p", section.detail),
        m("p", { class: "mt-1" }, m(Link, { href: section.href, target: "_blank", rel: "noopener" }, section.linkLabel)),
      ],
    });
  }

  /** A live attempt: the setup line, then either the live failure or the reading material and the loading box (and the ready line). */
  function liveTurns(live: LiveCreateAttemptDetail | null, workspaceName: string, isInstant: boolean): m.Children[] {
    const setupAt = isInstant ? 0 : FLOW_THINK_MS;
    const turns: m.Children[] = [agentTurn({ key: "creation-setup", text: SETUP_LINE, startAtMs: setupAt, isInstant })];
    if (state.isFailed) {
      turns.push(...failureTurns(workspaceName, state.errorText, state.errorKind, false));
      turns.push(recoveryButtons("Dismiss", dismissAttempt));
      return turns;
    }
    const guideAt = isInstant ? 0 : setupAt + streamDurationMs(SETUP_LINE) + FLOW_OPTIONS_GAP_MS;
    turns.push(setupGuide(guideAt, isInstant));
    const boxAt = isInstant ? 0 : guideAt + FLOW_OPTIONS_GAP_MS * 2;
    turns.push(loadingBox(workspaceName, live, boxAt));
    if (state.isDone) {
      turns.push(agentTurn({ key: "creation-ready", text: READY_LINE, startAtMs: FLOW_THINK_MS }));
    }
    return turns;
  }

  function creationTurns(detail: CreateAttemptDetail): m.Children[] {
    const isInstant = detail.kind === "record" || !state.isFromStartFlow;
    const request = detail.kind === "record" ? detail.record?.request : detail.live?.request;
    const workspaceName = (detail.kind === "record" ? detail.record?.workspace_name : detail.live?.workspace_name) ?? "";
    const summary = request !== undefined && request !== null ? [summaryTurn(request, isInstant)] : [];
    if (detail.kind === "record" && detail.record !== null) return [...summary, ...recordTurns(detail.record)];
    return [...summary, ...liveTurns(detail.live, workspaceName, isInstant)];
  }

  function stopWatching(): void {
    state.watcher?.stop();
    if (state.progressTimer !== null) clearInterval(state.progressTimer);
    if (state.washTimer !== null) clearTimeout(state.washTimer);
  }

  /** Show `createAttemptId`: fresh state, its detail, and (for a live attempt) the watcher. */
  function loadAttempt(createAttemptId: string): void {
    stopWatching();
    const own = freshState(createAttemptId);
    state = own;
    fetchCreateAttemptDetail(createAttemptId)
      .then((detail) => {
        // A later attempt took the page over while this read was in flight.
        if (state !== own) return;
        own.detail = detail;
        if (detail.kind === "live") {
          startWatching();
        } else if (detail.kind === "gone") {
          m.route.set("/");
        }
        m.redraw();
      })
      .catch(() => {
        if (state !== own) return;
        own.detail = { kind: "gone", live: null, record: null };
        m.route.set("/");
      });
  }

  return {
    oninit(vnode) {
      loadAttempt(attemptIdOf(vnode));
    },
    onbeforeupdate(vnode) {
      const createAttemptId = attemptIdOf(vnode);
      if (createAttemptId !== state.createAttemptId) loadAttempt(createAttemptId);
    },
    onremove() {
      stopWatching();
    },
    view() {
      const detail = state.detail;
      if (detail === null) {
        return m(PageContainer, { id: "creating", "data-agent-id": state.createAttemptId }, [
          m("div", { class: "flex justify-center pt-24" }, m(Spinner, { size: "lg" })),
        ]);
      }
      const prelude = state.isFromStartFlow
        ? transcriptTurns(startFlow.state.entries, { isInstant: true, isPressable: false })
        : [];
      const turns = creationTurns(detail);
      return m(
        "div",
        { id: "creating", "data-agent-id": state.createAttemptId, class: TRANSCRIPT_COLUMN_CLASS },
        // Every child is keyed: Mithril rejects a fragment that mixes keyed
        // vnodes with holes, so the modal is appended only while it is open.
        [...prelude, ...turns, scrollAnchor(prelude.length + turns.length), ...(state.isRetryFormOpen ? [retryForm()] : [])],
      );
    },
  };
};
