// The one warning for letting agents run commands on this computer, drawn by
// the Settings switch's modal and by the permission dialog alike, so the two
// places a user can allow it never describe it differently.

import m from "mithril";
import { Notice } from "./Notice";

/** What an approved workspace's agents will be able to do, in the user's words. */
export const REMOTE_EXECUTION_CONSEQUENCES: readonly string[] = [
  "Read, change, or delete any file you can.",
  "Read your saved passwords, keys, and browser sessions.",
  "Install or remove software.",
  "Act as you on any site or service you are signed in to.",
  "Do all of this without asking first, once a workspace has been approved.",
];

/** The red banner that heads both surfaces. */
export function remoteExecutionWarningBanner(
  text: string,
  id: string,
): m.Children {
  return m(Notice, { variant: "error", role: "alert", id }, text);
}

export function remoteExecutionConsequenceList(id: string): m.Children {
  return m(
    "ul",
    { id, class: "list-disc pl-5 flex flex-col gap-1 type-body text-primary" },
    REMOTE_EXECUTION_CONSEQUENCES.map((consequence) => m("li", consequence)),
  );
}
