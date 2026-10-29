// The creation page (/creating/<create_attempt_id>): a create attempt's
// progress, told as the tail of a conversation. The manifesto exchange opens
// it (already on the page, since every workspace's conversation starts there),
// then a user turn restates the settings the attempt was submitted with, the
// agent says it is setting the workspace up, and a loading box tracks the real
// attempt (status polling + the op-log SSE). Reached from the start flow in
// the same session, the flow's transcript renders between the two.
//
// An attempt submitted with its onboarding questions on (the start flow's
// creates, or the form's "Use onboarding questions?") keeps the loading box
// out of sight while the agent asks whether the reader knows what to build,
// and, for one who does not, whether to see examples or describe a problem.
// The answer decides the welcome the workspace opens on; a workspace ready by
// then is entered at once, and one still being built is from then on watched
// through the revealed box.
//
// The whole conversation is handed over as the new workspace's first chat
// (models/welcomeChat.ts): as soon as the page knows the attempt's settings
// when it asks nothing, so it is seeded even if the page is left before the
// workspace is ready, and once the questions are answered when it asks them
// (or as the page is left, with the answers so far). When the attempt is
// ready the agent says so, the page waits for that seed, and the wash carries
// the app in; a failure or an interrupted record becomes an agent turn with
// Retry and Dismiss / Discard, the retry reopening the create form as a modal
// prefilled from the attempt's record.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { CreateAttemptDetail, CreateAttemptRequestSummary } from "../../models/create";
import { CreateAttemptWatcher, fetchCreateAttemptDetail } from "../../models/create";
import { ProgressDisplay } from "../../models/progress";
import type { BuildAnswerId, OnboardingChoice, PathAnswerId, ShowcaseApp } from "../../models/creationTranscript";
import {
  BUILD_ASK,
  BUILD_CHOICES,
  FINISHING_LINE,
  IMBUE_CLOUD_SUMMARY_LINE,
  INTERRUPTED_LINE,
  PATH_ASK,
  PATH_CHOICES,
  READY_LINE,
  SETUP_LINE,
  SHOWCASE_APP,
  SHOWCASE_READ_MS,
  SUMMARY_HEADING,
  choiceLabel,
  failureLine,
  onboardingIntent,
  summarySettingLines,
} from "../../models/creationTranscript";
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
import type { OnboardingAnswers } from "../../models/welcomeChat";
import { postWelcomeChat, welcomeChatBody } from "../../models/welcomeChat";
import { Button } from "../components/Button";
import { ImageLightbox } from "../components/ImageLightbox";
import { Notice } from "../components/Notice";
import { PageContainer } from "../components/Layout";
import { Spinner } from "../components/Spinner";
import { createFormModal } from "./CreatePage";
import { TRANSCRIPT_COLUMN_CLASS, agentTurn, answerRow, disclosureList, scrollAnchor, userTurn } from "./start/transcript";
import { transcriptTurns } from "./StartPage";

/** How long the ready line is left alone before the wash starts. */
const READY_HOLD_MS = 900;
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
  errorText: string;
  errorKind: string;
  stageText: string;
  /** The bar, fed by the status polls and extrapolated between them. */
  progress: ProgressDisplay;
  logLines: string[];
  isLogOpen: boolean;
  /** Whether the log is kept scrolled to its end as lines arrive: it is until the reader scrolls up. */
  isLogPinnedToEnd: boolean;
  /** How many lines the log was last pinned at, so a redraw with no new line moves nothing. */
  pinnedLogLineCount: number;
  /** The onboarding questions' answers, by choice id; null while a question is open or was never asked. */
  buildAnswer: BuildAnswerId | null;
  pathAnswer: PathAnswerId | null;
  /** Whether the loading box is on the page: at once for an attempt asking nothing, else once the answers came. */
  isBoxRevealed: boolean;
  /** Whether the conversation has reached the desktop client (a page asking questions hands it over once answered). */
  isWelcomeChatHandedOver: boolean;
  /** Whether the settings turn's lines are open under its heading. */
  isSummaryDetailsOpen: boolean;
  /** The manifesto points the reader has opened. */
  openManifestoIds: Set<string>;
  isReportingMoreOpen: boolean;
  /** The hand-off of the conversation to the new workspace, settled either way before the wash. */
  welcomeChat: Promise<void> | null;
  /** Set once the page is left or a later attempt takes it over, so a pending entry never fires. */
  isEntryCancelled: boolean;
  isActionPending: boolean;
  isRetryFormOpen: boolean;
  /** Whether the showcase recording is open enlarged over the page. */
  isShowcaseLightboxOpen: boolean;
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
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * The agent turn that reports a failed attempt. It keeps the ids the Electron
 * e2e workspace runner (desktop_client/e2e_workspace_runner.py) polls to fail
 * a create fast and to read the cause: `#failure-view` and `#error-message`.
 */
export function failureTurn(workspaceName: string, error: string): m.Children {
  return agentTurn({
    key: "creation-failed",
    id: "failure-view",
    textId: "error-message",
    text: failureLine(workspaceName, error),
    startAtMs: FLOW_THINK_MS,
    // An error is read, not followed, and can run to pages: streamed a
    // character at a time it reads as the app hanging.
    isInstant: true,
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
  if (errorKind === "GIT_REF_NOT_FOUND") {
    return m(Notice, { key: "creation-guidance", id: "git-ref-help", extra: "max-w-[calc(100%-100px)]" }, [
      "This repository has no branch or tag with that name. Clear the Branch field to use the repository's ",
      "default branch, or enter one that exists, then retry.",
    ]);
  }
  return null;
}

function freshState(createAttemptId: string): CreatingState {
  return {
    createAttemptId,
    detail: null,
    watcher: null,
    isDone: false,
    isFailed: false,
    redirectUrl: "",
    errorText: "",
    errorKind: "",
    stageText: "",
    progress: new ProgressDisplay(),
    logLines: [],
    isLogOpen: false,
    isLogPinnedToEnd: true,
    pinnedLogLineCount: 0,
    buildAnswer: null,
    pathAnswer: null,
    isBoxRevealed: false,
    isWelcomeChatHandedOver: false,
    isSummaryDetailsOpen: false,
    openManifestoIds: new Set<string>(),
    isReportingMoreOpen: false,
    welcomeChat: null,
    isEntryCancelled: false,
    isActionPending: false,
    isRetryFormOpen: false,
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

  /** When the ready line that starts streaming at ``readyStartAtMs`` has landed, and the hold before the entry begins. */
  function readyLandedAtMs(readyStartAtMs: number): number {
    return readyStartAtMs + streamDurationMs(READY_LINE);
  }

  /** When the agent's next turn arrives after an answer: at once, or after the recording the answer brought. */
  function afterAnswerAtMs(): number {
    return state.pathAnswer === "examples" ? FLOW_THINK_MS + SHOWCASE_READ_MS : FLOW_THINK_MS;
  }

  // The conversation reaches the workspace, then the workspace's color takes
  // the window and the shell enters the workspace while the cover is whole.
  function startEntry(): void {
    const own = state;
    void (own.welcomeChat ?? Promise.resolve()).then(() => {
      // The page was left, or a later attempt took it over, while the hand-off was in flight.
      if (own.isEntryCancelled) return;
      if (isReducedMotion()) {
        enter();
        return;
      }
      const accent = accentForAttempt();
      wash.start(
        accent,
        washOrigin(),
        { width: window.innerWidth, height: window.innerHeight },
        enter,
      );
    });
  }

  /** The ready turn, streaming from ``readyStartAtMs``, gets its moment, then the entry begins. */
  function scheduleEntry(readyStartAtMs: number): void {
    const own = state;
    state.washTimer = setTimeout(() => {
      own.washTimer = null;
      startEntry();
    }, readyLandedAtMs(readyStartAtMs) + READY_HOLD_MS);
  }

  /** Whether the attempt was submitted with the onboarding questions on. */
  function isDrivenByAgent(): boolean {
    return state.detail?.live?.request.is_onboarding_driven_by_agent ?? false;
  }

  /** Whether the page is still waiting on an answer that decides the welcome. */
  function isAwaitingAnswer(): boolean {
    return (
      isDrivenByAgent() &&
      onboardingIntent(state.buildAnswer, state.pathAnswer) === null
    );
  }

  function onboardingAnswers(): OnboardingAnswers {
    return { isDriven: isDrivenByAgent(), buildAnswer: state.buildAnswer, pathAnswer: state.pathAnswer };
  }

  /**
   * Hand the conversation so far to the new workspace as its first chat. For an attempt asking
   * nothing, posted once the page knows the attempt's settings, while the workspace is still being
   * built, so the desktop client seeds it when the attempt finishes even if this page has been
   * left by then; for one asking its questions, posted once they are answered (the welcome is
   * written for the answer), or as the page is left. Posted again once the attempt is done, which
   * waits for that seed. Best effort: a workspace that cannot take it (its chat app still down, an
   * older template) is entered all the same, with the reason in the console.
   */
  function handOverWelcomeChat(): Promise<void> {
    const request = state.detail?.live?.request;
    if (request === undefined || request === null) return Promise.resolve();
    state.isWelcomeChatHandedOver = true;
    const entries = state.isFromStartFlow ? startFlow.state.entries : [];
    const isCloudPreset = state.isFromStartFlow && startFlow.isSubmittedCreateCloudPreset;
    return postWelcomeChat(
      state.createAttemptId,
      welcomeChatBody(entries, request, isCloudPreset, onboardingAnswers()),
    ).then(
      () => undefined,
      (error: unknown) => {
        console.warn("The conversation could not be handed to the new workspace:", error);
      },
    );
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
        // An open question holds the entry: the answer decides the welcome, and settles it.
        if (isAwaitingAnswer()) return;
        state.welcomeChat = handOverWelcomeChat();
        scheduleEntry(FLOW_THINK_MS);
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
      onProgress(progress) {
        state.progress.apply(progress, Date.now());
      },
    });
    state.watcher.start();
    // Keep the bar moving between polls (the poll only redraws every 2s).
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

  function discardAttempt(): void {
    state.isActionPending = true;
    fetch(
      `/api/v1/workspaces/create-attempts/${encodeURIComponent(state.createAttemptId)}/discard`,
      {
        method: "POST",
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
      onSubmitted: (operationId: string) => {
        state.isRetryFormOpen = false;
        m.route.set(`/creating/${operationId}`);
      },
    });
  }

  /**
   * Retry / Dismiss (a failed attempt) or Retry / Discard (an interrupted one).
   * A non-empty `retryUnavailableReason` holds Retry and says why; the other
   * way out stays open.
   */
  function recoveryButtons(secondLabel: string, onSecond: () => void, retryUnavailableReason = ""): m.Children {
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
          isDisabled: retryUnavailableReason !== "",
          title: retryUnavailableReason === "" ? undefined : retryUnavailableReason,
          onPress: () => {
            if (retryUnavailableReason === "") state.isRetryFormOpen = true;
          },
        },
      ],
    });
  }

  /** The failure turn, followed by the recognized-error guidance when the error kind has some. */
  function failureTurns(workspaceName: string, error: string, errorKind: string): m.Children[] {
    const guidance = failureGuidance(errorKind);
    return [failureTurn(workspaceName, error), ...(guidance !== null ? [guidance] : [])];
  }

  /** A record-backed attempt with no live thread: the failure with its log tail, or the interrupted notice. */
  function recordTurns(record: NonNullable<CreateAttemptDetail["record"]>): m.Children[] {
    if (record.state === "failed") {
      const turns = failureTurns(record.workspace_name, record.error ?? "unknown error", record.error_kind ?? "");
      if (record.log_tail.length > 0) {
        turns.push(
          m(
            "pre",
            { key: "creation-log-tail", class: "mt-3 type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-64 overflow-y-auto max-w-[calc(100%-100px)]" },
            record.log_tail.join("\n"),
          ),
        );
      }
      turns.push(recoveryButtons("Dismiss", dismissAttempt, record.retry_unavailable_reason));
      return turns;
    }
    return [
      agentTurn({ key: "creation-interrupted", text: INTERRUPTED_LINE, startAtMs: FLOW_THINK_MS, isInstant: true }),
      recoveryButtons("Discard", discardAttempt, record.retry_unavailable_reason),
    ];
  }

  /** The row of answers to an onboarding question, as the user's turn. */
  function answerChoices<Id extends string>(key: string, delayMs: number, isInstant: boolean, choices: OnboardingChoice<Id>[], onAnswer: (id: Id) => void): m.Children {
    return answerRow({
      key,
      delayMs,
      isInstant,
      buttons: choices.map((choice) => ({
        id: choice.id,
        label: choice.label,
        isEmphasized: choice.isEmphasized,
        onPress: () => onAnswer(choice.id),
      })),
    });
  }

  /**
   * An answer settled where the questions lead: the welcome is decided, so the conversation is
   * handed over. A workspace already ready is entered now, with no loading box ever shown; one
   * still being built is watched through the box from here on.
   */
  function settleAnswers(): void {
    if (onboardingIntent(state.buildAnswer, state.pathAnswer) === null) return;
    state.welcomeChat = handOverWelcomeChat();
    if (state.isDone) {
      scheduleEntry(afterAnswerAtMs());
      return;
    }
    state.isBoxRevealed = true;
  }

  function answerBuild(id: BuildAnswerId): void {
    if (state.buildAnswer !== null) return;
    state.buildAnswer = id;
    settleAnswers();
  }

  function answerPath(id: PathAnswerId): void {
    if (state.pathAnswer !== null) return;
    state.pathAnswer = id;
    settleAnswers();
  }

  /**
   * The onboarding questions as far as they have been answered: the first question with its row of
   * answers, the answer once given, and for a reader who did not know yet the follow-up the same way.
   * Turns a press caused arrive on the press's clock; the first question follows the setup line.
   */
  function questionTurns(askAt: number, isInstant: boolean): m.Children[] {
    const turns: m.Children[] = [
      agentTurn({ key: "creation-build-ask", text: BUILD_ASK, startAtMs: askAt, isInstant }),
    ];
    if (state.buildAnswer === null) {
      const buttonsAt = isInstant ? 0 : askAt + streamDurationMs(BUILD_ASK) + FLOW_OPTIONS_GAP_MS;
      turns.push(answerChoices("creation-build-answers", buttonsAt, isInstant, BUILD_CHOICES, answerBuild));
      return turns;
    }
    turns.push(
      userTurn({ key: "creation-build-answer", delayMs: 0, text: choiceLabel(BUILD_CHOICES, state.buildAnswer) }),
    );
    if (state.buildAnswer === "yes") return turns;
    turns.push(agentTurn({ key: "creation-path-ask", text: PATH_ASK, startAtMs: FLOW_THINK_MS }));
    if (state.pathAnswer === null) {
      const buttonsAt = FLOW_THINK_MS + streamDurationMs(PATH_ASK) + FLOW_OPTIONS_GAP_MS;
      turns.push(answerChoices("creation-path-answers", buttonsAt, false, PATH_CHOICES, answerPath));
      return turns;
    }
    turns.push(
      userTurn({ key: "creation-path-answer", delayMs: 0, text: choiceLabel(PATH_CHOICES, state.pathAnswer) }),
    );
    if (state.pathAnswer === "examples") turns.push(showcaseBlock(SHOWCASE_APP, FLOW_THINK_MS));
    return turns;
  }

  /** The one app others have built, for a reader who asked to see examples: its recording with its title and
   *  description under it, drawn the width of an agent turn. The recording opens enlarged on a click: it holds
   *  more detail than the column width shows. */
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
            "aria-label": `View the ${app.title} recording larger`,
            class: "block w-full cursor-zoom-in",
            onclick: () => {
              state.isShowcaseLightboxOpen = true;
            },
          },
          m("img", { src: app.gifUrl, alt: app.title, class: "block w-full bg-fill-subtle object-contain" }),
        ),
        m("div", { class: "px-4 py-3" }, [
          m("div", { class: "type-body font-semibold text-primary" }, app.title),
          m("div", { class: "mt-0.5 type-helper text-secondary" }, app.description),
        ]),
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
   * A live attempt: the setup line, then either the live failure, or the onboarding questions when
   * the attempt asks them, and the loading box (at once for an attempt asking nothing; under "let me
   * just finish" once the questions were answered with the workspace still being built; never, when
   * it was ready by then), with the ready line under whatever is there once the attempt is done.
   */
  function liveTurns(workspaceName: string, isInstant: boolean): m.Children[] {
    const setupAt = isInstant ? 0 : FLOW_THINK_MS;
    const turns: m.Children[] = [
      agentTurn({ key: "creation-setup", text: SETUP_LINE, startAtMs: setupAt, isInstant }),
    ];
    if (state.isFailed) {
      turns.push(...failureTurns(workspaceName, state.errorText, state.errorKind));
      turns.push(recoveryButtons("Dismiss", dismissAttempt));
      return turns;
    }
    const afterSetupAt = isInstant ? 0 : setupAt + streamDurationMs(SETUP_LINE) + FLOW_OPTIONS_GAP_MS;
    if (!isDrivenByAgent()) {
      turns.push(loadingBox(afterSetupAt));
      if (state.isDone) turns.push(readyTurn(FLOW_THINK_MS));
      return turns;
    }
    turns.push(...questionTurns(afterSetupAt, isInstant));
    if (isAwaitingAnswer()) return turns;
    // The turn after the answer (the finishing line, or the ready line when the workspace was ready by then)
    // waits for the recording the answer brought, when it brought one.
    const nextAt = afterAnswerAtMs();
    if (state.isBoxRevealed) {
      turns.push(agentTurn({ key: "creation-finishing", text: FINISHING_LINE, startAtMs: nextAt }));
      turns.push(loadingBox(nextAt + streamDurationMs(FINISHING_LINE) + FLOW_OPTIONS_GAP_MS));
      if (state.isDone) turns.push(readyTurn(FLOW_THINK_MS));
      return turns;
    }
    if (state.isDone) turns.push(readyTurn(nextAt));
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
    return [...summary, ...liveTurns(workspaceName, isInstant)];
  }

  /**
   * Leave `state` behind: the page is being removed, or a later attempt is taking it over. A
   * conversation not yet handed over (the questions still open) goes now, with the answers so far,
   * so the workspace still opens on it.
   */
  function stopWatching(): void {
    state.isEntryCancelled = true;
    state.watcher?.stop();
    if (state.progressTimer !== null) clearInterval(state.progressTimer);
    if (state.washTimer !== null) clearTimeout(state.washTimer);
    if (state.detail?.kind === "live" && !state.isFailed && !state.isWelcomeChatHandedOver) {
      void handOverWelcomeChat();
    }
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
          // An attempt asking its questions hands the conversation over once they are answered.
          if (detail.live?.request.is_onboarding_driven_by_agent === true) own.isBoxRevealed = false;
          else {
            own.isBoxRevealed = true;
            void handOverWelcomeChat();
          }
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
                  title: SHOWCASE_APP.title,
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
