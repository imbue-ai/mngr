// The styled error dialog's queue: any surface can raise one, and the Shell
// draws the one at the front. When no Shell is mounted to draw it, the error
// falls back to a native alert with its details written to the log.

import m from "mithril";
import { alertWithDetailsInLog, sentenceUnderTitle } from "./errors";
import type { ErrorAction, UserFacingError } from "./errors";

/** The title for a caller that knows something failed but not what to call it.
 * An error dialog is never untitled: its title is what names the failure, and
 * the sentence under it says why and what to do. */
export const FALLBACK_DIALOG_TITLE = "Hit a snag";

export interface ErrorDialogRequest {
  /** Set by the queue, so each dialog draws fresh (its See details shut). */
  id?: number;
  /** What failed, e.g. `Couldn't start "alpha"`. Give one wherever the caller
   * knows; without one the dialog falls back to FALLBACK_DIALOG_TITLE. */
  title?: string;
  error: UserFacingError;
  actions?: readonly ErrorAction[];
  isBugReportOffered?: boolean;
  workspaceAgentId?: string | null;
}

/**
 * A dialog for a failure whose sentence names what failed at its front
 * ("Couldn't start \"alpha\". The daemon refused."): the title takes that name
 * and the sentence keeps what follows it. Spread it into the request to add
 * actions or a workspace.
 */
export function titledError(
  title: string,
  error: UserFacingError,
): Pick<ErrorDialogRequest, "title" | "error"> {
  return {
    title,
    error: { ...error, message: sentenceUnderTitle(error.message, title) },
  };
}

/**
 * What the title and the sentence under it say together, for the native alert,
 * which has no title to draw: `titledError` took the lead naming the failure
 * off the sentence, so the alert has to put it back or say only the cause.
 * A request with no title of its own keeps its sentence as it stands --
 * FALLBACK_DIALOG_TITLE names nothing and belongs only on the dialog.
 */
function wholeSentence(request: ErrorDialogRequest): string {
  const { title, error } = request;
  if (title === undefined || error.message.startsWith(title)) {
    return error.message;
  }
  return `${title}. ${error.message}`;
}

export class ErrorDialogQueue {
  private readonly pending: ErrorDialogRequest[] = [];
  private hostCount = 0;
  private nextId = 0;

  get current(): ErrorDialogRequest | null {
    return this.pending[0] ?? null;
  }

  show(request: ErrorDialogRequest): void {
    if (this.hostCount === 0) {
      alertWithDetailsInLog({
        ...request.error,
        message: wholeSentence(request),
      });
      return;
    }
    this.nextId += 1;
    this.pending.push({ ...request, id: this.nextId });
    m.redraw();
  }

  /** Close the dialog on screen, bringing up the next one waiting. */
  dismiss(): void {
    if (this.pending.length === 0) return;
    this.pending.shift();
    m.redraw();
  }

  /** Called by the component that draws the dialogs, as it mounts. */
  attachHost(): void {
    this.hostCount += 1;
  }

  detachHost(): void {
    this.hostCount = Math.max(0, this.hostCount - 1);
  }
}

export const errorDialogs = new ErrorDialogQueue();

/** Raise the styled error dialog over whatever is on screen. */
export function showErrorDialog(request: ErrorDialogRequest): void {
  errorDialogs.show(request);
}
