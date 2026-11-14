// Why a workspace's update did not go out: our sentence, what fits it (a
// workspace too old to update itself gets its two remedies as buttons, a
// lapsed login the restart, a failure we cannot explain Report a bug), and
// what the machine said behind "See details".

import m from "mithril";
import type { AskAgentModel } from "../../models/assistChat";
import type { ErrorAction } from "../../models/errors";
import {
  NOT_RUNNING_SCHEDULE_CODE,
  NO_RESPONSE_DISPATCH_CODE,
  RECONNECT_DISPATCH_CODE,
  TOO_OLD_DISPATCH_CODE,
  isDispatchFailureReportable,
} from "../../models/updates";
import type { UpdateDispatchError } from "../../models/updates";
import { AskAgentFailure, askAgentActions } from "./AskAgentButton";
import { ErrorNotice, restartAppActions } from "./ErrorDisplay";
import type { ShownError } from "./variantBesideErrors";

/** What the "Ask its agent" button asks a workspace too old to update itself. */
const TOO_OLD_ASSIST_DESCRIPTION =
  "Imbue Studio can't update this workspace because it is too old to update itself. Bring it up to the " +
  "newest version, keeping the user's work and settings.";

export interface UpdateDispatchErrorOptions {
  /** Unique on the page; the details toggle's id is built from it. */
  id: string;
  agentId: string;
  error: UpdateDispatchError;
  /** The too-old remedies; a surface without them shows that failure's sentence alone. */
  askAgent?: AskAgentModel;
  onCreateWorkspace?: () => void;
  /** Offered when a schedule found the workspace not running; omit it for
   * one that is running, which a start cannot help. */
  onStartWorkspace?: () => void;
  /** Sends the failed request again; offered only for a failure a second try
   * can clear (one we cannot explain, or one that got no answer). */
  onRetry?: () => void;
  /** On where the surface's own heading above this notice already carries the
   * error's mark, so the sentence here sits flush under it. */
  isTitleMarked?: boolean;
}

/** The failure in place, sentence first. `surfaceActions` are the surface's
 * own buttons for the next step, sharing the error's row: they lead it,
 * unless the error's remedy is the surer fix. */
export function updateDispatchErrorNotice(
  options: UpdateDispatchErrorOptions,
  surfaceActions: readonly ErrorAction[] = [],
): m.Children {
  return dispatchErrorNotice(
    options,
    updateDispatchErrorActions(options, surfaceActions),
  );
}

/** This failure as a panel button beside it weighs it: the sentence its
 * notice shows, paired with the row that same notice draws. */
export function updateDispatchShownError(
  options: UpdateDispatchErrorOptions,
  surfaceActions: readonly ErrorAction[] = [],
): ShownError {
  return {
    message: options.error.message,
    actions: updateDispatchErrorActions(options, surfaceActions),
  };
}

/**
 * The buttons of the failure's row, the surest fix first: a lapsed login's
 * restart (nothing else goes through until then); starting a stopped
 * workspace a schedule needs running, then the surface's own; a too-old
 * workspace's remedies (updating it in place cannot work); else the
 * surface's own, then a Try again for a failure a second try can clear.
 */
function updateDispatchErrorActions(
  options: UpdateDispatchErrorOptions,
  surfaceActions: readonly ErrorAction[] = [],
): ErrorAction[] {
  const { error, onRetry } = options;
  if (error.code === RECONNECT_DISPATCH_CODE) return restartAppActions();
  if (isWorkspaceStartTheFix(options))
    return [
      { label: "Start workspace", onclick: options.onStartWorkspace },
      ...surfaceActions,
    ];
  const remedies = tooOldRemedies(options);
  if (remedies !== null) return remedies;
  const isRetryable =
    error.code === "" || error.code === NO_RESPONSE_DISPATCH_CODE;
  return [
    ...surfaceActions,
    ...(isRetryable && onRetry !== undefined
      ? [{ label: "Try again", onclick: onRetry }]
      : []),
  ];
}

function dispatchErrorNotice(
  options: UpdateDispatchErrorOptions,
  actions: ErrorAction[],
): m.Children {
  const { id, agentId, error, isTitleMarked } = options;
  return m(ErrorNotice, {
    id,
    error: { message: error.message, details: error.detail },
    actions,
    isBugReportOffered: isDispatchFailureReportable(error.code),
    workspaceAgentId: agentId,
    isTitleMarked,
    footer: tooOldAskFailure(options),
  });
}

/** A schedule refused because the workspace isn't running, where this
 * surface can start it. */
function isWorkspaceStartTheFix(
  options: UpdateDispatchErrorOptions,
): options is UpdateDispatchErrorOptions & { onStartWorkspace: () => void } {
  return (
    options.error.code === NOT_RUNNING_SCHEDULE_CODE &&
    options.onStartWorkspace !== undefined
  );
}

/** The too-old remedies this surface offers, or null where it has none. */
function tooOldRemedyOptions(options: UpdateDispatchErrorOptions): {
  askAgent: AskAgentModel;
  onCreateWorkspace: () => void;
} | null {
  const { error, askAgent, onCreateWorkspace } = options;
  return error.code === TOO_OLD_DISPATCH_CODE &&
    askAgent !== undefined &&
    onCreateWorkspace !== undefined
    ? { askAgent, onCreateWorkspace }
    : null;
}

function askAgentId(id: string): string {
  return `${id}-ask-agent`;
}

/** A workspace too old to update itself: asking its agent to bring it up to
 * date, then a new workspace in its place. */
function tooOldRemedies(
  options: UpdateDispatchErrorOptions,
): ErrorAction[] | null {
  const remedies = tooOldRemedyOptions(options);
  if (remedies === null) return null;
  return [
    ...askAgentActions({
      id: askAgentId(options.id),
      model: remedies.askAgent,
      workspaceAgentId: options.agentId,
      description: TOO_OLD_ASSIST_DESCRIPTION,
      label: "Ask its agent",
    }),
    { label: "Create a new workspace", onclick: remedies.onCreateWorkspace },
  ];
}

/** A failed "Ask its agent" start, under the error's row. */
function tooOldAskFailure(options: UpdateDispatchErrorOptions): m.Children {
  const remedies = tooOldRemedyOptions(options);
  if (remedies === null) return null;
  return m(AskAgentFailure, {
    id: askAgentId(options.id),
    model: remedies.askAgent,
    workspaceAgentId: options.agentId,
    isBoxed: false,
  });
}
