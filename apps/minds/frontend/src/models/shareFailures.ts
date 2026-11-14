// What the share panel tells the user when a sharing request fails: one
// sentence of ours per reason the desktop names (sharing_handler.py's
// SharingFailureReason), worded for the operation that failed, with the
// route's own account behind "See details" and the buttons that fit it.

import type { ErrorRemedy } from "../generated/ui";
import {
  NO_RESPONSE_SENTENCE,
  TextResponseBody,
  fallbackErrorSentence,
  reasonFromBody,
  responseDetails,
} from "./errors";
import type {
  JsonObject,
  RequestOutcome,
  ResponseBody,
  UserFacingError,
} from "./errors";

/** What the panel was doing when the request failed. */
export type SharingOperation =
  | "load"
  | "publish"
  | "unpublish"
  | "save"
  | "invite"
  | "mobile_link"
  | "agents_load"
  | "agent_invite"
  | "agent_revoke"
  | "shares_load"
  | "share_remove";

/** The button that reads again after a failed read; none for a write. */
function rereadRemedies(operation: SharingOperation): readonly ShareRemedy[] {
  if (operation === "load" || operation === "shares_load") return ["retry"];
  if (operation === "agents_load") return ["reload_visiting_agents"];
  return [];
}

function isPublicationOperation(operation: SharingOperation): boolean {
  return operation === "publish" || operation === "unpublish";
}

function isAgentOperation(operation: SharingOperation): boolean {
  return (
    operation === "agents_load" ||
    operation === "agent_invite" ||
    operation === "agent_revoke"
  );
}

/** A button a share failure offers: one of the app's shared remedies, drawn
 * the same as everywhere else, or one only the share views can carry out. */
export type ShareRemedy =
  | ErrorRemedy
  | "retry"
  | "retry_publication"
  | "reload_visiting_agents"
  | "start_workspace"
  | "update_workspace"
  | "sign_in"
  | "open_account_settings"
  | "restart_sharing"
  | "review_shared_workspaces";

export interface ShareFailure {
  readonly error: UserFacingError;
  readonly remedies: readonly ShareRemedy[];
  readonly isBugReportOffered: boolean;
  /** The in-app page "review_shared_workspaces" opens, as the route named
   * it; "" when the failure offers no page. */
  readonly remedyPath?: string;
  /** The workspace could not be reached, which also explains any read of it
   * that failed alongside. */
  readonly isWorkspaceUnreachable?: boolean;
}

export interface ShareFailureContext {
  /** Whether the workspace is stopped, so a workspace that could not be
   * reached is offered a start. */
  readonly isWorkspaceStopped: boolean;
}

/** What the agent is asked when the sharing list inside the workspace cannot
 * be read. */
export const UNREADABLE_SHARING_LIST_ASSIST_DESCRIPTION =
  "Imbue Studio can't read this workspace's sharing list " +
  "(data/.secrets/share_grants.toml), so sharing can't be changed. Check that " +
  "file and fix it so it can be read again.";

/** What the agent is asked when the workspace's shared link never answered. */
export const LINK_NOT_ANSWERING_ASSIST_DESCRIPTION =
  "This workspace is shared, but its link hasn't answered for over five " +
  "minutes. Find out why the link can't be reached from the web and fix it.";

const WAITLISTED_SENTENCE =
  "Your account is still on the waitlist, so this workspace can't be shared " +
  "yet. Open the invite link from your invitation email to unlock it.";

interface Wording {
  readonly message: string;
  readonly remedies?: readonly ShareRemedy[];
  readonly isBugReportOffered?: boolean;
  readonly remedyPath?: string;
}

/** The failure for a sharing request that did not succeed. */
export function shareFailureFrom(
  operation: SharingOperation,
  outcome: RequestOutcome,
  context: ShareFailureContext,
): ShareFailure {
  const reason = outcome.status === 0 ? null : reasonFromBody(outcome.body);
  const wording: Wording =
    outcome.status === 0
      ? {
          message: NO_RESPONSE_SENTENCE,
          remedies: rereadRemedies(operation),
        }
      : (wordingForReason(reason, operation, outcome.body, context) ??
        fallbackWording(operation, false, context));
  return {
    error: { message: wording.message, details: responseDetails(outcome) },
    remedies: wording.remedies ?? [],
    isBugReportOffered: wording.isBugReportOffered ?? false,
    remedyPath: wording.remedyPath ?? "",
    isWorkspaceUnreachable: reason === "WORKSPACE_UNREACHABLE",
  };
}

/** The grant list could not be read while the rest of the status could. */
export function unreadableGrantListFailure(
  isPublished: boolean,
  context: ShareFailureContext,
): ShareFailure {
  if (context.isWorkspaceStopped)
    return {
      error: {
        message: "Start this workspace to see and edit who it's shared with.",
        details: "",
      },
      remedies: ["start_workspace"],
      isBugReportOffered: false,
    };
  // Only a published workspace admits anyone, so only then is there access
  // to reassure the reader about.
  const stillHasAccess = isPublished
    ? " Everyone already granted access still has it."
    : "";
  return {
    error: {
      message:
        "Couldn't read who this workspace is shared with, so the list can't " +
        `be edited right now.${stillHasAccess} Try again. If this persists, ` +
        "report a bug.",
      details: "",
    },
    remedies: ["retry"],
    isBugReportOffered: true,
  };
}

/** The status read could not tell a shared workspace from an unshared one. */
export function unknownPublicationFailure(reason: string): ShareFailure {
  if (reason === "OWNER_SIGNED_OUT")
    return {
      error: {
        message:
          "Sign in to the account that owns this workspace to see its sharing.",
        details: "",
      },
      remedies: ["sign_in"],
      isBugReportOffered: false,
    };
  return {
    error: {
      message: fallbackErrorSentence(
        "Couldn't check whether this workspace is shared",
      ),
      details: "",
    },
    remedies: ["retry"],
    isBugReportOffered: true,
  };
}

function wordingForReason(
  reason: string | null,
  operation: SharingOperation,
  body: ResponseBody,
  context: ShareFailureContext,
): Wording | null {
  switch (reason) {
    case "UNAVAILABLE":
      return {
        message:
          "Sharing isn't available right now. Restart Imbue Studio and try " +
          "again. If this persists, report a bug.",
        remedies: ["RESTART_APP"],
        isBugReportOffered: true,
      };
    case "WORKSPACE_UNKNOWN":
      return {
        message:
          "Imbue Studio can't find this workspace right now. Try again in a moment.",
        remedies: rereadRemedies(operation),
      };
    case "ACCOUNTS_UNREADABLE":
      return {
        message:
          "Couldn't check which accounts are signed in. Try again in a moment.",
        remedies: rereadRemedies(operation),
      };
    case "NOT_SIGNED_IN":
      return {
        message:
          operation === "shares_load" || operation === "share_remove"
            ? "This account isn't signed in any more. Sign in again to see its shared workspaces."
            : "Sign in to Imbue Cloud to share this workspace.",
        remedies: ["sign_in"],
      };
    case "OWNER_SIGNED_OUT":
      return {
        message: "Sign in to the account that owns this workspace to share it.",
        remedies: ["sign_in"],
      };
    case "NOT_LINKED":
      return {
        message: isAgentOperation(operation)
          ? "Link this workspace to your Imbue Cloud account before inviting agents into it."
          : "Link this workspace to your Imbue Cloud account before sharing it.",
        remedies: ["open_account_settings"],
      };
    case "SIGN_IN_EXPIRED":
      return {
        message:
          "Your Imbue Cloud sign-in has expired. *Sign in again, then try again.*",
        remedies: ["sign_in"],
      };
    case "EMAIL_NOT_VERIFIED":
      return {
        message:
          "Imbue Cloud hasn't verified your account's email address yet. " +
          "*Verify it, then try again.*",
      };
    case "WAITLISTED":
      return { message: WAITLISTED_SENTENCE };
    case "SHARE_LIMIT_REACHED": {
      // The shared-workspaces page is where a slot is freed, including one a
      // share left behind by a removed workspace still holds.
      const remedyPath = sharedWorkspacesPathFromBody(body);
      return {
        message: shareLimitSentence(body),
        remedies: remedyPath !== "" ? ["review_shared_workspaces"] : [],
        remedyPath,
      };
    }
    case "WORKSPACE_UNREACHABLE":
      return context.isWorkspaceStopped
        ? {
            message: "This workspace is stopped. *Start it, then try again.*",
            remedies: ["start_workspace"],
          }
        : {
            message: unreachableSentence(operation),
            remedies: isPublicationOperation(operation)
              ? ["retry_publication"]
              : [],
          };
    case "WORKSPACE_TOO_OLD":
      return {
        message:
          "This workspace is too old to be shared. *Update it, then try again.*",
        remedies: ["update_workspace"],
      };
    case "GRANTS_UNREADABLE":
      return {
        message:
          "This workspace's list of who it's shared with can't be read, so " +
          "sharing can't be changed right now.",
        remedies: ["ASK_AGENT"],
        isBugReportOffered: true,
      };
    case "WRITE_FAILED":
      return {
        message: fallbackErrorSentence(
          operation === "save"
            ? "Couldn't save this change to the workspace"
            : "Couldn't set this workspace up for sharing",
        ),
        isBugReportOffered: true,
      };
    case "ADDRESS_MOVE_FAILED":
      return {
        message:
          "This workspace's sharing moved to a new address, but the workspace " +
          "didn't get the new link, so links you shared don't work.",
        remedies: ["restart_sharing"],
        isBugReportOffered: true,
      };
    case "NOT_PUBLISHED":
      return { message: "*Turn on sharing before inviting anyone.*" };
    case "CONNECTOR_FAILED":
      return fallbackWording(operation, true, context);
    case "AGENTS_UNSUPPORTED":
      return {
        message:
          "This workspace is too old to invite agents into. *Update it, then try again.*",
        remedies: ["update_workspace"],
      };
    case "AGENT_INVITE_REFUSED":
      return { message: "Agents can't be invited to this app." };
    case "SHARE_NOT_SET_UP":
      return {
        message:
          "This workspace is shared, but its link isn't set up inside it, so " +
          "an agent couldn't reach it. Restart sharing, then invite the agent again.",
        remedies: ["restart_sharing"],
      };
    default:
      return null;
  }
}

/** What a failure with no reason of its own says, per operation. */
function fallbackWording(
  operation: SharingOperation,
  isImbueCloudFailure: boolean,
  context: ShareFailureContext,
): Wording {
  switch (operation) {
    case "load":
      return {
        message: fallbackErrorSentence(
          "Couldn't check who this workspace is shared with",
        ),
        remedies: ["retry"],
        isBugReportOffered: true,
      };
    case "publish":
      return {
        message: fallbackErrorSentence(
          isImbueCloudFailure
            ? "Couldn't turn sharing on — Imbue Cloud didn't finish setting up the link"
            : "Couldn't turn sharing on",
        ),
        remedies: ["retry_publication"],
        isBugReportOffered: true,
      };
    case "unpublish":
      return {
        message: fallbackErrorSentence("Couldn't turn sharing off"),
        remedies: ["retry_publication"],
        isBugReportOffered: true,
      };
    case "save":
      return {
        message: fallbackErrorSentence("Couldn't save this change"),
        isBugReportOffered: true,
      };
    case "invite":
      return {
        message: fallbackErrorSentence("Couldn't send the invitation"),
        isBugReportOffered: true,
      };
    case "mobile_link":
      return {
        message: fallbackErrorSentence("Couldn't send the email"),
        isBugReportOffered: true,
      };
    case "agents_load":
      // The list is read inside the workspace without starting it, so a
      // stopped one cannot answer.
      return context.isWorkspaceStopped
        ? {
            message: "Start this workspace to see the agents invited to it.",
            remedies: ["start_workspace"],
          }
        : {
            message: fallbackErrorSentence(
              "Couldn't load the agents invited to this workspace",
            ),
            remedies: ["reload_visiting_agents"],
            isBugReportOffered: true,
          };
    case "agent_invite":
      return {
        message: fallbackErrorSentence("Couldn't create the invitation"),
        isBugReportOffered: true,
      };
    case "agent_revoke":
      return {
        message: fallbackErrorSentence("Couldn't revoke this agent's access"),
        isBugReportOffered: true,
      };
    case "shares_load":
      return {
        message: fallbackErrorSentence(
          isImbueCloudFailure
            ? "Couldn't read this account's shared workspaces from Imbue Cloud"
            : "Couldn't load this account's shared workspaces",
        ),
        remedies: ["retry"],
        isBugReportOffered: true,
      };
    case "share_remove":
      return {
        message: fallbackErrorSentence("Couldn't remove this share"),
        isBugReportOffered: true,
      };
  }
}

/** A running workspace that a request couldn't reach, naming what the
 * request was for. */
function unreachableSentence(operation: SharingOperation): string {
  const purpose =
    operation === "publish"
      ? " to set up sharing"
      : operation === "save"
        ? " to save this change"
        : operation === "invite"
          ? " to send the invitation"
          : "";
  return `Couldn't reach this workspace${purpose}. *Make sure it's running, then try again.*`;
}

/** The page a full share quota's refusal names (`shared_workspaces_path`);
 * "" when the body names none. */
function sharedWorkspacesPathFromBody(body: ResponseBody): string {
  if (body === null || body instanceof TextResponseBody) return "";
  const path = body["shared_workspaces_path"];
  return typeof path === "string" ? path : "";
}

function shareLimitSentence(body: ResponseBody): string {
  const record: JsonObject =
    body === null || body instanceof TextResponseBody ? {} : body;
  const limit = typeof record["limit"] === "number" ? record["limit"] : null;
  const current =
    typeof record["current"] === "number" ? record["current"] : null;
  if (limit === null || current === null)
    return (
      "Your plan doesn't allow more shared workspaces. *Turn sharing off for " +
      "one of them, then try again.*"
    );
  const allowed = `${limit} shared ${limit === 1 ? "workspace" : "workspaces"}`;
  const shared = current === 1 ? "1 is" : `${current} are`;
  return (
    `Your plan allows ${allowed} and ${shared} shared already. *Turn sharing ` +
    "off for one of them, then try again.*"
  );
}
