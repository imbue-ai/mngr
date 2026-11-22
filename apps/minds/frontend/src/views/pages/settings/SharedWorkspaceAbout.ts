// Settings' Workspace & apps tab for a workspace or app shared with the
// account (specs/studio-top-bar-and-settings, 4.12). A guest has none of the
// owner's settings, so the tab is an About page instead: whose it is, what of
// it the account can reach, and the way to take it off this account's lists.
//
// Taking it off the lists is the existing remove: it declines a listed share
// (or forgets a pasted link). It hides the share here and changes nothing
// about the access the owner gave, which the copy says in as many words.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import type { SharedEntry } from "../../../models/sharedWithMe";
import {
  isWorkspaceShare,
  ownerLabel,
  sharedActionErrorMessage,
  sharedAppsOf,
  sharedWithMeApi,
} from "../../../models/sharedWithMe";
import { Button } from "../../components/Button";
import { SectionHeader } from "../../components/Layout";
import { Modal } from "../../components/Modal";
import { Spinner } from "../../components/Spinner";

export interface SharedWorkspaceAboutAttrs {
  entry: SharedEntry;
}

/** What the page's one action is called, what it removes, and what it says it does. */
export interface SharedRemovalCopy {
  /** The section's heading and its button. */
  action: string;
  /** The confirmation's title. */
  question: string;
  sentence: string;
  /** The confirming button, and what it reads while the removal runs. */
  confirm: string;
  busy: string;
}

const UNDO_SENTENCE = "You can undo it from Home › Invitations.";

export function sharedRemovalCopy(entry: SharedEntry): SharedRemovalCopy {
  const share = entry.share;
  if (share.is_manual) {
    return {
      action: "Remove link",
      question: "Remove this link?",
      sentence: "Remove this link from your list.",
      confirm: "Remove",
      busy: "Removing…",
    };
  }
  const owner = ownerLabel(share);
  const unchanged = `it does not change the access ${owner} gave you. ${UNDO_SENTENCE}`;
  if (isWorkspaceShare(share)) {
    const name = entry.name || entry.id;
    return {
      action: "Leave workspace",
      question: `Leave ${name}?`,
      sentence: `Remove ${name} from your workspaces. This hides it here; ${unchanged}`,
      confirm: "Leave",
      busy: "Leaving…",
    };
  }
  const appNames = sharedAppsOf(entry).map((app) => app.name);
  if (appNames.length <= 1) {
    const name = appNames[0] ?? (entry.name || entry.id);
    return {
      action: "Remove app",
      question: `Remove ${name}?`,
      sentence: `Remove ${name} from your apps. This hides it here; ${unchanged}`,
      confirm: "Remove",
      busy: "Removing…",
    };
  }
  return {
    action: "Remove apps",
    question: `Remove everything ${owner} shared from this workspace?`,
    sentence:
      `Remove everything ${owner} shared with you from this workspace (${appNames.join(", ")}). ` +
      `This hides them here; ${unchanged}`,
    confirm: "Remove",
    busy: "Removing…",
  };
}

/** What the account can reach, in words: the whole workspace, or the apps named. */
export function sharedAccessLabel(entry: SharedEntry): string {
  if (isWorkspaceShare(entry.share)) return "The whole workspace";
  const appNames = sharedAppsOf(entry).map((app) => app.name);
  return appNames.length === 0 ? "Nothing yet" : `Only ${appNames.join(", ")}`;
}

export function SharedWorkspaceAbout(): m.Component<SharedWorkspaceAboutAttrs> {
  let isConfirming = false;
  let isRemoving = false;
  let errorMessage = "";

  async function remove(entry: SharedEntry): Promise<void> {
    const { shell } = getAppContext();
    isRemoving = true;
    errorMessage = "";
    m.redraw();
    try {
      await sharedWithMeApi.remove(shell.accountId, entry.share.share_key);
    } catch (error) {
      isRemoving = false;
      errorMessage = sharedActionErrorMessage(error);
      m.redraw();
      return;
    }
    isRemoving = false;
    isConfirming = false;
    // It is off this account's lists: back to its Home, which closes Settings on the way.
    shell.routeToHub();
  }

  return {
    view(vnode) {
      const { entry } = vnode.attrs;
      const share = entry.share;
      const copy = sharedRemovalCopy(entry);
      const owner = share.is_manual ? null : ownerLabel(share);
      const ownerEmail = share.owner.email || "";
      return m("div#shared-workspace-about", { class: "max-w-[560px]" }, [
        m(SectionHeader, "Owner"),
        m("div", { class: "mb-8" }, [
          m("p", { class: "type-body text-primary font-semibold" }, owner ?? "Unknown"),
          owner === null
            ? m("p", { class: "type-helper text-tertiary" }, "This is a link you pasted, which names nobody.")
            : ownerEmail !== "" && ownerEmail !== owner
              ? m("p", { class: "type-helper text-tertiary" }, ownerEmail)
              : null,
        ]),
        m(SectionHeader, "Your access"),
        m("div", { class: "mb-8" }, [
          m("p#shared-workspace-access", { class: "type-body text-primary" }, sharedAccessLabel(entry)),
          m(
            "p",
            { class: "type-helper text-tertiary mt-1" },
            `${owner ?? "The owner"} decides what you can reach here, and can change it.`,
          ),
        ]),
        m(SectionHeader, copy.action),
        m("p#shared-workspace-removal", { class: "type-body text-secondary mb-3" }, copy.sentence),
        m(Button, { variant: "danger", id: "leave-workspace-btn", onclick: () => (isConfirming = true) }, copy.action),
        m(
          Modal,
          {
            isOpen: isConfirming,
            id: "leave-workspace-dialog",
            onClose: () => {
              if (!isRemoving) isConfirming = false;
            },
          },
          [
            m("h2", { class: "type-heading text-primary mb-2" }, copy.question),
            m("p", { class: "type-body text-secondary mb-4" }, copy.sentence),
            errorMessage !== ""
              ? m("p", { class: "type-body text-important mb-4", role: "alert" }, errorMessage)
              : null,
            m("div", { class: "flex justify-end gap-2" }, [
              m(Button, { variant: "secondary", disabled: isRemoving, onclick: () => (isConfirming = false) }, "Cancel"),
              m(
                Button,
                {
                  variant: "danger",
                  id: "leave-workspace-confirm",
                  disabled: isRemoving,
                  onclick: () => void remove(entry),
                },
                isRemoving ? [m(Spinner, { size: "sm" }), copy.busy] : copy.confirm,
              ),
            ]),
          ],
        ),
      ]);
    },
  };
}
