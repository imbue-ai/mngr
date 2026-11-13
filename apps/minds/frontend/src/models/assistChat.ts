// Starting a seeded agent chat inside a workspace: POST /help/assist, which
// opens a new chat there seeded with "/assist <description>" and brings its
// window up in the workspace.

import m from "mithril";
import {
  errorFromResponse,
  isRequestFailureReportable,
  readResponseBody,
  routeSentenceFromBody,
} from "./errors";
import type { RequestResult, UserFacingError } from "./errors";

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
      // A refusal (the workspace has no assist skill, an empty request) is a
      // sentence the route wrote for the user, and retrying cannot clear it.
      // A 4xx without one (a proxy's plain-text 404) is an unexplained
      // failure like any other.
      const isRefusal = outcome.status >= 400 && outcome.status < 500;
      const refusal = isRefusal
        ? routeSentenceFromBody(outcome.body, "") || null
        : null;
      this.isRefused = refusal !== null;
      this.isFailureReportable =
        !this.isRefused && isRequestFailureReportable(outcome);
      this.error = errorFromResponse(
        "Couldn't start an agent in this workspace",
        outcome,
        { plainReason: refusal },
      );
    }
    this.redraw();
  }

  private redraw(): void {
    (this.options.redraw ?? m.redraw)();
  }
}
