// File-sharing permission dialog: yes/no on one path, with an editable path
// input, instant within-roots feedback, native Browse buttons when the
// Electron picker exists, and the same sync setting the Local files card
// draws -- switched on when the agent asked for a synchronized copy.

import m from "mithril";
import { INPUT_BASE } from "../../components/constants";
import { electronBridge } from "../../../electron-bridge";
import type { FileSharingPermissionDetail as Detail, InboxModel } from "../../../models/inbox";
import { collapseSharePathHome } from "../../../models/inbox";
import { Button } from "../../components/Button";
import { renderFolderSyncSetting } from "../../components/FolderSyncSetting";
import { Icon16 } from "../../components/Icon";
import { PermissionsShell } from "./PermissionsShell";

/** The sync option, offered alongside the grant rather than after it: the
 * moment the user decides to hand a folder over is the moment they know
 * whether the machine should also get its own copy of it. The same setting
 * the Local files card draws, so what is approved here is what that card
 * will show. */
function renderSyncOption(model: InboxModel, detail: Detail): m.Children {
  return m("div", { class: "mt-1 pt-3 border-t border-subtle", id: "file-sharing-sync" }, [
    renderFolderSyncSetting({
      path: detail.file_path,
      access: detail.access === "WRITE" ? "WRITE" : "READ",
      isOn: model.isSharePathSynced,
      unavailableReason: detail.sync_unavailable_reason,
      conflict: model.sharePathSyncConflict,
      onToggle: (enabled) => {
        model.isSharePathSynced = enabled;
      },
      onConflictChange: (conflict) => {
        model.sharePathSyncConflict = conflict;
      },
      note: detail.is_sync_requested
        ? m("p", { class: "type-helper text-info m-0", id: "file-sharing-sync-requested" }, "The agent asked for this.")
        : null,
    }),
  ]);
}

export interface FileSharingPermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

async function browseForPath(model: InboxModel, mode: "file" | "directory"): Promise<void> {
  const selected = await electronBridge
    .showFilePicker({ defaultPath: model.filePathValue.trim(), mode })
    .catch(() => null);
  if (typeof selected === "string" && selected.length > 0) {
    model.filePathValue = selected;
    m.redraw();
  }
}

export function FileSharingPermissionDetailView(): m.Component<FileSharingPermissionDetailAttrs> {
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      const hasPicker = electronBridge.isDesktop;
      return m(PermissionsShell, {
        model,
        // The requested path IS the request -- "Local files" said nothing. It
        // stays the original ask even while the input below is edited.
        headerLabel: collapseSharePathHome(detail.file_path, detail.home_dir),
        mark: m(Icon16, { name: "folder", extra: "text-primary" }),
        rationale: detail.rationale,
        progressLabel: "Granting permission...",
        body: m("div", { class: "flex flex-col gap-2" }, [
          m("p", { class: "type-body text-primary" }, [
            "The agent asks for ",
            m("b", detail.access_human_label),
            " access to:",
          ]),
          m("div", { class: "flex items-center gap-2" }, [
            m("input", {
              type: "text",
              id: "file-sharing-path-input",
              value: model.filePathValue,
              class: "flex-1 w-full leading-tight font-mono " + INPUT_BASE + " rounded-md",
              oninput: (event: Event) => {
                model.filePathValue = (event.target as HTMLInputElement).value;
              },
            }),
            hasPicker
              ? m(Button, { variant: "secondary", onclick: () => void browseForPath(model, "file") }, "Choose file…")
              : null,
            hasPicker
              ? m(
                  Button,
                  { variant: "secondary", onclick: () => void browseForPath(model, "directory") },
                  "Choose folder…",
                )
              : null,
          ]),
          model.isSharePathHintShown()
            ? m(
                "p",
                { id: "file-sharing-path-hint", class: "type-helper text-important" },
                "This path is outside the folders that can be shared.",
              )
            : null,
          m(
            "p",
            { class: "type-helper text-tertiary" },
            detail.access === "WRITE"
              ? "The agent will be able to read and modify this path."
              : "The agent will only be able to read this path.",
          ),
          detail.is_sync_supported ? renderSyncOption(model, detail) : null,
        ]),
      });
    },
  };
}
