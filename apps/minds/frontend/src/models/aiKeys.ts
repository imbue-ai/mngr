// The workspace AI-key mint page's state: the page's context read and the
// mint request, each with the error the page shows when it fails.

import m from "mithril";
import type { ResponseBody, UserFacingError } from "./errors";
import {
  TextResponseBody,
  bodyFromText,
  errorFromSentenceResponse,
  fallbackErrorSentence,
  jsonFromText,
  readResponseBody,
  responseDetails,
} from "./errors";
import { isReconnectNeeded, reconnectError } from "./reconnect";

export interface AiKeysContext {
  workspace_id: string;
  workspace_display_name: string;
  account_email: string;
  error_message: string;
}

type FetchLike = typeof fetch;

export class AiKeysMintModel {
  context: AiKeysContext | null = null;
  loadError: UserFacingError | null = null;
  /** The last load or mint was refused for this window's lapsed login, not a failed request. */
  isWindowLoginLapsed = false;
  isMinting = false;
  hasMintedOnce = false;
  statusMessage = "";
  mintError: UserFacingError | null = null;
  credentialBlob = "";

  private readonly fetchImpl: FetchLike;
  private readonly redraw: () => void;

  constructor(
    // A plain-call wrapper: passing the global `fetch` itself would invoke it
    // with the model as its receiver ("Illegal invocation" in browsers).
    fetchImpl: FetchLike = (input, init) => fetch(input, init),
    redraw: () => void = m.redraw,
  ) {
    this.fetchImpl = fetchImpl;
    this.redraw = redraw;
  }

  async load(workspace: string): Promise<void> {
    this.loadError = null;
    this.isWindowLoginLapsed = false;
    this.context = null;
    this.redraw();
    let status = 0;
    let text = "";
    try {
      const response = await this.fetchImpl(
        `/ui/api/ai-keys?workspace=${encodeURIComponent(workspace)}`,
        {
          credentials: "same-origin",
        },
      );
      status = response.status;
      text = await response.text().catch(() => "");
    } catch {
      // No answer: status 0 says so.
    }
    const payload = status === 200 ? jsonFromText(text) : null;
    if (
      payload !== null &&
      typeof payload === "object" &&
      !(payload instanceof TextResponseBody)
    ) {
      this.context = payload as AiKeysContext;
    } else {
      const outcome = { status, body: bodyFromText(text) };
      this.isWindowLoginLapsed = isReconnectNeeded(outcome);
      this.loadError = this.isWindowLoginLapsed
        ? reconnectError(outcome)
        : {
            message: fallbackErrorSentence("Couldn't load this page"),
            details: responseDetails(outcome),
          };
    }
    this.redraw();
  }

  async mint(): Promise<void> {
    const context = this.context;
    if (context === null || this.isMinting) return;
    this.isMinting = true;
    this.statusMessage = "Creating key...";
    this.mintError = null;
    this.isWindowLoginLapsed = false;
    this.redraw();
    let response: Response | null = null;
    try {
      response = await this.fetchImpl("/settings/ai-keys/mint", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: context.workspace_id }),
      });
    } catch {
      // No answer; handled below.
    }
    const body = response === null ? null : await readResponseBody(response);
    const credentials = credentialsIn(body);
    if (response === null) {
      this.statusMessage = "";
      this.mintError = {
        message:
          "Imbue Studio didn't respond. Select Create key again in a moment.",
        details: "No response.",
      };
    } else if (response.status !== 200 || credentials === "") {
      this.statusMessage = "";
      const outcome = { status: response.status, body };
      // The mint route's only 403 is its refusal of this window's lapsed login.
      this.isWindowLoginLapsed = response.status === 403;
      this.mintError = this.isWindowLoginLapsed
        ? reconnectError(outcome)
        : errorFromSentenceResponse(
            "Couldn't create a key for this workspace",
            outcome,
          );
    } else {
      // Always reveal the blob so the user can copy manually if the
      // clipboard write is rejected (insecure context, permissions).
      this.credentialBlob = credentials;
      this.hasMintedOnce = true;
      try {
        await navigator.clipboard.writeText(credentials);
        this.statusMessage =
          "Credentials copied to your clipboard. Paste them into the workspace sign-in dialog.";
      } catch {
        this.statusMessage =
          "Key created. Copy the credentials below and paste them into the workspace sign-in dialog.";
      }
    }
    this.isMinting = false;
    this.redraw();
  }
}

/** The minted credentials a JSON answer carries, or "" when it carries none. */
function credentialsIn(body: ResponseBody): string {
  if (body === null || body instanceof TextResponseBody) return "";
  const credentials = body["credentials"];
  return typeof credentials === "string" ? credentials : "";
}
