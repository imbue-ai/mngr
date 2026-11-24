// What the creation page says when a create did not finish: the failure sentence and the ways forward that fit
// its cause, and the notice for an attempt the app closed on. Pure functions over the attempt detail, so the page
// says the same thing for a live attempt and for one read back from its record.

import type { CreateAttemptFailure, CreateAttemptQuotaRefusal, CreateAttemptRequestSummary } from "./create";
import { fallbackErrorSentence } from "./errors";

const INTERRUPTED_LEAD = "The app closed while this workspace was being created.";

/** The interrupted attempt's notice; with a held Retry, why it can't be retried in place of the offer to. */
export function interruptedLine(retryUnavailableReason: string): string {
  return retryUnavailableReason === ""
    ? `${INTERRUPTED_LEAD} You can retry with the same settings or discard the partial workspace.`
    : `${INTERRUPTED_LEAD} ${retryUnavailableReason} You can discard the partial workspace.`;
}

/** What the failure notice says, and which ways forward fit it. Report a bug is always offered. */
export interface FailureCopy {
  message: string;
  /** Retry can help, possibly once the person deals with the cause outside Imbue Studio. */
  isRetryOffered: boolean;
  /** Imbue Cloud refused the create, so running the workspace on this computer is a way forward. */
  isRunHereOffered: boolean;
  /** The account can't use Imbue Cloud at all, so running here turns its Imbue Cloud backups and sharing off. */
  isImbueCloudUnusable: boolean;
  /** This version is too old for Imbue Cloud, so updating Imbue Studio is a way forward. */
  isUpdateOffered: boolean;
}

const RUN_HERE_TAIL = "or *run this workspace on this computer instead*.";

/** A full commit id, which the Branch field also takes. */
const FULL_COMMIT_SHA = /^[0-9a-f]{40}$/;

/** A plan limit as the person would count it, by the connector's entitlement name. */
function planLimitPhrase(refusal: CreateAttemptQuotaRefusal | null): string {
  if (refusal === null) return "You've reached a limit on your plan.";
  const limit = refusal.limit.toLocaleString("en-US");
  switch (refusal.entitlement) {
    case "max_remote_workspaces":
      return `You've reached your plan's limit of ${limit} running workspaces on Imbue Cloud.`;
    case "max_total_workspaces":
      return `You've reached your plan's limit of ${limit} workspaces on Imbue Cloud.`;
    case "max_active_machine_units":
      return `You've reached your plan's limit of ${limit} GB of memory across your running workspaces.`;
    case "max_total_machine_disk_gb":
      return `You've reached your plan's limit of ${limit} GB of workspace storage.`;
    default:
      return "You've reached a limit on your plan.";
  }
}

/**
 * The failure notice for a create attempt: "Couldn't create <name>." and a
 * sentence for the cause when the server recognised one (`errorKind`), else
 * the fallback. The raw error never goes in the sentence; the page keeps it
 * behind See details. A non-empty `retryUnavailableReason` holds Retry, so the
 * sentence gives that reason instead of a step that ends in Retry.
 */
export function failureCopy(
  workspaceName: string,
  failure: CreateAttemptFailure,
  request: CreateAttemptRequestSummary | null,
  retryUnavailableReason = "",
): FailureCopy {
  const copy = causeCopy(workspaceName, failure, request);
  if (!copy.isRetryOffered || retryUnavailableReason === "") return copy;
  const name = workspaceName || "the workspace";
  const lead = failure.errorKind === "OUTCOME_UNKNOWN" ? outcomeUnknownLead(name) : `Couldn't create ${name}.`;
  const runHere = copy.isRunHereOffered ? " You can run this workspace on this computer instead." : "";
  return { ...copy, message: `${lead} ${retryUnavailableReason}${runHere}` };
}

function outcomeUnknownLead(name: string): string {
  return `Something went wrong while creating ${name}, and Imbue Studio couldn't tell whether the workspace was made.`;
}

function causeCopy(
  workspaceName: string,
  failure: CreateAttemptFailure,
  request: CreateAttemptRequestSummary | null,
): FailureCopy {
  const name = workspaceName || "the workspace";
  const couldNot = `Couldn't create ${name}.`;
  const branch = request?.branch ?? "";
  const copyOf = (message: string, ways: Partial<Omit<FailureCopy, "message">> = {}): FailureCopy => ({
    message,
    isRetryOffered: true,
    isRunHereOffered: false,
    isImbueCloudUnusable: false,
    isUpdateOffered: false,
    ...ways,
  });
  // A missing branch or tag is told apart (GIT_REF_NOT_FOUND) whenever the
  // remote answers; a commit is not probed, so only for one can a refused
  // fetch still mean it isn't there.
  const isCommit = FULL_COMMIT_SHA.test(branch);
  const branchClause = isCommit ? "that commit may not exist" : "the address may be wrong";
  const checkWhat = isCommit ? "Check the address and commit" : "Check the address";
  switch (failure.errorKind) {
    case "GITHUB_AUTH_REQUIRED":
      return copyOf(
        `${couldNot} Imbue Studio couldn't get its repository. It may be private, or ${branchClause}. ` +
          `*${checkWhat}, then select Retry*.`,
      );
    case "GIT_AUTH_REQUIRED":
      return copyOf(
        `${couldNot} Imbue Studio couldn't get its repository. It may need a sign-in, or ${branchClause}. ` +
          `*${checkWhat}, then select Retry*.`,
      );
    case "GIT_SSH_AUTH_REQUIRED":
      return copyOf(
        `${couldNot} Imbue Studio couldn't get its repository. This computer's SSH key may not have access to it, ` +
          `or ${branchClause}. *${checkWhat} and that your SSH key can read the repository, then select Retry*.`,
      );
    case "GIT_REF_NOT_FOUND":
      return copyOf(
        `${couldNot} ${request?.repository ? shortRepository(request.repository) : "Its repository"} ` +
          `has no branch or tag called ${branch || "that"}. *Clear the Branch field to use its default ` +
          "branch, or enter one that exists, then select Retry*.",
      );
    case "BRANCH_CHECKOUT_FAILED":
      return copyOf(
        `${couldNot} Imbue Studio couldn't switch its code to the branch ${branch || "you named"}. *Check that the ` +
          "branch exists and that no uncommitted changes are in the way, then select Retry*.",
      );
    case "LOCAL_PATH_MISSING":
      return copyOf(
        `${couldNot} There's no folder at ${request?.repository || "that path"} on this computer. ` +
          "*Check the path, then select Retry*.",
      );
    case "PROVIDER_UNAVAILABLE":
      // The provider's own next step, worded as the recovery card words it.
      if (failure.providerRemediation) {
        return copyOf(`${couldNot} To fix it, *${failure.providerRemediation}, then select Retry*.`);
      }
      break;
    case "NAME_TAKEN":
      return copyOf(
        `${couldNot} There's already a workspace called ${name}. *Select Retry and pick a different name.*`,
      );
    case "EMAIL_NOT_VERIFIED":
      return copyOf(
        `${couldNot} Verify your email before creating a workspace on Imbue Cloud: *click the link in the ` +
          "verification email Imbue Cloud sent you (check your spam folder too), then select Retry*, " +
          RUN_HERE_TAIL,
        { isRunHereOffered: true, isImbueCloudUnusable: true },
      );
    case "ACCOUNT_SUSPENDED":
      return copyOf(
        `${couldNot} This account is suspended. *If you believe this is a mistake, contact support@imbue.com*, ` +
          RUN_HERE_TAIL,
        { isRetryOffered: false, isRunHereOffered: true, isImbueCloudUnusable: true },
      );
    case "CLIENT_TOO_OLD":
      return copyOf(
        `${couldNot} This version of Imbue Studio is too old to create workspaces on Imbue Cloud. ` +
          `*Update Imbue Studio*, ${RUN_HERE_TAIL}`,
        { isRetryOffered: false, isRunHereOffered: true, isImbueCloudUnusable: true, isUpdateOffered: true },
      );
    case "NO_CLOUD_CAPACITY":
      return copyOf(
        `${couldNot} Imbue Cloud has no room for a new workspace right now. Try again later, ${RUN_HERE_TAIL}`,
        { isRunHereOffered: true },
      );
    case "WAITLISTED":
      return copyOf(
        `${couldNot} Your account is still on the waitlist, so it can't run workspaces on Imbue Cloud yet. ` +
          `*Open the invite link in your invitation email and accept the invite, then select Retry*, ${RUN_HERE_TAIL}`,
        { isRunHereOffered: true, isImbueCloudUnusable: true },
      );
    case "QUOTA_EXCEEDED":
      return copyOf(
        `${couldNot} ${planLimitPhrase(failure.quotaRefusal)} *Remove a workspace you no longer need, then select ` +
          `Retry*, ${RUN_HERE_TAIL}`,
        { isRunHereOffered: true },
      );
    case "OUTCOME_UNKNOWN":
      return copyOf(
        `${outcomeUnknownLead(name)} *Check your workspaces list before trying again.* If this persists, report a bug.`,
      );
    case "TARGET_UNRESOLVED":
      return copyOf(
        `Couldn't work out where to run ${name}. *Select Retry and choose where the workspace should run.*`,
      );
    case "TEMPLATE_UNREADABLE":
      return copyOf(
        `Couldn't create ${name}: this template's settings can't be read. ` +
          "*Choose a different template or branch, then select Retry*.",
      );
    default:
      break;
  }
  return copyOf(fallbackErrorSentence(`Couldn't create ${name}`));
}

/** A repository the way people say it: the github.com prefix and .git suffix trimmed. */
export function shortRepository(repository: string): string {
  return repository
    .replace(/^https:\/\/github\.com\//, "")
    .replace(/\.git$/, "");
}
