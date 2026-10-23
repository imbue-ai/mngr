// The creation page's own turns: the user turn that restates the settings a
// create was submitted with, the agent's setup line, the onboarding questions
// the page asks while the workspace is built, the welcome that answers them,
// the one app others have built that the page shows on "See examples", and the
// failure and interrupted copy.
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

/** The answers the first question and the follow-up offer, by id: what the page's state and the hand-over carry. */
export type BuildAnswerId = "not-sure" | "no" | "yes";
export type PathAnswerId = "examples" | "problem";

/** One answer the page offers to an onboarding question: its button, and the words the user's bubble says. */
export interface OnboardingChoice<Id extends string> {
  id: Id;
  label: string;
  isEmphasized: boolean;
}

/** The first question, asked while the workspace is built. */
export const BUILD_ASK = "Do you know what you want to build in Studio?";
export const BUILD_CHOICES: OnboardingChoice<BuildAnswerId>[] = [
  { id: "not-sure", label: "I’m not sure", isEmphasized: false },
  { id: "no", label: "No", isEmphasized: false },
  { id: "yes", label: "Yes", isEmphasized: true },
];

/** The follow-up for a reader who does not know yet. */
export const PATH_ASK =
  "Do you want to see examples of how other people have used Studio or simply ramble about some problems you have?";
export const PATH_CHOICES: OnboardingChoice<PathAnswerId>[] = [
  { id: "examples", label: "See examples", isEmphasized: false },
  { id: "problem", label: "Describe a problem", isEmphasized: true },
];

/**
 * The label of the choice called ``id`` among ``choices``: what the user's bubble says for that answer. An id the
 * list lacks is an error: the list is data, which the id type cannot keep in step.
 */
export function choiceLabel<Id extends string>(choices: OnboardingChoice<Id>[], id: Id): string {
  const choice = choices.find((candidate) => candidate.id === id);
  if (choice === undefined) throw new Error(`No onboarding choice is called ${id}`);
  return choice.label;
}

/** What the agent says when the questions are answered before the workspace is ready, over the loading box. */
export const FINISHING_LINE = "Great, let me just finish setting up your workspace...";

/**
 * Where the questions led: the reader knows what to build, wants to describe a problem, or wants to see
 * examples first. The welcome the workspace opens on is written for each.
 */
export type OnboardingIntent = "build" | "problem" | "examples";

/** The intent the questions settle on (the follow-up's answer names its own), or null while one is still open. */
export function onboardingIntent(
  buildAnswer: BuildAnswerId | null,
  pathAnswer: PathAnswerId | null,
): OnboardingIntent | null {
  if (buildAnswer === "yes") return "build";
  return pathAnswer;
}

/** The welcome a workspace opens on when the questions were never asked or never answered. */
export const DEFAULT_ONBOARDING_INTENT: OnboardingIntent = "problem";

/** One app others have built, shown as a recording with a title and a line or two under it. */
export interface ShowcaseApp {
  id: string;
  title: string;
  description: string;
  /** The recording's hosted address (models/onboardingMedia.ts). */
  gifUrl: string;
}

/** The one app the creation page shows a reader who asked to see examples. */
export const SHOWCASE_APP: ShowcaseApp = {
  id: "todo-list",
  title: "To-do list",
  description: "One list pulled from your email, Slack, and texts, with the next step already drafted for you.",
  gifUrl: onboardingMediaUrl(SHOWCASE_RECORDING_NAMES.todoList),
};

/** How long the recording is left to look at before the agent's next turn arrives under it. */
export const SHOWCASE_READ_MS = 2500;

/** One showcase app as the seeded chat carries it: its title, the recording, and its description. */
export function showcaseMarkdown(app: ShowcaseApp): string {
  return [
    `**${app.title}**`,
    `![${app.title}](${app.gifUrl})`,
    app.description,
  ].join("\n\n");
}

export const READY_LINE = "You're all set.";

/**
 * What the agent says under the ready line for a reader who wants to describe a problem (and, after the
 * examples, for one who asked to see them): an invitation to complain, with one example to answer in kind.
 */
export const WELCOME_BODY = [
  "The best way to start is to build something.",
  "**What’s annoying, overwhelming, or tedious in your digital life?** " +
    "Work or personal, big or small, go ahead and complain. I’ll help you build something that makes it better.",
  "*Example: I feel scattered. My todos are in a million different places, and I can never find things when I " +
    "need them. I want a command center that pulls everything into one place and tells me what to do next.*",
].join("\n\n");

/** What the agent says under the ready line for a reader who already knows what to build. */
export const WELCOME_BODY_BUILD = [
  "Just tell me what you want to build.",
  "**Focus on describing the problem — just ramble and provide as much context as you can.**",
  "*Example: I want a command center that pulls everything into one place and tells me what to do next " +
    "because my todos are in a million different places, and I can never find things when I need them.*",
].join("\n\n");

/** The closing turn as markdown: the ready line as a heading over the welcome written for ``intent``. */
export function readyTurnMarkdown(intent: OnboardingIntent): string {
  switch (intent) {
    case "build":
      return `## ${READY_LINE}\n\n${WELCOME_BODY_BUILD}`;
    // A reader who asked for examples saw the recording on the creation page; the welcome is the problem one.
    case "problem":
    case "examples":
      return `## ${READY_LINE}\n\n${WELCOME_BODY}`;
  }
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
