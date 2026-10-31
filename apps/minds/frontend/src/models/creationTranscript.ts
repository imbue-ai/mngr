// The creation page's own turns: the user turn that restates the settings a
// create was submitted with, the agent's setup line, the recording an
// onboarding create shows under it (with its intro line), the welcome, and
// the failure and interrupted copy.
// Pure functions over the attempt detail so the page renders the same words
// whether it was reached from the start flow, the create form, or a reload.

import type { CreateAttemptRequestSummary } from "./create";
import { backupProviderLabel, launchModeLabel } from "./create";
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
export const INTERRUPTED_LINE =
  "The app closed while this workspace was being created. " +
  "You can retry with the same settings or discard the partial workspace.";

/** The failure turn's opening line. */
export function failureLine(workspaceName: string, error: string): string {
  return `Could not create ${workspaceName || "the workspace"}: ${error || "unknown error"}`;
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
