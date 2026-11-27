// The creation page (/creating/<create_attempt_id>), the one page every create attempt shows
// (specs/onboarding-tweaks, section 2): a heading, and under it the build's progress, or what went wrong and the
// ways forward.
//
// Everything it draws comes from the attempt's detail, so the page is the same whoever submitted the create and
// after any reload: a live attempt tracks the real create (status polling + the op-log SSE), a failed one offers
// Retry, Run on this computer and Dismiss, and one the app closed on offers Retry and Discard. Retry reopens the
// create form as a modal prefilled from the attempt's record.
//
// The page asks the desktop client to seed the new workspace's first chat as soon as it knows the attempt is
// live, so the workspace is seeded even if the page is left before it is ready. When the attempt is done the
// workspace loads under the shell's cover (models/firstRunCover.ts), which carries this page's heading on and
// says the workspace is ready, then lowers, once the workspace has its provider page, or its chat, on screen.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type {
  CreateAttemptDetail,
  CreateAttemptFailure,
  CreateAttemptOwner,
  CreateAttemptRequestSummary,
} from "../../models/create";
import { CreateAttemptWatcher, fetchCreateAttemptDetail } from "../../models/create";
import type { FailureCopy } from "../../models/createFailureCopy";
import { failureCopy, interruptedLine } from "../../models/createFailureCopy";
import type { ErrorAction, UserFacingError } from "../../models/errors";
import { errorFromResponse, fallbackErrorSentence } from "../../models/errors";
import { SETTING_UP_LINE, SETTING_UP_LONG_LINE, creationHeading, firstNameOf } from "../../models/firstRun";
import { firstRunCover } from "../../models/firstRunCover";
import { ProgressDisplay } from "../../models/progress";
import { postWelcomeChat, postWelcomeChatLanding } from "../../models/welcomeChat";
import { welcomeChatArrival } from "../../models/welcomeChatArrival";
import { defaultFetchJson } from "../../models/workspaceOptions";
import { updateAppActions } from "../components/ErrorDisplay";
import { Spinner } from "../components/Spinner";
import { createFormModal } from "./CreatePage";
import { CreateFailure } from "./create/CreateFailure";
import { firstRunWelcome, settingUpStatus } from "./create/FirstRunWelcome";

/** How often a running discard is checked on. */
const DISCARD_POLL_MS = 1000;
/** The most create-log lines a failure's details carry. */
const FAILURE_LOG_MAX_LINES = 1000;
/** How far from the log's end the reader may be and still count as reading its end. */
const LOG_PIN_SLACK_PX = 4;
/** The column a failure or a notice is read in, under the heading. */
const NOTICE_COLUMN_CLASS = "mt-[60px] w-full max-w-[720px] text-primary";

interface CreatingState {
  createAttemptId: string;
  detail: CreateAttemptDetail | null;
  watcher: CreateAttemptWatcher | null;
  isDone: boolean;
  isFailed: boolean;
  /** Why a live attempt failed, once it has. */
  failure: CreateAttemptFailure | null;
  /** The bar, fed by the status polls and extrapolated between them. */
  progress: ProgressDisplay;
  logLines: string[];
  isLogOpen: boolean;
  /** Whether the log is kept scrolled to its end as lines arrive: it is until the reader scrolls up. */
  isLogPinnedToEnd: boolean;
  /** How many lines the log was last pinned at, so a redraw with no new line moves nothing. */
  pinnedLogLineCount: number;
  isActionPending: boolean;
  isRetryFormOpen: boolean;
  /** The retry form runs the attempt on this computer ("Run on this computer"). */
  isRetryRunHere: boolean;
  /** The run-here form also turns Imbue Cloud backups and sharing off: the account can't use Imbue Cloud at all. */
  isRetryImbueCloudRefused: boolean;
  /** A discard of what an interrupted attempt left: running, or why it failed. */
  discard: { phase: "removing" } | { phase: "failed"; error: UserFacingError } | null;
  discardTimer: ReturnType<typeof setTimeout> | null;
  /** Set once the page is left or a later attempt takes it over, so a pending answer is dropped. */
  isStopped: boolean;
  progressTimer: ReturnType<typeof setInterval> | null;
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
    failure: null,
    progress: new ProgressDisplay(),
    logLines: [],
    isLogOpen: false,
    isLogPinnedToEnd: true,
    pinnedLogLineCount: 0,
    isActionPending: false,
    isRetryFormOpen: false,
    isRetryRunHere: false,
    isRetryImbueCloudRefused: false,
    discard: null,
    discardTimer: null,
    isStopped: false,
    progressTimer: null,
  };
}

function attemptIdOf(vnode: m.Vnode): string {
  return (vnode.attrs as { agentId?: string }).agentId ?? "";
}

/** The attempt's owner facts and its workspace's name, from whichever form of the detail carries them. */
function attemptFacts(detail: CreateAttemptDetail): { owner: CreateAttemptOwner; workspaceName: string } {
  const source = detail.kind === "record" ? detail.record : detail.live;
  return {
    owner: { is_first_workspace: source?.is_first_workspace ?? false, account_id: source?.account_id ?? "" },
    workspaceName: source?.workspace_name ?? "",
  };
}

/** The page's heading: the welcome, by the owner's first name, for a first workspace; else the workspace's name. */
export function headingFor(detail: CreateAttemptDetail): string {
  const { owner, workspaceName } = attemptFacts(detail);
  const entry = getAppContext().stores.accounts.accounts.find((account) => account.user_id === owner.account_id);
  const firstName =
    entry === undefined ? "" : firstNameOf({ displayName: entry.display_name ?? "", email: entry.email });
  return creationHeading(owner.is_first_workspace, firstName, workspaceName);
}

export const CreatingPage: m.ClosureComponent = () => {
  // Reassigned per attempt: the router keeps this instance across
  // /creating/<a> -> /creating/<b> (the retry's route change), so every helper
  // reads the variable rather than a captured object.
  let state: CreatingState = freshState("");

  /**
   * Ask for the workspace's first chat to be seeded. Asked once the page knows the attempt is live, while the
   * workspace is still being built, so the desktop client seeds it when the attempt finishes even if this page
   * has been left by then; asked again once the attempt is done, which answers with the chat's id. Answers null
   * when the seed failed, with the reason in the console.
   */
  function seedWelcomeChat(): Promise<string | null> {
    return postWelcomeChat(state.createAttemptId).then(
      (chatId) => (chatId === "" ? null : chatId),
      (error: unknown) => {
        console.warn("The new workspace's first chat could not be seeded:", error);
        return null;
      },
    );
  }

  /**
   * The create is done: once the workspace has its seed (which is what tells it to play its arrival), load the
   * workspace under the shell's cover, which carries this page's heading on and says it is ready, then lowers, only
   * when the workspace has its provider page, or its chat, on screen.
   */
  function beginEntry(redirectUrl: string): void {
    const own = state;
    const detail = own.detail;
    if (detail === null) return;
    const startedAtMs = Date.now();
    void seedWelcomeChat().then((chatId) => {
      if (state !== own || own.isStopped) return;
      firstRunCover.raise(own.createAttemptId, headingFor(detail));
      const workspaceId = workspaceIdFromRedirect(redirectUrl);
      if (chatId !== null && workspaceId !== null) {
        // Left alone, the chat's app only looks for the workspace's page every few seconds.
        const { shell } = getAppContext();
        shell.requestFocusChat(shell.stores.workspaces.toAgentScopedId(workspaceId), chatId);
      }
      void welcomeChatArrival.begin(Promise.resolve(chatId)).then((landing) => {
        firstRunCover.markReady(own.createAttemptId);
        if (landing === null) return;
        void postWelcomeChatLanding(own.createAttemptId, landing, Date.now() - startedAtMs).catch(
          (error: unknown) => {
            console.warn("The landing in the new workspace could not be recorded:", error);
          },
        );
      });
      enterWorkspaceFromRedirect(redirectUrl);
    });
  }

  function startWatching(): void {
    state.watcher = new CreateAttemptWatcher(state.createAttemptId, {
      onDone(redirectUrl) {
        state.isDone = true;
        beginEntry(redirectUrl);
      },
      onFailed(failure) {
        state.isFailed = true;
        state.failure = failure;
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

  /** Keep the log's end in view as lines arrive, unless the reader has scrolled up to read something. */
  function pinLogToEnd(element: HTMLElement): void {
    if (!state.isLogPinnedToEnd || state.logLines.length === state.pinnedLogLineCount) return;
    state.pinnedLogLineCount = state.logLines.length;
    element.scrollTop = element.scrollHeight;
  }

  function retryForm(): m.Children {
    return createFormModal({
      key: "creating-retry-form",
      id: "creating-retry-form",
      onClose: () => {
        state.isRetryFormOpen = false;
      },
      retryId: state.createAttemptId,
      initialPreset: state.isRetryRunHere ? "local" : undefined,
      isImbueCloudRefused: state.isRetryImbueCloudRefused,
      backdrop: "wash",
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

  /**
   * A failed attempt: our sentence, its details and report button, and the ways forward it allows.
   *
   * It carries the ids the Electron e2e workspace runner
   * (desktop_client/e2e_workspace_runner.py) polls to fail a create fast:
   * `#failure-view`, around its sentence `#error-message`, and -- named from
   * `#failure-view` as every error's details are -- the `#failure-view-details`
   * toggle and the `#failure-view-details-panel` holding the raw cause.
   */
  function failureNotice(
    workspaceName: string,
    failure: CreateAttemptFailure,
    request: CreateAttemptRequestSummary | null,
    logLines: readonly string[],
    retryUnavailableReason: string,
  ): m.Children {
    const copy = failureCopy(workspaceName, failure, request, retryUnavailableReason);
    return m(
      "div",
      { class: NOTICE_COLUMN_CLASS },
      m(CreateFailure, {
        id: "failure-view",
        textId: "error-message",
        error: { message: copy.message, details: failureDetails(failure.error, logLines) },
        actions: failureActions(copy, retryUnavailableReason),
      }),
    );
  }

  /** An interrupted attempt: nothing failed outright, so it reads as a
   * warning the user answers -- or the discard it asked for, under way or
   * failed. */
  function interruptedNotice(workspaceName: string, retryUnavailableReason: string): m.Children {
    const discard = state.discard;
    if (discard?.phase === "removing") {
      return m("div", { class: `${NOTICE_COLUMN_CLASS} text-center` }, [
        m("p", { class: "m-0" }, interruptedLine(retryUnavailableReason)),
        m("p", { id: "discarding", class: "mt-4" }, `Removing what was left of ${workspaceName || "the workspace"}…`),
      ]);
    }
    if (discard?.phase === "failed") {
      // The attempt is still there to be dealt with, so the question it was
      // answering stands beside the report.
      return m(
        "div",
        { class: NOTICE_COLUMN_CLASS },
        m(CreateFailure, {
          id: "discard-failed",
          error: discard.error,
          actions: interruptedActions(retryUnavailableReason),
        }),
      );
    }
    return m(
      "div",
      { class: NOTICE_COLUMN_CLASS },
      m(CreateFailure, {
        id: "creation-interrupted",
        tone: "warn",
        isBugReportOffered: false,
        error: { message: interruptedLine(retryUnavailableReason), details: "" },
        actions: interruptedActions(retryUnavailableReason),
      }),
    );
  }

  /** A record-backed attempt with no live create behind it: the failure with its log tail, or the interrupted notice. */
  function recordStatus(record: NonNullable<CreateAttemptDetail["record"]>): m.Children {
    if (record.state === "failed") {
      const failure: CreateAttemptFailure = {
        error: record.error ?? "",
        errorKind: record.error_kind ?? "",
        quotaRefusal: record.quota_refusal ?? null,
        providerRemediation: record.provider_remediation ?? "",
      };
      return failureNotice(
        record.workspace_name,
        failure,
        record.request,
        record.log_tail,
        record.retry_unavailable_reason,
      );
    }
    return interruptedNotice(record.workspace_name, record.retry_unavailable_reason);
  }

  /** A live attempt: the build's progress, or its failure. */
  function liveStatus(live: NonNullable<CreateAttemptDetail["live"]>): m.Children {
    if (state.isFailed && state.failure !== null) {
      return failureNotice(live.workspace_name, state.failure, live.request, state.logLines, "");
    }
    const percent = state.progress.percentAt(Date.now(), state.isDone);
    // A wait of minutes is not "just a sec": the line says so plainly and the estimate stands under the bar.
    const isLongWait = !state.isDone && state.progress.isOverAMinuteLeft();
    // Only a first workspace is talked to here; a later one's heading already says what is being set up.
    const line = !live.is_first_workspace ? "" : isLongWait ? SETTING_UP_LONG_LINE : SETTING_UP_LINE;
    return settingUpStatus(line, percent, [
      m("div", { class: "mt-4 flex items-center gap-6 type-body" }, [
        isLongWait
          ? m(
              "span",
              { id: "creating-remaining", class: "text-[color:var(--first-run-ink-muted)]" },
              state.progress.remainingText(),
            )
          : null,
        m(
          "button",
          {
            type: "button",
            class: "first-run-link first-run-link--plain",
            onclick: () => {
              state.isLogOpen = !state.isLogOpen;
            },
          },
          state.isLogOpen ? "Hide details" : "Show details",
        ),
      ]),
      state.isLogOpen
        ? m(
            "pre",
            {
              id: "logs",
              class:
                "mt-3 w-[min(720px,90vw)] type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-[22vh] " +
                "overflow-y-auto text-left",
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
    ]);
  }

  /** Leave `state` behind: the page is being removed, or a later attempt is taking it over. */
  function stopWatching(): void {
    state.isStopped = true;
    state.watcher?.stop();
    if (state.progressTimer !== null) clearInterval(state.progressTimer);
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
          void seedWelcomeChat();
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
      const status =
        detail === null || detail.kind === "gone"
          ? m("div", { class: "mt-10" }, m(Spinner, { size: "lg" }))
          : detail.kind === "record" && detail.record !== null
            ? recordStatus(detail.record)
            : detail.live !== null
              ? liveStatus(detail.live)
              : null;
      return firstRunWelcome({
        id: "creating",
        attemptId: state.createAttemptId,
        heading: detail === null || detail.kind === "gone" ? "" : headingFor(detail),
        status,
        overlays: state.isRetryFormOpen ? [retryForm()] : [],
      });
    },
  };
};
