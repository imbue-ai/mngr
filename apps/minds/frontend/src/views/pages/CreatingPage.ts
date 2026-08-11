// The /creating/<create_attempt_id> page: live progress for an in-flight
// create (status polling + the op-log SSE, which stays SSE by decision), the
// failure view with recognized-error guidance, the onboarding walkthrough
// that plays while the machine sets up (port of Creating.jinja + onboarding.js,
// see ./creating/OnboardingWalkthrough.ts), and the record-backed detail for
// attempts with no live thread (interrupted: retry/discard; failed: error +
// persisted log tail + dismiss). Port of creating.js + create_attempt_record.js.

import m from "mithril";
import { getAppContext } from "../../app-context";
import type { CreateAttemptDetail, LiveCreateAttemptDetail } from "../../models/create";
import { CreateAttemptWatcher, fetchCreateAttemptDetail, progressForElapsed } from "../../models/create";
import { Button } from "../components/Button";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import { OnboardingWalkthrough } from "./creating/OnboardingWalkthrough";

const DEFAULT_EXPECTED_DURATION_SECONDS = 60;

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
  isActionPending: boolean;
  progressTimer: ReturnType<typeof setInterval> | null;
}

function enterWorkspaceFromRedirect(redirectUrl: string): void {
  // redirect_url is the /goto/<host-id>/ URL; the shell resolves either
  // coordinate, so extract the id and enter in-app.
  const match = redirectUrl.match(/\/goto\/((?:agent|host)-[a-f0-9]+)\//i);
  if (match) {
    getAppContext().shell.enterWorkspace(match[1]);
  } else {
    window.location.href = redirectUrl;
  }
}

export const CreatingPage: m.ClosureComponent = () => {
  const state: CreatingState = {
    createAttemptId: "",
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
    isActionPending: false,
    progressTimer: null,
  };

  function startWatching(): void {
    state.watcher = new CreateAttemptWatcher(state.createAttemptId, {
      // The walkthrough owns entry from here (diving into whatever picture
      // is on screen on its way, or going straight in from the tips step);
      // it reads isReady off this same state on its next render.
      onDone(redirectUrl) {
        state.isDone = true;
        state.redirectUrl = redirectUrl;
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

  function failureView(
    workspaceName: string,
    logTail: string[] | null,
    errorText: string,
    errorKind: string,
  ): m.Children {
    return m("div", { id: "failure-view", class: "flex flex-col gap-4 max-w-[640px] mx-auto pt-12" }, [
      m("h1", { class: "type-heading text-primary" }, `Could not create ${workspaceName || "the machine"}`),
      m(Notice, { variant: "error" }, m("span", { id: "error-message" }, errorText || "unknown error")),
      errorKind === "GITHUB_AUTH_REQUIRED"
        ? m(Notice, { id: "github-auth-help" }, [
            "This repository looks private. Install the GitHub app or use a repository URL that includes ",
            "credentials, then retry.",
          ])
        : null,
      errorKind === "GIT_AUTH_REQUIRED"
        ? m(Notice, { id: "git-auth-help" }, [
            "This git host rejected anonymous access. Use a repository URL that includes credentials, then retry.",
          ])
        : null,
      logTail !== null && logTail.length > 0
        ? m(
            "pre",
            { class: "type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-64 overflow-y-auto" },
            logTail.join("\n"),
          )
        : null,
      m("div", { class: "flex gap-3" }, [
        m(
          Button,
          {
            variant: "secondary",
            onclick: () => m.route.set(`/create?retry=${encodeURIComponent(state.createAttemptId)}`),
          },
          "Retry",
        ),
        m(
          Button,
          {
            variant: "danger",
            id: "create-attempt-dismiss-btn",
            disabled: state.isActionPending,
            onclick: dismissAttempt,
          },
          "Dismiss",
        ),
      ]),
    ]);
  }

  function progressView(workspaceName: string, live: LiveCreateAttemptDetail | null): m.Children {
    const elapsedSeconds = (Date.now() - state.startedAtMs) / 1000;
    const expectedDurationSeconds = live?.expected_duration_seconds ?? DEFAULT_EXPECTED_DURATION_SECONDS;
    const percent = state.isDone ? 100 : Math.min(99.5, progressForElapsed(elapsedSeconds, expectedDurationSeconds));
    return [
      // The progress block keeps its natural height; the walkthrough below it
      // takes whatever is left (see the root's column comment).
      m("div", { id: "progress-view", class: "shrink-0 flex flex-col gap-4 max-w-[640px] mx-auto" }, [
        m("h1", { class: "type-heading text-primary text-center" }, `Setting up ${workspaceName || "your machine"}`),
        m(
          "div",
          { class: "h-1.5 bg-fill-subtle rounded-full overflow-hidden" },
          m("div", {
            id: "bar-fill",
            class: "h-full rounded-full bg-accent transition-[width] duration-300 ease-out",
            style: `width: ${percent.toFixed(1)}%`,
          }),
        ),
        m("p", { id: "stage", class: "type-helper text-secondary text-center min-h-5" }, state.stageText),
        m(
          "div",
          { class: "text-center" },
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
                // A share of the window rather than a fixed height: this panel
                // is the biggest claim on a budget the whole column has to fit
                // inside, and how much there is to claim depends on how tall
                // the window is. The walkthrough gives up the difference.
                class: "type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-[22vh] overflow-y-auto",
                onupdate: (vnode) => {
                  const element = vnode.dom as HTMLElement;
                  element.scrollTop = element.scrollHeight;
                },
              },
              state.logLines.join("\n"),
            )
          : null,
      ]),
      m(OnboardingWalkthrough, {
        isRemote: live?.is_remote ?? false,
        onboardingServices: live?.onboarding_services ?? [],
        isReady: state.isDone,
        onEnter: () => {
          if (state.redirectUrl) enterWorkspaceFromRedirect(state.redirectUrl);
        },
      }),
    ];
  }

  function recordView(detail: CreateAttemptDetail): m.Children {
    const record = detail.record;
    if (record === null) return null;
    if (record.state === "failed") {
      // Derive the error from the record here rather than writing component
      // state during render (view code must stay side-effect free).
      return failureView(record.workspace_name, record.log_tail, record.error ?? "unknown error", record.error_kind ?? "");
    }
    return m("div", { class: "flex flex-col gap-4 max-w-[640px] mx-auto pt-12" }, [
      m("h1", { class: "type-heading text-primary" }, `${record.workspace_name} was interrupted`),
      m(Notice, { variant: "warn" }, [
        "The app closed while this machine was being created. You can retry the create (pre-filled with the ",
        "same settings) or discard the leftover partial machine.",
      ]),
      m("div", { class: "flex gap-3" }, [
        m(
          Button,
          {
            variant: "primary",
            disabled: state.isActionPending,
            onclick: () => m.route.set(`/create?retry=${encodeURIComponent(state.createAttemptId)}`),
          },
          "Retry",
        ),
        m(Button, { variant: "danger", disabled: state.isActionPending, onclick: discardAttempt }, "Discard"),
      ]),
    ]);
  }

  return {
    oninit(vnode) {
      state.createAttemptId = (vnode.attrs as { agentId?: string }).agentId ?? "";
      fetchCreateAttemptDetail(state.createAttemptId)
        .then((detail) => {
          state.detail = detail;
          if (detail.kind === "live") {
            startWatching();
          } else if (detail.kind === "gone") {
            m.route.set("/");
          }
          m.redraw();
        })
        .catch(() => {
          state.detail = { kind: "gone", live: null, record: null };
          m.route.set("/");
        });
    },
    onremove() {
      state.watcher?.stop();
      if (state.progressTimer !== null) clearInterval(state.progressTimer);
    },
    view() {
      const detail = state.detail;
      if (detail !== null && detail.kind !== "record" && !state.isFailed) {
        // The walkthrough is laid out to fit the window rather than to scroll:
        // this column is exactly as tall as the scroll card it sits in, the
        // progress block above takes its natural height, and #onboarding takes
        // what is left -- scaling the illustration into whatever that comes to
        // (see OnboardingWalkthrough). Opening the logs is just another claim
        // on the same budget: the picture gets smaller, the page does not grow.
        return m(
          "div",
          {
            id: "creating",
            "data-agent-id": state.createAttemptId,
            class: "h-full flex flex-col gap-4 px-6 py-6",
          },
          progressView(detail.live?.workspace_name ?? "", detail.live),
        );
      }
      // The failure and interrupted-record views are ordinary pages: they hold
      // as much text as the create produced, so they scroll if they are tall.
      return m(PageContainer, { id: "creating", "data-agent-id": state.createAttemptId }, [
        detail === null
          ? m("div", { class: "flex justify-center pt-24" }, m(Spinner, { size: "lg" }))
          : detail.kind === "record"
            ? recordView(detail)
            : failureView(detail.live?.workspace_name ?? "", null, state.errorText, state.errorKind),
      ]);
    },
  };
};
