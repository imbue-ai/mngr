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
      "A workspace is a computer of your Mind's own: its files, its tools, its memory of what you have worked " +
      "on together, and the services you have let it reach. It keeps running between conversations, so your " +
      "Mind can keep working while you are away and pick up where you left off when you come back.",
    linkLabel: "More about workspaces",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "now",
    label: "What is happening right now",
    detail:
      "The workspace template is being copied onto the machine, its tools are being installed, and its " +
      "services are starting up. The last step connects it to this app so you can talk to your Mind here. " +
      "The log under the progress bar shows each step as it happens.",
    linkLabel: "How setup works",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "do",
    label: "What you can do with it",
    detail:
      "Ask your Mind for anything you would ask a capable colleague: research, writing, code, keeping track " +
      "of things. Give it access to your email, calendar or other services one permission at a time, and take " +
      "any of them back whenever you like. Share the workspace with other people when you want to work together.",
    linkLabel: "What a Mind can do",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "data",
    label: "How your data is handled",
    detail:
      "Everything your Mind knows lives in this workspace. It is never sold and never used to train models for " +
      "other people. Backups are yours to turn on, check and turn off, and you can move the workspace to your " +
      "own computer or another provider at any time.",
    linkLabel: "Our data promises",
    href: PRODUCT_HOME_URL,
  },
  {
    id: "later",
    label: "Changing it later",
    detail:
      "The name, the color, the compute it runs on and every permission can be changed later from the " +
      "workspace's settings. Nothing you choose now is final.",
    linkLabel: "Workspace settings",
    href: PRODUCT_HOME_URL,
  },
];
export const READY_LINE = "Your workspace is ready.";
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
