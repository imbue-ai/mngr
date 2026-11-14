// The creation page (/creating/<create_attempt_id>): a create attempt's
// progress, told as the tail of a conversation. The manifesto exchange opens
// it (already on the page, since every workspace's conversation starts there),
// then a user turn restates the settings the attempt was submitted with, the
// agent says it is setting the workspace up, and a loading box tracks the real
// attempt (status polling + the op-log SSE). Reached from the start flow in
// the same session, the flow's transcript renders between the two.
//
// An onboarding attempt (the start flow's creates, or the form's "Show
// onboarding examples?") shows a recording of apps built in Studio, under a
// line introducing it, between the setup line and the loading box.
//
// The whole conversation is handed over as the new workspace's first chat
// (models/welcomeChat.ts) as soon as the page knows the attempt's settings, so
// it is seeded even if the page is left before the workspace is ready. When
// the attempt is ready the agent says so and the wash comes up: the workspace
// mounts behind it, and it lifts once the workspace reports the welcome chat on
// screen (models/welcomeChatArrival.ts). A failure or an interrupted record
// becomes an agent turn with Retry and Dismiss / Discard, the retry reopening
// the create form as a modal prefilled from the attempt's record.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type {
  CreateAttemptDetail,
  CreateAttemptFailure,
  CreateAttemptRequestSummary,
} from "../../models/create";
import { CreateAttemptWatcher, fetchCreateAttemptDetail } from "../../models/create";
import { ProgressDisplay } from "../../models/progress";
import type { FailureCopy, ShowcaseApp } from "../../models/creationTranscript";
import {
  IMBUE_CLOUD_SUMMARY_LINE,
  READY_LINE,
  SETUP_LINE,
  SHOWCASE_APP,
  SHOWCASE_INTRO_LINE,
  SUMMARY_HEADING,
  failureCopy,
  interruptedLine,
  summarySettingLines,
} from "../../models/creationTranscript";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import { errorFromResponse, fallbackErrorSentence } from "../../models/errors";
import {
  FLOW_OPTIONS_GAP_MS,
  FLOW_THINK_MS,
  MANIFESTO_HEADING,
  MANIFESTO_OPENER,
  MANIFESTO_OPENER_EMPHASIS,
  MANIFESTO_POINTS,
  MANIFESTO_QUESTION,
  REPORTING_ASK,
  REPORTING_MORE_DETAIL,
  REPORTING_MORE_LABEL,
  startFlow,
  streamDurationMs,
} from "../../models/startFlow";
import { wash } from "../../models/wash";
import { postWelcomeChat, postWelcomeChatLanding, welcomeChatBody } from "../../models/welcomeChat";
import { welcomeChatArrival } from "../../models/welcomeChatArrival";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { Button } from "../components/Button";
import { updateAppActions } from "../components/ErrorDisplay";
import type { ErrorTone } from "../components/errorDesign";
import { ChatError } from "./start/ChatError";
import { ImageLightbox } from "../components/ImageLightbox";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import { createFormModal } from "./CreatePage";
import { TRANSCRIPT_COLUMN_CLASS, agentTurn, disclosureList, scrollAnchor, userTurn } from "./start/transcript";
import { transcriptTurns } from "./StartPage";

/** How often a running discard is checked on. */
const DISCARD_POLL_MS = 1000;
/** The most create-log lines a failure's details carry. */
const FAILURE_LOG_MAX_LINES = 1000;
/** The mithril key a failed create's turn is drawn under. */
const FAILURE_EXTRAS_KEY = "creation-failure";
/** The space over the ready heading: twice a turn's, so it reads as the conversation's close. */
const READY_MARGIN_CLASS = "mt-20";
/** The one bold line of the loading box, over its bar. */
const SETUP_BOX_HEADING = "Setting up your workspace";
/** The toggle under the settings turn that opens the settings themselves. */
const SUMMARY_DETAILS_LABEL = "Details";
/** How far from the log's end the reader may be and still count as reading its end. */
const LOG_PIN_SLACK_PX = 4;

interface CreatingState {
  createAttemptId: string;
  detail: CreateAttemptDetail | null;
  watcher: CreateAttemptWatcher | null;
  isDone: boolean;
  isFailed: boolean;
  redirectUrl: string;
  /** Why a live attempt failed, once it has. */
  failure: CreateAttemptFailure | null;
  stageText: string;
  /** The bar, fed by the status polls and extrapolated between them. */
  progress: ProgressDisplay;
  logLines: string[];
  isLogOpen: boolean;
  /** Whether the log is kept scrolled to its end as lines arrive: it is until the reader scrolls up. */
  isLogPinnedToEnd: boolean;
  /** How many lines the log was last pinned at, so a redraw with no new line moves nothing. */
  pinnedLogLineCount: number;
  /** Whether the settings turn's lines are open under its heading. */
  isSummaryDetailsOpen: boolean;
  /** The manifesto points the reader has opened. */
  openManifestoIds: Set<string>;
  isReportingMoreOpen: boolean;
  /** Settles when the wash over the new workspace may lift. */
  coverUntil: Promise<void>;
  /** Set once the wash is up, from when the page going away is the entry working rather than the user leaving. */
  isEntryStarted: boolean;
  isActionPending: boolean;
  isRetryFormOpen: boolean;
  /** The retry form runs the attempt on this computer ("Run on this computer"). */
  isRetryRunHere: boolean;
  /** The run-here form also turns Imbue Cloud backups and sharing off: the account can't use Imbue Cloud at all. */
  isRetryImbueCloudRefused: boolean;
  /** A discard of what an interrupted attempt left: running, or why it failed. */
  discard: { phase: "removing" } | { phase: "failed"; error: UserFacingError } | null;
  discardTimer: ReturnType<typeof setTimeout> | null;
  /** Set once the page is left or a later attempt takes it over, so a discard's pending answer is dropped. */
  isStopped: boolean;
  /** Whether the showcase recording is open enlarged over the page. */
  isShowcaseLightboxOpen: boolean;
  progressTimer: ReturnType<typeof setInterval> | null;
  washTimer: ReturnType<typeof setTimeout> | null;
  /** The start-flow transcript this create came out of, if this window's flow submitted it. */
  isFromStartFlow: boolean;
}

/** The workspace id in a /goto/<workspace-id>/ URL; the shell resolves either coordinate. */
function workspaceIdFromRedirect(redirectUrl: string): string | null {
  return redirectUrl.match(/\/goto\/((?:agent|host)-[a-f0-9]+)\//i)?.[1] ?? null;
}

export function enterWorkspaceFromRedirect(redirectUrl: string): void {
  const workspaceId = workspaceIdFromRedirect(redirectUrl);
  if (workspaceId !== null) {
    getAppContext().shell.enterWorkspace(workspaceId);
  } else {
    window.location.href = redirectUrl;
  }
}

function isReducedMotion(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * A failure in the creation chat: the sentence as an agent turn, its raw text
 * behind See details, and the ways forward as the user's answer row.
 *
 * It carries the ids the Electron e2e workspace runner
 * (desktop_client/e2e_workspace_runner.py) polls to fail a create fast:
 * `#failure-view`, around its sentence `#error-message`, and -- named from
 * `#failure-view` as every error's details are -- the `#failure-view-details`
 * toggle and the `#failure-view-details-panel` holding the raw cause.
 */
export function failureTurn(
  error: UserFacingError,
  actions: ErrorAction[] = [],
  options: { tone?: ErrorTone; isBugReportOffered?: boolean } = {},
): m.Children {
  return m(ChatError, {
    key: FAILURE_EXTRAS_KEY,
    id: "failure-view",
    textId: "error-message",
    error,
    actions,
    tone: options.tone,
    isBugReportOffered: options.isBugReportOffered,
  });
}

/** Everything under a failed create: the raw error, then the end of its create log. */
function failureDetails(error: string, logLines: readonly string[]): string {
  const log = logLines.slice(-FAILURE_LOG_MAX_LINES).join("\n");
  return [error, log !== "" ? `Create log:\n${log}` : ""].filter((part) => part !== "").join("\n\n");
}

function freshState(createAttemptId: string): CreatingState {
  return {
    createAttemptId,
    detail: null,
    watcher: null,
    isDone: false,
    isFailed: false,
    redirectUrl: "",
    failure: null,
    stageText: "",
    progress: new ProgressDisplay(),
    logLines: [],
    isLogOpen: false,
    isLogPinnedToEnd: true,
    pinnedLogLineCount: 0,
    isSummaryDetailsOpen: false,
    openManifestoIds: new Set<string>(),
    isReportingMoreOpen: false,
    coverUntil: Promise.resolve(),
    isEntryStarted: false,
    isActionPending: false,
    isRetryFormOpen: false,
    isRetryRunHere: false,
    isRetryImbueCloudRefused: false,
    discard: null,
    discardTimer: null,
    isStopped: false,
    isShowcaseLightboxOpen: false,
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

  /** When the ready line that starts streaming at ``readyStartAtMs`` has landed, and the entry begins. */
  function readyLandedAtMs(readyStartAtMs: number): number {
    return readyStartAtMs + streamDurationMs(READY_LINE);
  }

  // The workspace's color takes the window, the shell enters the workspace while
  // the cover is whole, and the cover stays until the welcome chat is on screen.
  function startEntry(): void {
    state.isEntryStarted = true;
    const isInstant = isReducedMotion();
    wash.start(
      accentForAttempt(),
      // Nothing grows under reduced motion, so the corner covers the window as well as the box would.
      isInstant ? { x: 0, y: 0 } : washOrigin(),
      { width: window.innerWidth, height: window.innerHeight },
      enter,
      state.coverUntil,
      isInstant,
    );
  }

  /** The ready turn, streaming from ``readyStartAtMs``, gets its moment, then the entry begins. */
  function scheduleEntry(readyStartAtMs: number): void {
    const own = state;
    state.washTimer = setTimeout(() => {
      own.washTimer = null;
      startEntry();
    }, readyLandedAtMs(readyStartAtMs));
  }

  /** Whether the attempt is an onboarding create, which shows the recording under the setup line. */
  function isShowcaseShown(): boolean {
    return state.detail?.live?.request.is_onboarding_driven_by_agent ?? false;
  }

  /**
   * Hand the conversation so far to the new workspace as its first chat: posted once the page knows
   * the attempt's settings, while the workspace is still being built, so the desktop client seeds it
   * when the attempt finishes even if this page has been left by then. Posted again once the attempt
   * is done, which starts the seed at once and answers with the chat's id. Answers null when the
   * page has no settings to send or the hand-over failed, with the reason in the console.
   */
  function handOverWelcomeChat(): Promise<string | null> {
    const request = state.detail?.live?.request;
    if (request === undefined || request === null) return Promise.resolve(null);
    const entries = state.isFromStartFlow ? startFlow.state.entries : [];
    const isCloudPreset = state.isFromStartFlow && startFlow.isSubmittedCreateCloudPreset;
    return postWelcomeChat(
      state.createAttemptId,
      welcomeChatBody(entries, request, isCloudPreset, isShowcaseShown()),
    ).then(
      (chatId) => (chatId === "" ? null : chatId),
      (error: unknown) => {
        console.warn("The conversation could not be handed to the new workspace:", error);
        return null;
      },
    );
  }

  /**
   * Seed the welcome chat and wait for it to be on screen; settles when the wash may lift. A chat
   * the workspace knows about is asked for at once, since left alone its app only looks for the
   * workspace's page every few seconds.
   */
  function awaitWelcomeChatOnScreen(redirectUrl: string): Promise<void> {
    const createAttemptId = state.createAttemptId;
    const startedAtMs = Date.now();
    const seed = handOverWelcomeChat();
    const workspaceId = workspaceIdFromRedirect(redirectUrl);
    void seed.then((chatId) => {
      if (chatId === null || workspaceId === null) return;
      const { shell } = getAppContext();
      shell.requestFocusChat(shell.stores.workspaces.toAgentScopedId(workspaceId), chatId);
    });
    return welcomeChatArrival.begin(seed).then((landing) => {
      if (landing === null) return;
      void postWelcomeChatLanding(createAttemptId, landing, Date.now() - startedAtMs).catch((error: unknown) => {
        console.warn("The landing in the new workspace could not be recorded:", error);
      });
    });
  }

  function accentForAttempt(): string {
    const cached = getAppContext().stores.workspaces.accentEntry(state.createAttemptId);
    return cached?.accent ?? "#000000";
  }

  function startWatching(): void {
    state.watcher = new CreateAttemptWatcher(state.createAttemptId, {
      onDone(redirectUrl) {
        state.isDone = true;
        state.redirectUrl = redirectUrl;
        state.coverUntil = awaitWelcomeChatOnScreen(redirectUrl);
        scheduleEntry(FLOW_THINK_MS);
      },
      onFailed(failure) {
        state.isFailed = true;
        state.failure = failure;
        state.stageText = "";
      },
      onStageText(text) {
        if (!state.isFailed) state.stageText = text;
      },
      onLogLines(lines) {
        state.logLines.push(...lines);
      },
      onProgress(progress) {
        state.progress.apply(progress, Date.now());
      },
    });
    state.watcher.start();
    // Keep the bar moving between polls (a poll redraws only when it answers).
    state.progressTimer = setInterval(() => m.redraw(), 250);
  }

  function dismissAttempt(): void {
    state.isActionPending = true;
    fetch(
      `/api/v1/workspaces/create-attempts/${encodeURIComponent(state.createAttemptId)}`,
      {
        method: "DELETE",
        credentials: "same-origin",
      },
    )
      .then(() => getAppContext().shell.routeToHub())
      .catch(() => {
        state.isActionPending = false;
        m.redraw();
      });
  }

  /**
   * Discard what an interrupted attempt left, staying on the page until the
   * discard finishes: its status read on DONE is what clears the record, and
   * only then does the row leave the list.
   */
  function discardAttempt(): void {
    const own = state;
    const workspaceName = own.detail?.record?.workspace_name || "the workspace";
    const whatFailed = `Imbue Studio couldn't remove what was left of ${workspaceName}`;
    // Another attempt took the page over, or the page is gone.
    const isAbandoned = (): boolean => state !== own || own.isStopped;
    const fail = (error: UserFacingError): void => {
      if (isAbandoned()) return;
      own.discard = { phase: "failed", error };
      own.isActionPending = false;
      m.redraw();
    };
    own.isActionPending = true;
    own.discard = { phase: "removing" };
    const statusUrl = `/api/v1/workspaces/operations/create-attempt-discard/${encodeURIComponent(own.createAttemptId)}`;
    const poll = (): void => {
      own.discardTimer = null;
      void defaultFetchJson(statusUrl).then((result) => {
        if (isAbandoned()) return;
        if (!result.ok) {
          // A finalized discard answers 404: the record is already gone.
          if (result.status === 404) getAppContext().shell.routeToHub();
          else fail(errorFromResponse(whatFailed, result));
          return;
        }
        const body = result.body as { is_done?: boolean; status?: string; log?: string } | null;
        if (body?.is_done === true) {
          getAppContext().shell.routeToHub();
        } else if (body?.status === "FAILED") {
          fail({ message: fallbackErrorSentence(whatFailed), details: body.log ?? "" });
        } else {
          own.discardTimer = setTimeout(poll, DISCARD_POLL_MS);
        }
      });
    };
    void defaultFetchJson(`/api/v1/workspaces/create-attempts/${encodeURIComponent(own.createAttemptId)}/discard`, {
      method: "POST",
    }).then((result) => {
      if (isAbandoned()) return;
      // A 404 means the record is already gone (discarded from another window), as for the poll.
      if (result.ok) poll();
      else if (result.status === 404) getAppContext().shell.routeToHub();
      else fail(errorFromResponse(whatFailed, result));
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
    if (isCloudPreset) {
      return userTurn({ key: "creation-summary", delayMs: 0, isInstant, text: IMBUE_CLOUD_SUMMARY_LINE });
    }
    // The settings sit behind a toggle: the heading is what was said, the lines are what it meant.
    return userTurn({
      key: "creation-summary",
      delayMs: 0,
      isInstant,
      text: SUMMARY_HEADING,
      details: {
        id: "creation-summary-details",
        label: SUMMARY_DETAILS_LABEL,
        lines: summarySettingLines(request, isCloudPreset),
        isOpen: state.isSummaryDetailsOpen,
        onToggle: () => {
          state.isSummaryDetailsOpen = !state.isSummaryDetailsOpen;
        },
      },
    });
  }

  /** Keep the log's end in view as lines arrive, unless the reader has scrolled up to read something. */
  function pinLogToEnd(element: HTMLElement): void {
    if (!state.isLogPinnedToEnd || state.logLines.length === state.pinnedLogLineCount) return;
    state.pinnedLogLineCount = state.logLines.length;
    element.scrollTop = element.scrollHeight;
  }

  function loadingBox(arriveAtMs: number): m.Children {
    const percent = state.progress.percentAt(Date.now(), state.isDone);
    const remainingText = state.isDone ? "" : state.progress.remainingText();
    return m(
      "div",
      {
        key: "creation-box",
        id: "creating-box",
        class: "start-chat-in mt-8 w-full rounded-lg border border-default bg-surface-primary p-4",
        style: `--start-chat-delay: ${arriveAtMs}ms; --start-chat-fade: 280ms;`,
      },
      [
        m("div", { class: "flex items-center gap-2" }, [
          m("span", {
            id: "creating-blob",
            class: "creating-blob inline-block h-5 w-5 shrink-0",
            style: `background-color: ${accentForAttempt()};`,
            "aria-hidden": "true",
          }),
          m("span", { class: "font-semibold" }, SETUP_BOX_HEADING),
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
        m(
          "p",
          { id: "stage", class: "mt-2 type-helper text-secondary min-h-5" },
          state.isDone ? state.stageText : state.progress.labelOr(state.stageText),
        ),
        remainingText !== ""
          ? m("p", { id: "remaining", class: "type-helper text-tertiary" }, remainingText)
          : null,
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
                // Opened at its end, then kept there only while the reader stays there: the
                // progress timer redraws several times a second, and pinning on every redraw
                // would undo any scroll up the moment it was made.
                oncreate: (vnode) => {
                  state.isLogPinnedToEnd = true;
                  state.pinnedLogLineCount = 0;
                  pinLogToEnd(vnode.dom as HTMLElement);
                },
                onupdate: (vnode) => pinLogToEnd(vnode.dom as HTMLElement),
                onscroll: (event: Event) => {
                  const element = event.target as HTMLElement;
                  state.isLogPinnedToEnd =
                    element.scrollHeight - element.scrollTop - element.clientHeight <= LOG_PIN_SLACK_PX;
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
      initialPreset: state.isRetryRunHere ? "local" : undefined,
      isImbueCloudRefused: state.isRetryImbueCloudRefused,
      onSubmitted: (operationId: string) => {
        state.isRetryFormOpen = false;
        m.route.set(`/creating/${operationId}`);
      },
    });
  }

  function openRetryForm(isRunHere: boolean, isImbueCloudRefused = false): void {
    state.isRetryRunHere = isRunHere;
    state.isRetryImbueCloudRefused = isImbueCloudRefused;
    state.isRetryFormOpen = true;
  }

  /**
   * A failed attempt's ways forward, the sure fix first: Update Imbue Studio,
   * Run on this computer, Retry (which works only once the cause is dealt
   * with), then Dismiss. A non-empty `retryUnavailableReason` holds Retry,
   * disabled with the reason on hover; nothing in the row is emphasized
   * then, since a disabled answer never leads.
   */
  function failureActions(copy: FailureCopy, retryUnavailableReason: string): ErrorAction[] {
    const isRetryHeld = retryUnavailableReason !== "";
    const retry: ErrorAction[] = copy.isRetryOffered
      ? [
          {
            label: "Retry",
            isDisabled: isRetryHeld,
            tooltip: isRetryHeld ? retryUnavailableReason : undefined,
            onclick: () => {
              if (!isRetryHeld) openRetryForm(false);
            },
          },
        ]
      : [];
    return [
      ...(copy.isUpdateOffered ? updateAppActions() : []),
      ...(copy.isRunHereOffered
        ? [{ label: "Run on this computer", onclick: () => openRetryForm(true, copy.isImbueCloudUnusable) }]
        : []),
      ...retry,
      {
        label: "Dismiss",
        isDismissal: true,
        onclick: () => {
          if (!state.isActionPending) dismissAttempt();
        },
      },
    ];
  }

  /** An interrupted attempt's question: Retry, or Discard. A non-empty
   * `retryUnavailableReason` holds Retry, says why, and leaves Discard -- the
   * answer that works -- leading the row. */
  function interruptedActions(retryUnavailableReason: string): ErrorAction[] {
    const isRetryHeld = retryUnavailableReason !== "";
    const discard: ErrorAction = {
      id: "discard",
      label: "Discard",
      onclick: () => {
        if (!state.isActionPending) discardAttempt();
      },
    };
    const retry: ErrorAction = {
      id: "retry",
      label: "Retry",
      isDisabled: isRetryHeld,
      tooltip: isRetryHeld ? retryUnavailableReason : undefined,
      onclick: () => {
        if (!isRetryHeld) openRetryForm(false);
      },
    };
    return isRetryHeld ? [discard, retry] : [retry, discard];
  }

  /** A failed attempt: our sentence, its details and report button, and the ways forward it allows. */
  function failureTurns(
    workspaceName: string,
    failure: CreateAttemptFailure,
    request: CreateAttemptRequestSummary | null,
    logLines: readonly string[],
    retryUnavailableReason = "",
  ): m.Children[] {
    const copy = failureCopy(workspaceName, failure, request, retryUnavailableReason);
    const error: UserFacingError = { message: copy.message, details: failureDetails(failure.error, logLines) };
    return [failureTurn(error, failureActions(copy, retryUnavailableReason))];
  }

  /** An interrupted attempt: nothing failed outright, so it reads as a
   * warning the user answers -- or the discard it asked for, under way or
   * failed. */
  function interruptedTurns(workspaceName: string, retryUnavailableReason: string): m.Children[] {
    const discard = state.discard;
    if (discard?.phase === "removing") {
      return [
        agentTurn({
          key: "creation-interrupted",
          text: interruptedLine(retryUnavailableReason),
          startAtMs: FLOW_THINK_MS,
          isInstant: true,
        }),
        agentTurn({
          key: "creation-discarding",
          id: "discarding",
          text: `Removing what was left of ${workspaceName || "the workspace"}…`,
          startAtMs: 0,
          isInstant: true,
        }),
      ];
    }
    if (discard?.phase === "failed") {
      // The attempt is still there to be dealt with, so the question it was
      // answering stands beside the report.
      return [
        m(ChatError, {
          key: "creation-discard-failure",
          id: "discard-failed",
          error: discard.error,
          actions: interruptedActions(retryUnavailableReason),
        }),
      ];
    }
    return [
      m(ChatError, {
        key: "creation-interrupted",
        id: "creation-interrupted",
        tone: "warn",
        isBugReportOffered: false,
        error: { message: interruptedLine(retryUnavailableReason), details: "" },
        actions: interruptedActions(retryUnavailableReason),
      }),
    ];
  }

  /** A record-backed attempt with no live thread: the failure with its log tail, or the interrupted notice. */
  function recordTurns(record: NonNullable<CreateAttemptDetail["record"]>): m.Children[] {
    if (record.state === "failed") {
      const failure: CreateAttemptFailure = {
        error: record.error ?? "",
        errorKind: record.error_kind ?? "",
        quotaRefusal: record.quota_refusal ?? null,
        providerRemediation: record.provider_remediation ?? "",
      };
      return failureTurns(
        record.workspace_name,
        failure,
        record.request,
        record.log_tail,
        record.retry_unavailable_reason,
      );
    }
    return interruptedTurns(record.workspace_name, record.retry_unavailable_reason);
  }

  /** The recording of an app built in Studio, drawn the width of an agent turn. It opens enlarged on a click:
   *  it holds more detail than the column width shows. */
  function showcaseBlock(app: ShowcaseApp, arriveAtMs: number): m.Children {
    return m(
      "div",
      {
        key: `showcase-${app.id}`,
        "data-showcase": app.id,
        class:
          "start-chat-in mt-10 w-full max-w-[calc(100%-100px)] overflow-hidden rounded-lg border border-default " +
          "bg-surface-primary",
        style: `--start-chat-delay: ${arriveAtMs}ms; --start-chat-fade: 280ms;`,
      },
      [
        m(
          "button",
          {
            type: "button",
            "aria-label": "View the recording larger",
            class: "block w-full cursor-zoom-in",
            onclick: () => {
              state.isShowcaseLightboxOpen = true;
            },
          },
          m("img", { src: app.gifUrl, alt: app.label, class: "block w-full bg-fill-subtle object-contain" }),
        ),
      ],
    );
  }

  function readyTurn(startAtMs: number): m.Children {
    return agentTurn({
      key: "creation-ready",
      text: READY_LINE,
      startAtMs,
      class: "type-heading",
      marginClass: READY_MARGIN_CLASS,
    });
  }

  /**
   * A live attempt: the setup line, then either the live failure, or the recording under its intro line (for
   * an onboarding attempt) and the loading box, with the ready line under them once the attempt is done.
   */
  function liveTurns(
    request: CreateAttemptRequestSummary | null,
    workspaceName: string,
    isInstant: boolean,
  ): m.Children[] {
    const setupAt = isInstant ? 0 : FLOW_THINK_MS;
    const turns: m.Children[] = [
      agentTurn({ key: "creation-setup", text: SETUP_LINE, startAtMs: setupAt, isInstant }),
    ];
    if (state.isFailed && state.failure !== null) {
      turns.push(...failureTurns(workspaceName, state.failure, request, state.logLines));
      return turns;
    }
    const afterSetupAt = isInstant ? 0 : setupAt + streamDurationMs(SETUP_LINE) + FLOW_OPTIONS_GAP_MS;
    if (!isShowcaseShown()) {
      turns.push(loadingBox(afterSetupAt));
    } else {
      turns.push(agentTurn({ key: "creation-showcase-intro", text: SHOWCASE_INTRO_LINE, startAtMs: afterSetupAt, isInstant }));
      const showcaseAt = isInstant ? 0 : afterSetupAt + streamDurationMs(SHOWCASE_INTRO_LINE) + FLOW_OPTIONS_GAP_MS;
      turns.push(showcaseBlock(SHOWCASE_APP, showcaseAt));
      turns.push(loadingBox(isInstant ? 0 : showcaseAt + FLOW_THINK_MS));
    }
    if (state.isDone) turns.push(readyTurn(FLOW_THINK_MS));
    return turns;
  }

  /** The manifesto exchange, already on the page: every workspace's conversation opens with it. */
  function manifestoTurns(): m.Children[] {
    return [
      agentTurn({
        key: "manifesto-opener",
        text: MANIFESTO_OPENER,
        emphasis: MANIFESTO_OPENER_EMPHASIS,
        startAtMs: 0,
        isInstant: true,
      }),
      userTurn({ key: "manifesto-question", delayMs: 0, isInstant: true, text: MANIFESTO_QUESTION }),
      agentTurn({ key: "manifesto-answer", text: MANIFESTO_HEADING, startAtMs: 0, isInstant: true }),
      disclosureList({
        key: "manifesto-points",
        startAtMs: 0,
        isInstant: true,
        points: MANIFESTO_POINTS,
        openIds: state.openManifestoIds,
        onToggle: (id) => {
          if (state.openManifestoIds.has(id)) state.openManifestoIds.delete(id);
          else state.openManifestoIds.add(id);
        },
      }),
      agentTurn({
        key: "manifesto-reporting",
        text: REPORTING_ASK,
        startAtMs: 0,
        isInstant: true,
        more: {
          label: REPORTING_MORE_LABEL,
          detail: REPORTING_MORE_DETAIL,
          isOpen: state.isReportingMoreOpen,
          onToggle: () => {
            state.isReportingMoreOpen = !state.isReportingMoreOpen;
          },
        },
      }),
    ];
  }

  /** The page's own turns: the settings, then the attempt as it stands. */
  function creationTurns(detail: CreateAttemptDetail): m.Children[] {
    const isInstant = detail.kind === "record" || !state.isFromStartFlow;
    const request =
      detail.kind === "record" ? detail.record?.request : detail.live?.request;
    const workspaceName =
      (detail.kind === "record"
        ? detail.record?.workspace_name
        : detail.live?.workspace_name) ?? "";
    const summary =
      request !== undefined && request !== null
        ? [summaryTurn(request, isInstant)]
        : [];
    if (detail.kind === "record" && detail.record !== null) {
      return [...summary, ...recordTurns(detail.record)];
    }
    return [...summary, ...liveTurns(request ?? null, workspaceName, isInstant)];
  }

  /** Leave `state` behind: the page is being removed, or a later attempt is taking it over. */
  function stopWatching(): void {
    state.isStopped = true;
    if (state.isDone && !state.isEntryStarted) welcomeChatArrival.cancel();
    state.watcher?.stop();
    if (state.progressTimer !== null) clearInterval(state.progressTimer);
    if (state.washTimer !== null) clearTimeout(state.washTimer);
    if (state.discardTimer !== null) clearTimeout(state.discardTimer);
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
          void handOverWelcomeChat();
          startWatching();
        } else if (detail.kind === "gone") {
          getAppContext().shell.routeToHub();
        }
        m.redraw();
      })
      .catch(() => {
        if (state !== own) return;
        own.detail = { kind: "gone", live: null, record: null };
        getAppContext().shell.routeToHub();
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
      const prelude = [
        ...manifestoTurns(),
        ...(state.isFromStartFlow
          ? transcriptTurns(startFlow.state.entries, { isInstant: true, isPressable: false })
          : []),
      ];
      const turns = creationTurns(detail);
      return m(
        "div",
        { id: "creating", "data-agent-id": state.createAttemptId, class: TRANSCRIPT_COLUMN_CLASS },
        // Every child is keyed: Mithril rejects a fragment that mixes keyed
        // vnodes with holes, so the modal is appended only while open.
        [
          ...prelude,
          ...turns,
          scrollAnchor(prelude.length + turns.length),
          ...(state.isRetryFormOpen ? [retryForm()] : []),
          ...(state.isShowcaseLightboxOpen
            ? [
                m(ImageLightbox, {
                  key: "showcase-lightbox",
                  imageUrl: SHOWCASE_APP.gifUrl,
                  title: SHOWCASE_APP.label,
                  onClose: () => {
                    state.isShowcaseLightboxOpen = false;
                  },
                }),
              ]
            : []),
        ],
      );
    },
  };
};
