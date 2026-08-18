// The grant dialog for one request, by kind.
//
// Rendered inside the "Waiting on you" row that names the request, in the
// machine's Permissions pane -- the one place a request is reviewed. Each kind
// brings its own body and wraps it in PermissionsShell, which carries the
// parts every kind shares (Reason, the credential form, Deny / Approve).

import m from "mithril";
import type { InboxDetail } from "../../../models/inbox";
import { InboxModel } from "../../../models/inbox";
import { Notice } from "../../components/Notice";
import { AccountsPermissionDetailView } from "./AccountsPermissionDetail";
import { FileSharingPermissionDetailView } from "./FileSharingPermissionDetail";
import { PredefinedPermissionDetailView } from "./PredefinedPermissionDetail";
import { WorkspacePermissionDetailView } from "./WorkspacePermissionDetail";

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
    case "unknown_scope":
      return m("div", { class: "flex flex-col gap-3" }, [
        m(
          Notice,
          { variant: "warn" },
          `The requested scope '${detail.scope}' is not in the catalog, so there are no permissions to offer.`,
        ),
        m(
          "button",
          {
            class: "self-start type-body text-secondary underline cursor-pointer",
            onclick: () => model.deny(),
          },
          "Deny this request",
        ),
      ]);
    case "unsupported":
      return m(Notice, { variant: "error" }, detail.message);
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
