// Starting a seeded agent chat inside a workspace: POST /help/assist, which
// opens a new chat there seeded with "/assist <description>" and brings its
// window up in the workspace.

import m from "mithril";
import {
  errorFromResponse,
  errorFromSentenceResponse,
  fallbackErrorSentence,
  isRequestFailureReportable,
  readResponseBody,
  reasonFromBody,
  routeSentenceFromBody,
} from "./errors";
import type { RequestOutcome, RequestResult, UserFacingError } from "./errors";

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export interface AssistChatRequest {
  workspaceAgentId: string;
  /** What the agent is asked to help with; it becomes the chat's first message. */
  description: string;
}

export const defaultFetch: FetchLike = (url, init) =>
  fetch(url, { credentials: "same-origin", ...init });

/** Ask the workspace for the chat. Resolves (never rejects): status 0 when no
 * response arrived. */
export async function startAssistChat(
  request: AssistChatRequest,
  fetcher: FetchLike = defaultFetch,
): Promise<RequestResult> {
  let response: Response;
  try {
    response = await fetcher("/help/assist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        description: request.description,
        workspace_agent_id: request.workspaceAgentId,
      }),
    });
  } catch {
    return { ok: false, status: 0, body: null };
  }
  if (response.ok) return { ok: true, status: response.status, body: null };
  return {
    ok: false,
    status: response.status,
    body: await readResponseBody(response),
  };
}

/** Why a start failed, as the route names it; null for a refusal that names
 * none (a bad request) or no answer at all. */
export type AssistChatFailureReason =
  "TOO_OLD" | "START_FAILED" | "UNREACHABLE" | "SPAWN_FAILED";

const FAILURE_REASONS: readonly AssistChatFailureReason[] = [
  "TOO_OLD",
  "START_FAILED",
  "UNREACHABLE",
  "SPAWN_FAILED",
];

export function assistChatFailureReason(
  outcome: RequestOutcome,
): AssistChatFailureReason | null {
  const reason = reasonFromBody(outcome.body);
  return FAILURE_REASONS.find((known) => known === reason) ?? null;
}

const SENTENCE_BY_REASON: Record<AssistChatFailureReason, string> = {
  TOO_OLD:
    "This workspace can't start a helper agent yet. *Update it, then try again.*",
  START_FAILED: fallbackErrorSentence("Couldn't start this workspace"),
  UNREACHABLE: fallbackErrorSentence(
    "Couldn't reach this workspace to start an agent",
  ),
  SPAWN_FAILED: fallbackErrorSentence(
    "Couldn't start an agent in this workspace",
  ),
};

/** What a failed start tells the user: the sentence for the route's reason;
 * for any other refusal, the JSON `error` sentence the route wrote for the
 * user (retrying cannot clear it); else the no-answer sentence or the
 * fallback. The response goes behind the details. */
export function assistChatError(outcome: RequestOutcome): UserFacingError {
  const whatFailed = "Couldn't start an agent in this workspace";
  const reason = assistChatFailureReason(outcome);
  if (reason !== null)
    return errorFromResponse(whatFailed, outcome, {
      plainReason: SENTENCE_BY_REASON[reason],
    });
  const isRefusal = outcome.status >= 400 && outcome.status < 500;
  return isRefusal
    ? errorFromSentenceResponse(whatFailed, outcome)
    : errorFromResponse(whatFailed, outcome);
}

export interface AskAgentModelOptions {
  fetcher?: FetchLike;
  redraw?: () => void;
  /** Runs once the chat exists; the button's owner decides where to go next. */
  onStarted: (workspaceAgentId: string) => void;
}

/** One "Ask the agent" button's state: idle, starting, or failed with an error
 * to show beside it. */
export class AskAgentModel {
  isBusy = false;
  error: UserFacingError | null = null;
  /** The route refused the last start for a reason a second press can't
   * change, so the button is no longer offered. */
  isRefused = false;
  /** The last start failed in a way a bug report can help with: not a
   * refusal, and not one that got no response. */
  isFailureReportable = false;
  private readonly options: AskAgentModelOptions;

  constructor(options: AskAgentModelOptions) {
    this.options = options;
  }

  async start(request: AssistChatRequest): Promise<void> {
    if (this.isBusy) return;
    this.isBusy = true;
    this.error = null;
    this.isRefused = false;
    this.isFailureReportable = false;
    this.redraw();
    const outcome = await startAssistChat(request, this.options.fetcher);
    this.isBusy = false;
    if (outcome.ok) {
      this.options.onStarted(request.workspaceAgentId);
    } else {
      // A refusal (too old for a helper, an empty request) can't be cleared
      // by pressing again. A 4xx without the route's sentence (a proxy's
      // plain-text 404) is an unexplained failure like any other.
      const isRefusal = outcome.status >= 400 && outcome.status < 500;
      this.isRefused =
        isRefusal && routeSentenceFromBody(outcome.body, "") !== "";
      this.error = assistChatError(outcome);
      this.isFailureReportable =
        !this.isRefused && isRequestFailureReportable(outcome);
    }
    this.redraw();
  }

  private redraw(): void {
    (this.options.redraw ?? m.redraw)();
  }
}
