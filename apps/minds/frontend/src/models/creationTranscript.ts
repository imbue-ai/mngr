// The creation page's own turns: the user turn that restates the settings a
// create was submitted with, the agent's setup line, the recording an
// onboarding create shows under it (with its intro line), the welcome, and
// the failure and interrupted copy.
// Pure functions over the attempt detail so the page renders the same words
// whether it was reached from the start flow, the create form, or a reload.

import type { CreateAttemptFailure, CreateAttemptQuotaRefusal, CreateAttemptRequestSummary } from "./create";
import { backupProviderLabel, launchModeLabel } from "./create";
import { fallbackErrorSentence } from "./errors";
import { connectionLaunchLabel } from "./providers";
import { formatMachineSizeNumbers } from "./machineSize";
import {
  SHOWCASE_RECORDING_NAMES,
  onboardingMediaUrl,
} from "./onboardingMedia";

export const SETUP_LINE =
  "We're setting up your workspace now. Once it's ready, just describe what you want to build. Everything you " +
  "make works right away from any device, and you can share it with your team, family, or friends.";

/** The recording the creation page shows under the setup line and its intro, drawn as an image with nothing under it. */
export interface ShowcaseApp {
  id: string;
  /** The recording's alt text, and the enlarged view's title. */
  label: string;
  /** The recording's hosted address (models/onboardingMedia.ts). */
  gifUrl: string;
}

/** The one recording the creation page shows while an onboarding create is built. */
export const SHOWCASE_APP: ShowcaseApp = {
  id: "todo-list",
  label: "An app built in Studio",
  gifUrl: onboardingMediaUrl(SHOWCASE_RECORDING_NAMES.todoList),
};

/** What the agent says over the recording. */
export const SHOWCASE_INTRO_LINE = "A few examples of how people build with Studio:";

/** The recording as the seeded chat carries it, under the line that introduces it. */
export function showcaseMarkdown(app: ShowcaseApp): string {
  return `${SHOWCASE_INTRO_LINE}\n\n![${app.label}](${app.gifUrl})`;
}

export const READY_LINE = "You're all set.";

/**
 * What the agent says under the ready line: an invitation to complain, with one example to answer in kind.
 */
export const WELCOME_BODY = [
  "The best way to start is to build something.",
  "**What’s annoying, overwhelming, or tedious in your digital life?** " +
    "Work or personal, big or small, go ahead and complain. I’ll help you build something that makes it better.",
  "*Example: I feel scattered. My todos are in a million different places, and I can never find things when I " +
    "need them. I want a command center that pulls everything into one place and tells me what to do next.*",
].join("\n\n");

/** The closing turn as markdown: the ready line as a heading over the welcome. */
export function readyTurnMarkdown(): string {
  return `## ${READY_LINE}\n\n${WELCOME_BODY}`;
}

/**
 * What an Imbue Cloud create restates instead of its settings. The cloud
 * preset is not something the reader picked -- the flow chose every one of
 * those values for them -- so listing them back reads as configuration they
 * are answerable for rather than as the one choice they actually made.
 */
export const IMBUE_CLOUD_SUMMARY_LINE = "Create on Imbue Cloud";
const INTERRUPTED_LEAD = "The app closed while this workspace was being created.";

/** The interrupted attempt's notice; with a held Retry, why it can't be retried in place of the offer to. */
export function interruptedLine(retryUnavailableReason: string): string {
  return retryUnavailableReason === ""
    ? `${INTERRUPTED_LEAD} You can retry with the same settings or discard the partial workspace.`
    : `${INTERRUPTED_LEAD} ${retryUnavailableReason} You can discard the partial workspace.`;
}

/** What the failure turn says, and which ways forward fit it. Report a bug is always offered. */
export interface FailureCopy {
  message: string;
  /** Retry can help; it cannot while the account itself is refused. */
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
 * The failure turn for a create attempt: "Couldn't create <name>." and a
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
          `*Open the invite link in your invitation email*, ${RUN_HERE_TAIL}`,
        { isRetryOffered: false, isRunHereOffered: true, isImbueCloudUnusable: true },
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

/** The connection as the form offered it; the bare instance name when the connection is no longer known. */
function connectionSummaryLabel(request: CreateAttemptRequestSummary): string {
  return request.cloud_account_alias !== ""
    ? connectionLaunchLabel(
        request.cloud_account_alias,
        request.cloud_account_backend_label,
      )
    : request.cloud_account;
}

/** The line over the settings in the user's turn. */
export const SUMMARY_HEADING = "Create a workspace with these settings:";

/**
 * The settings, one per line, in the order the create form asks for them, under SUMMARY_HEADING. Region and
 * machine size are omitted when the mode has none, so a local create does not read as missing something.
 * Empty for the start flow's Imbue Cloud answer, which restates only itself -- see IMBUE_CLOUD_SUMMARY_LINE
 * (choosing Imbue Cloud inside the form still lists everything: there the settings are yours).
 *
 * ``isCloudPreset`` is provenance, which the request itself does not carry --
 * the two submissions are identical on the wire -- so the caller passes what
 * the start flow remembers.
 */
export function summarySettingLines(
  request: CreateAttemptRequestSummary,
  isCloudPreset: boolean,
): string[] {
  if (isCloudPreset) return [];
  const compute =
    request.cloud_account !== ""
      ? connectionSummaryLabel(request)
      : launchModeLabel(request.launch_mode);
  const settings: Array<[string, string]> = [
    ["Name", request.display_name],
    ["Compute", compute],
    ["Backup", backupProviderLabel(request.backup_provider)],
    ["Region", request.region],
    ["Machine size", request.instance_type !== "" ? request.instance_type : formatMachineSizeNumbers(request)],
    ["Template repository", shortRepository(request.repository)],
    ["Branch", request.branch !== "" ? request.branch : "latest"],
  ];
  return settings
    .filter(([, value]) => value !== "")
    .map(([label, value]) => `${label} — ${value}`);
}

/** What the settings turn says in full: the heading over its lines, or the Imbue Cloud answer alone. */
export function summaryLines(
  request: CreateAttemptRequestSummary,
  isCloudPreset: boolean,
): string[] {
  if (isCloudPreset) return [IMBUE_CLOUD_SUMMARY_LINE];
  return [SUMMARY_HEADING, ...summarySettingLines(request, isCloudPreset)];
}
