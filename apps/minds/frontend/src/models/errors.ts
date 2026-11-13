// What a failure tells the user: our sentence on top, and everything
// underneath it -- the status, the server's text, a command's output -- kept
// for the collapsed "See details" and for a bug report about it.

import type { ErrorRemedy } from "../generated/ui";

/** A failure as the user is told about it. */
export interface UserFacingError {
  /** Our sentence, the only text on the visible line. */
  message: string;
  /** Everything we have underneath it; "" when there is nothing more. */
  details: string;
}

/** A button offered beside an error: the remedy that fits it (Try again,
 * Sign in, Start workspace...). An error's first fix is the one it mainly
 * asks for, and is drawn as the primary button. */
export interface ErrorAction {
  label: string;
  onclick: () => void;
  id?: string;
  /** Shown but not pressable right now; `tooltip` says why. */
  isDisabled?: boolean;
  /** Pressed and still running: a spinner in place of the label. */
  isBusy?: boolean;
  /** A fix that destroys something (a Deny): drawn in the danger color
   * wherever it sits among the fixes, rather than as the main button. */
  isDestructive?: boolean;
  /** A way out rather than a fix (Dismiss, Discard): drawn as a ghost after
   * the fixes, and never the error's main button or its loudest. */
  isDismissal?: boolean;
  tooltip?: string;
}

/** A JSON object, its fields unchecked until something reads them. */
export type JsonObject = { readonly [key: string]: unknown };

/** A failed request's body: its JSON object, a TextResponseBody for anything
 * else it sent, or null when it sent nothing. */
export type ResponseBody = JsonObject | TextResponseBody | null;

/** A request's status (0 when no response arrived) and, when it failed, its
 * body. */
export interface RequestOutcome {
  status: number;
  body: ResponseBody;
}

/** What a request helper resolves with: the parsed answer when it succeeded,
 * else the failure's outcome. */
export type RequestResult =
  | { ok: true; status: number; body: unknown }
  | ({ ok: false } & RequestOutcome);

/** The sentence for a request to Imbue Studio on this computer that got no
 * answer. Never an internet claim: the request only goes to the app's own
 * local server. */
export const NO_RESPONSE_SENTENCE =
  "Imbue Studio didn't respond. Try again in a moment.";

/** The sentence for a request refused because this window's own login with
 * Imbue Studio on this computer has lapsed; only a restart brings it back. */
export const WINDOW_LOGIN_LAPSED_SENTENCE =
  "Imbue Studio needs to restart to reconnect this window.";

/** A response body that was not a JSON object (a proxy's or the app-wide
 * 500's plain text), kept under `error` so a caller that shows `error` as the
 * reason still finds it. Unlike a JSON body's `error`, it is never a sentence a
 * route wrote for the user. */
export class TextResponseBody {
  readonly error: string;

  constructor(text: string) {
    this.error = text;
  }
}

function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** A failed response's body as the error helpers read it; null when it sent
 * none (or the body could not be read). */
export async function readResponseBody(
  response: Response,
): Promise<ResponseBody> {
  return bodyFromText(await response.text().catch(() => ""));
}

/** A failed response's body from its text: a JSON object as itself, any other
 * text (JSON that is not an object included, which no route answers with) as a
 * TextResponseBody, and null for "". */
export function bodyFromText(text: string): ResponseBody {
  const parsed = jsonFromText(text);
  if (parsed instanceof TextResponseBody || isJsonObject(parsed)) return parsed;
  return text === "" ? null : new TextResponseBody(text);
}

/** A successful response's body from its text: its JSON, whatever its shape,
 * a TextResponseBody when it is not JSON, and null for "". */
export function jsonFromText(text: string): unknown {
  if (text === "") return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return new TextResponseBody(text);
  }
}

/** The keys a response body carries its own sentences under, most telling first. */
const BODY_SENTENCE_KEYS = ["error", "detail", "message"] as const;

export function errorMessageFromBody(
  body: ResponseBody,
  fallback: string,
): string {
  if (body instanceof TextResponseBody) return body.error || fallback;
  for (const key of BODY_SENTENCE_KEYS) {
    const value = stringField(body, key);
    if (value.length > 0) return value;
  }
  return fallback;
}

/** The sentence a route's JSON refusal words itself with (its error, detail
 * or message), else `fallback`; a text body's text is never one. */
export function routeSentenceFromBody(
  body: ResponseBody,
  fallback: string,
): string {
  return body instanceof TextResponseBody
    ? fallback
    : errorMessageFromBody(body, fallback);
}

/** A string field of a JSON body; "" for anything else, a text body included. */
function stringField(body: ResponseBody, key: string): string {
  if (body === null || body instanceof TextResponseBody) return "";
  const value = body[key];
  return typeof value === "string" ? value : "";
}

/** The `reason` a route names its failure by, or null when it names none. */
export function reasonFromBody(body: ResponseBody): string | null {
  return stringField(body, "reason") || null;
}

/** The machine-readable `code` a route's refusal carries, or null when it has none. */
export function errorCodeFromBody(body: ResponseBody): string | null {
  return stringField(body, "code") || null;
}

/** The refusing route's own account of why (its `detail`), or "" when it gave
 * none. */
export function refusalDetailFromBody(body: ResponseBody): string {
  return stringField(body, "detail");
}

/** Whether a failed request's error asks for a bug report: not one that got
 * no answer, whose sentence already says to try again in a moment. */
export function isRequestFailureReportable(outcome: RequestOutcome): boolean {
  return outcome.status !== 0;
}

export interface RetryOptions {
  /** Whether the user can try the failed step again, by a Try again the error
   * offers or by repeating what they did; true unless said otherwise. The
   * fallback sentences ask them to try again only when they can. */
  isRetryable?: boolean;
}

/** "<what failed>. Try again. If this persists, report a bug.", without the
 * "Try again." when nothing can be tried again. */
export function fallbackErrorSentence(
  whatFailed: string,
  { isRetryable = true }: RetryOptions = {},
): string {
  return isRetryable
    ? `${whatFailed}. Try again. If this persists, report a bug.`
    : `${whatFailed}. If this persists, report a bug.`;
}

/** The status and whatever the response said, as one block of text. */
export function responseDetails(outcome: RequestOutcome): string {
  if (outcome.status === 0) return "No response.";
  const statusLine = `HTTP ${outcome.status}`;
  const bodyText = bodyAsText(outcome.body);
  return bodyText ? `${statusLine}\n${bodyText}` : statusLine;
}

function bodyAsText(body: ResponseBody): string {
  if (body === null) return "";
  if (body instanceof TextResponseBody) return body.error;
  const lines: string[] = [];
  const shownKeys = new Set<string>();
  // The body's own sentences first, readable as prose; then whatever else it
  // carried, whole.
  for (const key of BODY_SENTENCE_KEYS) {
    const value = body[key];
    // A sentence key the body sent as a string is this loop's whether or not
    // it holds a sentence: an empty one left to the branch below would print
    // as a line of JSON that says nothing.
    if (typeof value !== "string") continue;
    shownKeys.add(key);
    if (value !== "" && !lines.includes(value)) lines.push(value);
  }
  const rest = Object.fromEntries(
    Object.entries(body).filter(([key]) => !shownKeys.has(key)),
  );
  if (Object.keys(rest).length > 0) lines.push(JSON.stringify(rest));
  return lines.join("\n");
}

export interface ResponseErrorOptions extends RetryOptions {
  /** A reason the caller knows is fit to show, used as the sentence. */
  plainReason?: string | null;
}

/**
 * The error for a failed request: `plainReason` when the caller has one it
 * knows is fit to show, else the no-answer sentence for a request that got no
 * response, else the fallback sentence naming `whatFailed`. The response always
 * goes into the details.
 */
export function errorFromResponse(
  whatFailed: string,
  outcome: RequestOutcome,
  { plainReason = null, isRetryable = true }: ResponseErrorOptions = {},
): UserFacingError {
  const message =
    plainReason ??
    (outcome.status === 0 && isRetryable
      ? NO_RESPONSE_SENTENCE
      : fallbackErrorSentence(whatFailed, { isRetryable }));
  return { message, details: responseDetails(outcome) };
}

/**
 * The error for a refusal from a route that words its own failures: the
 * `error` it answers with is already our sentence, so it is the visible line,
 * and the status and its `detail` go behind See details. A response that
 * carries no sentence (an empty or text body, the app-wide 500) reads as
 * `whatFailed`'s fallback, and no response at all as the no-answer sentence.
 */
export function errorFromSentenceResponse(
  whatFailed: string,
  outcome: RequestOutcome,
  options: RetryOptions = {},
): UserFacingError {
  const sentence =
    outcome.status === 0 ? "" : stringField(outcome.body, "error");
  if (sentence === "") return errorFromResponse(whatFailed, outcome, options);
  const detail = refusalDetailFromBody(outcome.body);
  return {
    message: sentence,
    details: responseDetails({
      status: outcome.status,
      body: detail ? { detail } : null,
    }),
  };
}

// A Record, so a remedy the server adds fails to compile here until it is known.
const KNOWN_REMEDIES: Record<ErrorRemedy, true> = {
  RESTART_APP: true,
  UPDATE_APP: true,
  ASK_AGENT: true,
};

/** The remedy a refusal names beside its sentence (the button that fixes
 * it), or null for none or for one this build does not know. */
export function remedyFromBody(body: ResponseBody): ErrorRemedy | null {
  const remedy = stringField(body, "remedy");
  return isKnownRemedy(remedy) ? remedy : null;
}

function isKnownRemedy(value: string): value is ErrorRemedy {
  return Object.hasOwn(KNOWN_REMEDIES, value);
}

/** The error for a call that threw (a bridge call, a parse): the fallback
 * sentence, with what was thrown as the details. */
export function errorFromException(
  whatFailed: string,
  thrown: unknown,
  options: RetryOptions = {},
): UserFacingError {
  return {
    message: fallbackErrorSentence(whatFailed, options),
    details: thrown instanceof Error ? thrown.message : String(thrown),
  };
}

/**
 * The sentence to show under a dialog title that already names the failure.
 *
 * Our sentences carry what failed at the front ("Couldn't start \"alpha\". The
 * daemon refused."), because most of them are shown with nothing above them to
 * say it. An error dialog puts that in its title, so the lead comes off here
 * rather than being said twice -- leaving the cause, or, when there was none
 * to give, the fallback's "Try again. If this persists, report a bug."
 */
export function sentenceUnderTitle(message: string, title: string): string {
  const lead = `${title}. `;
  return message.startsWith(lead) ? message.slice(lead.length) : message;
}

/**
 * The copy markup an error sentence is written in, as one span: a `command`
 * set as code, or *the ask* the user is to follow, which may hold a command.
 *
 * Splitting on it leaves the text between at the even indices and the span
 * itself at the odd ones. One pattern for both delimiters, so the scan takes
 * whichever opens first and an asterisk inside a command belongs to the
 * command rather than opening an ask.
 */
const COPY_MARKUP_PATTERN = /(`[^`]+`|\*(?:`[^`]+`|[^*])+\*)/;

/** A stretch of an error sentence: the plain text between the markup, a
 * `command`, or *the ask*. A span's `text` is what the delimiters held, so an
 * ask's may hold markup of its own. */
export type CopyMarkupSpan =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "ask"; text: string };

/** A sentence read as the spans it is written in, in order. The one place the
 * markup is parsed: everything that shows or weighs a sentence reads it from
 * here. */
export function copyMarkupSpans(message: string): CopyMarkupSpan[] {
  return message.split(COPY_MARKUP_PATTERN).map((chunk, index) => {
    if (index % 2 === 0) return { kind: "text", text: chunk };
    const text = chunk.slice(1, -1);
    return chunk.startsWith("`")
      ? { kind: "code", text }
      : { kind: "ask", text };
  });
}

/** A sentence with its copy markup taken off, for somewhere that shows text
 * and not our markup: a bug report, a native alert, an OS notification. */
export function plainErrorText(message: string): string {
  return copyMarkupSpans(message)
    .map((span) =>
      // A command is taken as it was written; an ask may still hold one.
      span.kind === "ask" ? plainErrorText(span.text) : span.text,
    )
    .join("");
}

/** The text a bug report carries for an error: the sentence the user saw,
 * then everything under it. */
export function bugReportTextFor(error: UserFacingError): string {
  const message = plainErrorText(error.message);
  return error.details ? `${message}\n\n${error.details}` : message;
}

/**
 * Tell the user about a failure through a native alert, for when the styled
 * error display cannot be shown. The alert stays one sentence of ours plus
 * "See the log for details.", and the details go to the log instead (the
 * renderer console, which the desktop app writes to disk).
 */
export function alertWithDetailsInLog(error: UserFacingError): void {
  console.error(`[error] ${bugReportTextFor(error)}`);
  window.alert(`${plainErrorText(error.message)} See the log for details.`);
}
