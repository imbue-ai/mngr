// A workspace's suggestion switches (specs/chat-suggestions, 3.7): whether its chat offers suggested replies
// after a reply, and things to try on a new chat. The values live only in
// the workspace's chat app; the desktop client's /ui/api/workspaces/<id>/suggestion-settings routes pass each read
// and write on to it (ui_api_suggestion_settings.py).

import m from "mithril";
import type { RequestOutcome, RequestResult, UserFacingError } from "./errors";
import { errorFromSentenceResponse } from "./errors";
import type { FetchJson } from "./workspaceOptions";
import { defaultFetchJson } from "./workspaceOptions";

/** The switches' names on the wire, in the order the group lists them. */
export const SUGGESTION_SWITCH_KEYS = [
  "is_next_step_suggestions_enabled",
  "is_new_chat_suggestions_enabled",
] as const;
export type SuggestionSwitchKey = (typeof SUGGESTION_SWITCH_KEYS)[number];

/** What both routes answer: the switches as they stand in the workspace. */
export type SuggestionSettings = Record<SuggestionSwitchKey, boolean>;

/**
 * Where the group stands: `loading` until the workspace has answered, `ready` with its values, or `unavailable`
 * when it gave none (it is not running, or its chat app is from before the switches).
 */
export type SuggestionSettingsStatus = "loading" | "ready" | "unavailable";

const WRITE_FAILURE = "Couldn't save this workspace's suggestion settings";

export function suggestionSettingsUrl(agentId: string): string {
  return `/ui/api/workspaces/${encodeURIComponent(agentId)}/suggestion-settings`;
}

/** The switches out of a route's answer, or null when it does not carry every one as a boolean. */
export function suggestionSettingsFromBody(body: unknown): SuggestionSettings | null {
  if (body === null || typeof body !== "object") return null;
  const fields = body as Record<string, unknown>;
  const [nextStep, newChat] = SUGGESTION_SWITCH_KEYS.map((key) => fields[key]);
  if (typeof nextStep !== "boolean" || typeof newChat !== "boolean") return null;
  return {
    is_next_step_suggestions_enabled: nextStep,
    is_new_chat_suggestions_enabled: newChat,
  };
}

/** One workspace's switches as the Suggestions group draws them, and the writes its flips send. */
export class SuggestionSettingsModel {
  status: SuggestionSettingsStatus = "loading";
  settings: SuggestionSettings | null = null;
  /** Why the last flip did not take; cleared by the next one. */
  writeError: UserFacingError | null = null;
  /** The value each switch held before the write now out for it. */
  private readonly valueBeforeWrite = new Map<SuggestionSwitchKey, boolean>();
  private isStopped = false;

  constructor(
    readonly agentId: string,
    private readonly fetchJson: FetchJson = defaultFetchJson,
    private readonly redraw: () => void = () => m.redraw(),
  ) {}

  isWriting(key: SuggestionSwitchKey): boolean {
    return this.valueBeforeWrite.has(key);
  }

  async load(): Promise<void> {
    const result = await this.fetchJson(suggestionSettingsUrl(this.agentId));
    if (this.isStopped) return;
    const settings = result.ok ? suggestionSettingsFromBody(result.body) : null;
    this.settings = settings;
    this.status = settings === null ? "unavailable" : "ready";
    this.redraw();
  }

  /**
   * Flip one switch: shown at once, written at once, and put back with the reason if the write fails. A flip of
   * a switch whose write is still out is dropped.
   */
  async flip(key: SuggestionSwitchKey, isOn: boolean): Promise<void> {
    if (this.settings === null || this.isWriting(key)) return;
    this.valueBeforeWrite.set(key, this.settings[key]);
    this.settings = { ...this.settings, [key]: isOn };
    this.writeError = null;
    const result = await this.fetchJson(suggestionSettingsUrl(this.agentId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: isOn }),
    });
    if (this.isStopped) return;
    const valueBefore = this.valueBeforeWrite.get(key) ?? !isOn;
    this.valueBeforeWrite.delete(key);
    const answered = result.ok ? suggestionSettingsFromBody(result.body) : null;
    if (answered === null) {
      this.settings = { ...this.settings, [key]: valueBefore };
      this.writeError = errorFromSentenceResponse(WRITE_FAILURE, failedOutcome(result));
    } else {
      // The answer is the workspace's whole state, read before any write still out landed: those switches keep
      // what was flipped here until their own answers arrive.
      const stillOut = Object.fromEntries(
        [...this.valueBeforeWrite.keys()].map((pendingKey) => [pendingKey, this.settings?.[pendingKey]]),
      );
      this.settings = { ...answered, ...stillOut };
    }
    this.redraw();
  }

  /** The group is gone, or another workspace took it over: answers still out are dropped. */
  stop(): void {
    this.isStopped = true;
  }
}

/** A write's answer as a failure: a success whose body carried no switches says nothing usable either. */
function failedOutcome(result: RequestResult): RequestOutcome {
  return result.ok ? { status: result.status, body: null } : result;
}
