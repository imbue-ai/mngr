// Shared workspaces (/workspaces/shared): every share each signed-in account
// holds, each joined to its workspace record, so a share whose workspace is
// gone or unknown (a leaked max_shared_workspaces slot) can be removed from
// here. Reached from the account card's usage row and from the quota refusals
// the create flow and the share panel show.

import m from "mithril";
import { getAppContext } from "../../app-context";
import {
  browserSharedWorkspacesDeps,
  SharedWorkspacesModel,
  type AccountSharesSection,
  type SharedWorkspaceRow,
} from "../../models/sharedWorkspaces";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";

interface SharedWorkspacesState {
  model: SharedWorkspacesModel;
}

function recordStateCopy(row: SharedWorkspaceRow): string {
  switch (row.record_state) {
    case "active":
      return "Workspace exists";
    case "destroyed":
      return "Workspace destroyed";
    case "missing":
      return "Workspace unknown";
  }
}

function rowMetaLine(row: SharedWorkspaceRow): string {
  const parts = [row.account_email, row.device_label, recordStateCopy(row), row.last_seen_display];
  if (row.created_display) parts.push(`shared since ${row.created_display}`);
  if (row.share_state === "suspended") parts.push("suspended");
  return parts.join(" · ");
}

function SharedRow(): m.Component<{
  row: SharedWorkspaceRow;
  section: AccountSharesSection;
  model: SharedWorkspacesModel;
}> {
  return {
    view(vnode) {
      const { row, section, model } = vnode.attrs;
      const isArmed = model.armedHostId === row.host_id;
      const isRemoving = model.removingHostIds.has(row.host_id);
      const actionLabel = row.is_hosted_here ? "Stop sharing" : "Remove";
      return m(Card, { layout: "row-spread" }, [
        m("div", { class: "min-w-0" }, [
          m("div", { class: "flex items-center gap-2 flex-wrap" }, [
            m("div", { class: "font-semibold break-words" }, row.display_name),
            row.is_likely_leaked
              ? m(
                  "span",
                  {
                    class:
                      "inline-flex items-center px-2 py-0.5 rounded-full type-helper bg-fill-subtle text-important border border-default",
                    "data-leaked": row.host_id,
                  },
                  "Likely leaked",
                )
              : null,
          ]),
          m("div", { class: "type-helper text-tertiary break-all" }, row.workspace_domain),
          m("div", { class: "type-helper text-tertiary" }, rowMetaLine(row)),
        ]),
        m("div", { class: "flex items-center justify-end gap-2 shrink-0" }, [
          !isArmed
            ? m(
                Button,
                {
                  variant: row.is_likely_leaked ? "danger" : "secondary",
                  disabled: isRemoving,
                  onclick: () => {
                    model.armedHostId = row.host_id;
                  },
                },
                isRemoving ? "Removing..." : actionLabel,
              )
            : m("span", { class: "flex flex-col items-end gap-1" }, [
                m(
                  "span",
                  { class: "type-helper text-secondary text-right max-w-xs" },
                  row.is_hosted_here
                    ? "Stop sharing this workspace? Its links stop working; who may open it is kept for the next time."
                    : "Remove this share? Its links stop working and the slot is freed.",
                ),
                m("span", { class: "flex items-center gap-2" }, [
                  m(
                    Button,
                    { variant: "danger", onclick: () => void model.remove(section, row.host_id) },
                    actionLabel,
                  ),
                  m(
                    Button,
                    {
                      variant: "secondary",
                      onclick: () => {
                        model.armedHostId = null;
                      },
                    },
                    "Cancel",
                  ),
                ]),
              ]),
        ]),
      ]);
    },
  };
}

function renderSection(section: AccountSharesSection, model: SharedWorkspacesModel, isMultiAccount: boolean): m.Children {
  const heading = isMultiAccount ? m("h2", { class: "type-heading text-primary" }, section.email) : null;
  if (section.isUnavailable)
    return m("div", { class: "flex flex-col gap-2" }, [
      heading,
      m(Notice, { variant: "warn" }, `Could not reach Imbue Cloud for ${section.email}'s shares.`),
    ]);
  if (section.rows === null)
    return m("div", { class: "flex flex-col gap-2" }, [heading, m("div", { class: "type-body text-secondary" }, "Loading...")]);
  if (section.rows.length === 0)
    return m("div", { class: "flex flex-col gap-2" }, [
      heading,
      m("div", { class: "type-body text-secondary" }, "No shared workspaces."),
    ]);
  // Keyed rows live in their own fragment: mithril refuses a fragment that
  // mixes keyed vnodes with an unkeyed (or absent) heading.
  return m("div", { class: "flex flex-col gap-2" }, [
    heading,
    m(
      "div",
      { class: "flex flex-col gap-2" },
      section.rows.map((row) => m(SharedRow, { key: row.host_id, row, section, model })),
    ),
  ]);
}

export const SharedWorkspacesPage: m.Component<Record<string, never>, SharedWorkspacesState> = {
  oninit(vnode) {
    vnode.state.model = new SharedWorkspacesModel(browserSharedWorkspacesDeps(() => m.redraw()));
    vnode.state.model.syncAccounts(getAppContext().stores.accounts.accounts);
  },
  onupdate(vnode) {
    vnode.state.model.syncAccounts(getAppContext().stores.accounts.accounts);
  },
  view(vnode) {
    const { model } = vnode.state;
    const isMultiAccount = model.sections.length > 1;
    return m(
      PageContainer,
      m("div", { class: "flex flex-col gap-4 pt-10 pb-10" }, [
        m("h1", { class: "type-heading-lg" }, "Shared workspaces"),
        m(
          "p",
          { class: "type-body text-secondary" },
          "Every workspace your accounts share. A share counts toward your shared-workspaces limit until it is " +
            "removed, including one whose workspace was destroyed on another computer or is no longer known.",
        ),
        model.isLoaded && model.leakedCount > 0
          ? m(
              Notice,
              { variant: "warn" },
              `${model.leakedCount} share${model.leakedCount === 1 ? "" : "s"} look${model.leakedCount === 1 ? "s" : ""} leaked: ` +
                "the workspace is destroyed or unknown. Removing it frees the slot.",
            )
          : null,
        model.errorMessage !== null ? m(Notice, { variant: "error" }, model.errorMessage) : null,
        model.sections.length === 0
          ? m("div", { class: "type-body text-secondary" }, "Sign in to an account to see its shared workspaces.")
          : m(
              "div",
              { class: "flex flex-col gap-6" },
              model.sections.map((section) =>
                m("div", { key: section.accountId }, renderSection(section, model, isMultiAccount)),
              ),
            ),
      ]),
    );
  },
};
