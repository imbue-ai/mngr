// The grant dialog for one request, by kind.
//
// Rendered inside the "Waiting on you" row that names the request, in the
// machine's Permissions pane -- the one place a request is reviewed. Each kind
// brings its own body and wraps it in PermissionsShell, which carries the
// parts every kind shares (Reason, the credential form, Deny / Approve).

import m from "mithril";
import type { InboxDetail } from "../../../models/inbox";
import { InboxModel } from "../../../models/inbox";
import { fallbackErrorSentence } from "../../../models/errors";
import { ErrorNotice } from "../../components/ErrorDisplay";
import { AccountsPermissionDetailView } from "./AccountsPermissionDetail";
import { CustomServicePermissionDetailView } from "./CustomServicePermissionDetail";
import { FileSharingPermissionDetailView } from "./FileSharingPermissionDetail";
import { PredefinedPermissionDetailView } from "./PredefinedPermissionDetail";
import { RemoteExecutionPermissionDetailView } from "./RemoteExecutionPermissionDetail";
import { WorkspacePermissionDetailView } from "./WorkspacePermissionDetail";

/** What failed for a request the page can't show. Opening it again fails the
 * same way, so its sentence asks for no retry. */
const UNSHOWABLE_WHAT_FAILED = "Imbue Studio couldn't show this request";

/** The grant dialog for one request, by kind. */
export function requestDetailView(model: InboxModel, detail: InboxDetail): m.Children {
  switch (detail.kind) {
    case "predefined":
      return m(PredefinedPermissionDetailView, { model, detail });
    case "file_sharing":
      return m(FileSharingPermissionDetailView, { model, detail });
    case "workspace":
      return m(WorkspacePermissionDetailView, { model, detail });
    case "accounts":
      return m(AccountsPermissionDetailView, { model, detail });
    case "custom_service":
      return m(CustomServicePermissionDetailView, { model, detail });
    case "remote_execution":
      return m(RemoteExecutionPermissionDetailView, { model, detail });
    case "unknown_scope":
      // The gateway refuses a scope its catalog lacks when the agent files the
      // request, so this is a bug: the generic error, and Deny to unblock the agent.
      return m(ErrorNotice, {
        id: "permissions-unknown-scope",
        error: {
          message: fallbackErrorSentence(UNSHOWABLE_WHAT_FAILED, { isRetryable: false }),
          details: `Scope '${detail.scope}' is not in the catalog.`,
        },
        actions: [{ label: "Deny", id: "permissions-deny-btn", isDestructive: true, onclick: () => model.deny() }],
      });
    case "unsupported":
      // No handler owns this request, so a Deny would reach the same dead end; Close is the way out.
      return m(ErrorNotice, {
        id: "permissions-unsupported",
        error: { message: fallbackErrorSentence(UNSHOWABLE_WHAT_FAILED, { isRetryable: false }), details: detail.message },
        actions: [{ label: "Close", onclick: () => model.close() }],
      });
    case "unavailable":
      return m("div", { class: "flex flex-col items-center justify-center gap-2 py-8 text-center" }, [
        m("p", { class: "type-heading text-primary" }, "This permission request is no longer available"),
        detail.message ? m("p", { class: "type-body text-tertiary" }, detail.message) : null,
      ]);
    default: {
      const unreachable: never = detail;
      void unreachable;
      return null;
    }
  }
}
