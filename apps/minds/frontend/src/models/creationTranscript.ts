// The creation page's own turns: the user turn that restates the settings a
// create was submitted with, the agent's setup line, and the failure and
// interrupted copy. Pure functions over the attempt detail so the page renders
// the same words whether it was reached from the start flow, the create form,
// or a reload.

import type { CreateAttemptRequestSummary } from "./create";
import { backupProviderLabel, launchModeLabel } from "./create";
import type { DisclosurePoint } from "./startFlow";

export const SETUP_LINE =
  "Setting up your workspace. This takes a minute or two. While you wait, here is what is going on, " +
  "and what you will be able to do once it is up.";

/** Where every "read more" link points until the docs it belongs to exist. */
export const PRODUCT_HOME_URL = "https://imbue.com/product/mind";

/** A section of the reading material on the creation page: the line, what opens under it, and its link. */
export interface SetupSection extends DisclosurePoint {
  linkLabel: string;
  href: string;
}

export const SETUP_SECTIONS: SetupSection[] = [
  {
    id: "what",
    label: "What a workspace is",
    detail:
      "A workspace is a private computer where you and your Mind work together. It holds your files, apps, " +
      "tools, and the memory you build together. It keeps your work in one place from one conversation to the " +
      "next, so you don’t have to start over.\n\n" +
      "When you hand off a task or set a routine, your Mind can keep working while you’re away.",
    linkLabel: "How workspaces work",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "now",
    label: "What’s happening right now",
    detail:
      "We’re setting up your workspace on its own computer. We’re installing the tools your Mind needs to make " +
      "apps, work with your files and accounts, and keep tasks running while you’re away.\n\n" +
      "The last step connects your workspace to this app so you can start using it.\n\n" +
      "Want more detail? The setup log shows each part as it happens.",
    linkLabel: "How setup works",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "do",
    label: "What you can do with it",
    detail:
      "Start with a problem you want to solve or an app you want to make. Your Mind can build tools around the " +
      "way you work, use the files and accounts you connect, handle a task, or run a routine on a schedule.\n\n" +
      "You can keep what you make private, invite people to work with you in the same workspace, or share a " +
      "clean copy they can make their own. Working in the same workspace is like sharing a Google Doc: everyone " +
      "works in the same place. Sharing a copy gives someone the app without giving them your data.\n\n" +
      "When Mind needs an account, it’ll ask you to connect it. You can see and remove that access later.",
    linkLabel: "See what you can make with Mind",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "data",
    label: "How your data is handled",
    detail:
      "Your workspace keeps its own files, apps, memory, and settings. Imbue never sells that data or uses it to " +
      "train AI models for other people.\n\n" +
      "When Mind uses an outside AI model or connected service, that company’s data rules apply too.\n\n" +
      "We’re working toward full end-to-end encryption. Once that’s ready, no one but you—not even Imbue—will " +
      "be able to read what’s in your workspace.\n\n" +
      "Your workspace is backed up as you use it, much like version history in a document. It’s built to move " +
      "with you, too. You can download it to your computer or move it to another service without starting " +
      "over. The work you’ve built stays yours.",
    linkLabel: "Read our data promises",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "later",
    label: "Changing it later",
    detail:
      "You can rename your workspace, change its color, connect or disconnect accounts, and change the computer " +
      "it runs on later in Settings.",
    linkLabel: "Workspace settings",
    href: PRODUCT_HOME_URL,
  },
];
export const READY_LINE = "Your workspace is ready! What would you like to do first?";

/** One way to start: its title, and the line or two under it. */
export interface StartOption {
  title: string;
  detail: string;
}

/** The ways to start the closing turn offers, in the order they are numbered. */
export const START_OPTIONS: StartOption[] = [
  {
    title: "Start with an app",
    detail: "Pick a useful app someone else made, then change it to fit you.",
  },
  {
    title: "Make something new",
    detail:
      "Tell your Mind about a problem you want to solve or an app you want to make. It’ll help you shape it, " +
      "build it, and improve it as you use it.",
  },
  {
    title: "Connect your data",
    detail:
      "Connect your email, calendar, Slack, GitHub, or another service so Mind can help with the information " +
      "already there.",
  },
  {
    title: "Hand off some work",
    detail: "Give your Mind a task to do now, or set something to run on a schedule.",
  },
  {
    title: "Learn how Mind works",
    detail: "Ask your Mind to explain what it can do, how your workspace works, and what you control.",
  },
];

/** The closing turn as markdown: the ready line as a heading one level above the numbered option headings under it. */
export function readyTurnMarkdown(): string {
  return [`## ${READY_LINE}`, ...startOptionsMarkdown()].join("\n\n");
}

/** The closing turn's options as markdown: a numbered heading per option with its detail under it. */
export function startOptionsMarkdown(): string[] {
  return START_OPTIONS.map((option, index) => `### ${index + 1}. ${option.title}\n\n${option.detail}`);
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
  return repository.replace(/^https:\/\/github\.com\//, "").replace(/\.git$/, "");
}

/**
 * The settings, one per line, in the order the create form asks for them.
 * Region and machine size are omitted when the mode has none, so a local
 * create does not read as missing something. The start flow's Imbue Cloud
 * answer restates only itself -- see IMBUE_CLOUD_SUMMARY_LINE. Choosing Imbue
 * Cloud inside the form still lists everything: there the settings are yours.
 *
 * ``isCloudPreset`` is provenance, which the request itself does not carry --
 * the two submissions are identical on the wire -- so the caller passes what
 * the start flow remembers.
 */
export function summaryLines(request: CreateAttemptRequestSummary, isCloudPreset: boolean): string[] {
  if (isCloudPreset) return [IMBUE_CLOUD_SUMMARY_LINE];
  const compute = request.cloud_account !== "" ? request.cloud_account : launchModeLabel(request.launch_mode);
  const settings: Array<[string, string]> = [
    ["Name", request.display_name],
    ["Compute", compute],
    ["Backup", backupProviderLabel(request.backup_provider)],
    ["Region", request.region],
    ["Machine size", request.instance_type],
    ["Template repository", shortRepository(request.repository)],
    ["Branch", request.branch !== "" ? request.branch : "latest"],
  ];
  return [
    "Create a workspace with these settings:",
    ...settings.filter(([, value]) => value !== "").map(([label, value]) => `${label} — ${value}`),
  ];
}
