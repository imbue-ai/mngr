// Model for the Permissions tab's Client permissions section and for the
// prompt a workspace's page raises when it first asks for one (the
// client-permissions spec, sections 5.5 and 5.6).
//
// A client permission is a capability of this computer (phase 1: the
// microphone) that a workspace's pages may use, decided per workspace by this
// install. The decision lives in the backend's per-workspace install store,
// never in the workspace and never on another computer. Both surfaces here
// write through the same PUT, which answers with the refreshed view: the tab
// adopts it, as every other write in the Permissions tab does, and the prompt
// writes BEFORE it answers main, so a page that asks again at once is answered
// from the store.
//
// Loading is separate from PermissionsModel's latchkey read: nothing here
// touches the workspace's machine or its gateway, so the section renders when
// neither can be reached.

import m from "mithril";
import type {
  ClientPermissionAnswer,
  ClientPermissionAsk,
} from "../electron-bridge";
import { electronBridge } from "../electron-bridge";
import type {
  ClientPermissionDecision,
  UiClientPermissionRow,
  UiClientPermissionWriteRequest,
  UiClientPermissions,
} from "../generated/ui";
import type { FetchJson } from "./workspaceOptions";
import { errorMessageFromBody } from "./errors";
import { defaultFetchJson } from "./workspaceOptions";

export type ClientPermissionsLoadStatus =
  "idle" | "loading" | "load_failed" | "ready";

export interface ClientPermissionsModelOptions {
  fetchJson?: FetchJson;
  redraw?: () => void;
}

/** A row's control states (D14). "default" is the deferring state:
 * the workspace holds no value of its own and the Imbue default decides. */
export type ClientPermissionChoice = "default" | ClientPermissionDecision;

export const CLIENT_PERMISSION_CHOICES: readonly ClientPermissionChoice[] = [
  "default",
  "ask",
  "granted",
  "denied",
];

const DECISION_LABELS: Record<ClientPermissionDecision, string> = {
  ask: "Ask",
  granted: "Allow",
  denied: "Don't allow",
};

export function clientPermissionDecisionLabel(
  decision: ClientPermissionDecision,
): string {
  return DECISION_LABELS[decision];
}

/** What the Imbue default resolves to for a row. While the row defers, that is
 * its resolved decision; once it holds a value of its own the default is hidden
 * behind it, and phase 1 ships one default, ask. */
export function defaultDecisionOf(
  row: UiClientPermissionRow,
): ClientPermissionDecision {
  return row.stored === null ? row.decision : "ask";
}

export function selectedChoiceOf(
  row: UiClientPermissionRow,
): ClientPermissionChoice {
  return row.stored ?? "default";
}

/** What choosing a state stores: null makes the workspace defer again. */
export function storedValueForChoice(
  choice: ClientPermissionChoice,
): ClientPermissionDecision | null {
  return choice === "default" ? null : choice;
}

/** The control's label for a choice. The deferring state says what the
 * default currently resolves to, so the row reads "nothing chosen here;
 * Studio will ask". */
export function clientPermissionChoiceLabel(
  choice: ClientPermissionChoice,
  row: UiClientPermissionRow,
): string {
  if (choice === "default")
    return `Use the default (${clientPermissionDecisionLabel(defaultDecisionOf(row))})`;
  return clientPermissionDecisionLabel(choice);
}

function clientPermissionsUrl(agentId: string): string {
  return `/ui/api/workspaces/${encodeURIComponent(agentId)}/client-permissions`;
}

/** Store one decision for a workspace, or with null make it defer to the
 * default again, answering the refreshed view. Rejects with the server's
 * reason when the write was refused. */
export async function putClientPermission(
  fetchJson: FetchJson,
  agentId: string,
  permission: string,
  decision: ClientPermissionDecision | null,
): Promise<UiClientPermissions> {
  const result = await fetchJson(
    `${clientPermissionsUrl(agentId)}/${encodeURIComponent(permission)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        decision,
      } satisfies UiClientPermissionWriteRequest),
    },
  );
  if (!result.ok)
    throw new Error(errorMessageFromBody(result.body, `HTTP ${result.status}`));
  return result.body as UiClientPermissions;
}

/** Load + write state for one workspace's client permissions. */
export class ClientPermissionsModel {
  readonly agentId: string;
  status: ClientPermissionsLoadStatus = "idle";
  data: UiClientPermissions | null = null;
  errorMessage = "";
  /** The permission whose write is in flight, or null. One at a time: the
   * answer is the whole view, so two in flight would fight over the screen. */
  private busyPermission: string | null = null;
  private readonly fetchJsonImpl: FetchJson;
  private readonly redrawImpl: () => void;

  constructor(
    agentId: string,
    dependencies: ClientPermissionsModelOptions = {},
  ) {
    this.agentId = agentId;
    this.fetchJsonImpl = dependencies.fetchJson ?? defaultFetchJson;
    this.redrawImpl = dependencies.redraw ?? m.redraw;
  }

  /** Start the first read; later calls are no-ops, so reopening the tab
   * neither refetches nor blanks the section. */
  ensureLoaded(): void {
    if (this.status !== "idle") return;
    void this.load();
  }

  async load(): Promise<void> {
    this.status = "loading";
    this.errorMessage = "";
    this.redrawImpl();
    const result = await this.fetchJsonImpl(clientPermissionsUrl(this.agentId));
    if (!result.ok) {
      this.status = "load_failed";
      this.errorMessage = errorMessageFromBody(
        result.body,
        `HTTP ${result.status}`,
      );
      this.redrawImpl();
      return;
    }
    this.data = result.body as UiClientPermissions;
    this.status = "ready";
    this.redrawImpl();
  }

  isWriting(): boolean {
    return this.busyPermission !== null;
  }

  isRowBusy(permission: string): boolean {
    return this.busyPermission === permission;
  }

  clearErrorMessage(): void {
    if (this.errorMessage === "") return;
    this.errorMessage = "";
    this.redrawImpl();
  }

  /** Store a decision for one permission (null: defer to the default again)
   * and adopt the answered view. A second click while a write runs is dropped
   * rather than queued, so the stored state never races itself. Resolves to
   * whether the change was stored. */
  async setDecision(
    permission: string,
    decision: ClientPermissionDecision | null,
  ): Promise<boolean> {
    if (this.busyPermission !== null) return false;
    this.busyPermission = permission;
    this.errorMessage = "";
    this.redrawImpl();
    try {
      this.data = await putClientPermission(
        this.fetchJsonImpl,
        this.agentId,
        permission,
        decision,
      );
      this.status = "ready";
      return true;
    } catch (error) {
      // The last good view stays on screen: a refused write changed nothing.
      this.errorMessage =
        "Could not save the change: " +
        (error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      this.busyPermission = null;
      this.redrawImpl();
    }
  }
}

export interface ClientPermissionPromptModelOptions {
  fetchJson?: FetchJson;
  redraw?: () => void;
  /** How an answer reaches main: the bridge, unless a test listens itself. */
  answer?: (answer: ClientPermissionAnswer) => void;
}

/** The prompt a workspace's page raised for a client permission, as this
 * window shows it: one at a time, the rest waiting their turn.
 *
 * An answer with Remember checked is stored before main hears it, so the
 * store already holds it when the page's request resolves. A write that fails
 * still answers main, as an unremembered answer, so the page's request never
 * hangs on a store that would not take it; the failure stays on screen until
 * it is acknowledged. Closing without an answer is a one-time Don't allow. */
export class ClientPermissionPromptModel {
  /** The ask on screen, or null. */
  current: ClientPermissionAsk | null = null;
  /** Whether the answer is to be stored for this workspace on this computer.
   * Starts checked on every ask (D11). */
  isRemembered = true;
  /** The answer being stored, while its write is in flight. */
  pendingDecision: ClientPermissionAnswer["decision"] | null = null;
  /** Why the last answer could not be remembered. The answer itself has
   * reached main, as a one-time one. */
  errorMessage = "";
  private readonly waiting: ClientPermissionAsk[] = [];
  private readonly fetchJsonImpl: FetchJson;
  private readonly redrawImpl: () => void;
  private readonly answerImpl: (answer: ClientPermissionAnswer) => void;

  constructor(dependencies: ClientPermissionPromptModelOptions = {}) {
    this.fetchJsonImpl = dependencies.fetchJson ?? defaultFetchJson;
    this.redrawImpl = dependencies.redraw ?? m.redraw;
    this.answerImpl =
      dependencies.answer ??
      ((answer) => electronBridge.answerClientPermission(answer));
  }

  get isOpen(): boolean {
    return this.current !== null;
  }

  get isBusy(): boolean {
    return this.pendingDecision !== null;
  }

  /** Show an ask, or hold it until the one on screen is answered. */
  show(ask: ClientPermissionAsk): void {
    if (this.current !== null) {
      this.waiting.push(ask);
      return;
    }
    this.present(ask);
  }

  setRemembered(isRemembered: boolean): void {
    this.isRemembered = isRemembered;
    this.redrawImpl();
  }

  /** Answer the ask on screen with the Remember checkbox as it stands. */
  async choose(decision: ClientPermissionAnswer["decision"]): Promise<void> {
    const ask = this.current;
    if (ask === null || this.isBusy || this.errorMessage !== "") return;
    if (!this.isRemembered) {
      this.answerImpl({
        promptId: ask.promptId,
        decision,
        isRemembered: false,
      });
      this.advance();
      return;
    }
    this.pendingDecision = decision;
    this.redrawImpl();
    try {
      await putClientPermission(
        this.fetchJsonImpl,
        ask.workspaceId,
        ask.permission,
        decision,
      );
    } catch (error) {
      this.pendingDecision = null;
      this.answerImpl({
        promptId: ask.promptId,
        decision,
        isRemembered: false,
      });
      this.errorMessage =
        `Your answer applies this time, but couldn't be remembered: ` +
        (error instanceof Error ? error.message : String(error));
      this.redrawImpl();
      return;
    }
    this.pendingDecision = null;
    this.answerImpl({ promptId: ask.promptId, decision, isRemembered: true });
    this.advance();
  }

  /** Close the prompt without an answer (Escape, the backdrop, the close X):
   * a one-time Don't allow. After a failed remember the answer is already
   * given, so this only puts the failure away. Nothing closes while a write
   * is in flight: its answer is about to be sent. */
  dismiss(): void {
    const ask = this.current;
    if (ask === null || this.isBusy) return;
    if (this.errorMessage === "")
      this.answerImpl({
        promptId: ask.promptId,
        decision: "denied",
        isRemembered: false,
      });
    this.advance();
  }

  private present(ask: ClientPermissionAsk): void {
    this.current = ask;
    this.isRemembered = true;
    this.errorMessage = "";
    this.redrawImpl();
  }

  private advance(): void {
    this.current = null;
    this.errorMessage = "";
    const next = this.waiting.shift();
    if (next !== undefined) this.present(next);
    else this.redrawImpl();
  }
}

/** The one prompt per window. */
export const clientPermissionPrompt = new ClientPermissionPromptModel();
