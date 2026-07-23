// The inbox's right detail pane: per-kind mithril views over the typed
// request-detail payloads (the replacement for the old server-rendered HTML
// fragments), plus the grant/deny submission flow that used to live in the
// inbox page's inline script: Approve gating, the wildcard exclusivity rule,
// file-sharing path validation with the native pickers, the approving busy
// state, and the GRANTED / DENIED / NEEDS_MANUAL_CREDENTIALS / FAILED
// outcome handling.
import m from "mithril";

import type {
  FileSharingPermissionDetail,
  InboxDetailPayload,
  PredefinedPermissionDetail,
  WorkspacePermissionDetail,
} from "../chrome_state";
import { buttonClasses } from "../ui";
import { Spinner } from "./Spinner";

// The list-side surface the detail flow drives: selection identity, the
// denying-card marker, and post-resolution advancement (implemented by the
// inbox modal's list controller).
export interface InboxDetailListGlue {
  getSelectedId(): string | null;
  markDenying(id: string): void;
  advanceAfterResolution(resolvedId: string | null): Promise<void>;
}

interface GrantResponse {
  outcome?: string;
  message?: string;
  set_credentials_example?: string;
}

// Expand a leading ``~`` / ``~/`` to the payload's home directory, mirroring
// the server-side ``_expand_home_prefix``. ``~user`` (another user's home) is
// left unchanged so the within-roots check rejects it, matching the server.
export function expandSharePathHome(value: string, homeDir: string): string {
  if (homeDir === "") return value;
  if (value === "~" || value.startsWith("~/")) return homeDir + value.slice(1);
  return value;
}

// Whether ``value`` is at or beneath one of ``roots``. Case-insensitive and
// purely lexical, mirroring the server-side check (and WsgiDAV's
// share-prefix matching).
export function isSharePathWithinRoots(value: string, roots: string[]): boolean {
  if (value === "") return false;
  const lower = value.toLowerCase();
  return roots.some((root) => {
    const normalized = String(root).replace(/\/+$/, "").toLowerCase() || "/";
    return lower === normalized || lower.startsWith(`${normalized}/`);
  });
}

// One controller per rendered detail payload (a fresh one per selection):
// checkbox/radio/path state plus the submission machinery.
export class InboxDetailController {
  readonly payload: InboxDetailPayload;
  private readonly listGlue: InboxDetailListGlue;
  private readonly scrollContainer: () => Element | null;

  // name="permissions"-equivalent selections, keyed by permission value.
  checkedPermissions = new Set<string>();
  // The workspace dialog's target radio: "selected" | "all".
  targetScope: "selected" | "all";
  // The predefined dialog's simple-vs-editor view.
  isEditorShown = false;
  // The file-sharing dialog's editable path.
  sharePath = "";
  isApproving = false;
  isDenyDisabled = false;
  isProgressShown = false;
  errorMessage: string | null = null;
  manualCredentials: { message: string; command: string } | null = null;

  constructor(payload: InboxDetailPayload, listGlue: InboxDetailListGlue, scrollContainer: () => Element | null) {
    this.payload = payload;
    this.listGlue = listGlue;
    this.scrollContainer = scrollContainer;
    this.targetScope = payload.kind === "workspace" && payload.show_target_choice ? "selected" : "all";
    if (payload.kind === "predefined") {
      payload.checked_permissions.forEach((permission) => this.checkedPermissions.add(permission));
      // No agent-requested permissions: the simple view has nothing to
      // summarize, so the warn notice + Adjust affordance lead.
    } else if (payload.kind === "workspace") {
      payload.verbs.forEach((verb) => {
        if (verb.is_checked) this.checkedPermissions.add(verb.permission);
      });
    } else if (payload.kind === "file_sharing") {
      this.sharePath = payload.file_path;
      // The request itself names the single (path, access) pair; there is no
      // per-permission choice.
      this.checkedPermissions.add("file-sharing");
    } else if (payload.kind === "accounts") {
      this.checkedPermissions.add("accounts");
    }
  }

  // The wildcard catch-all covers the specific permissions: while it is
  // ticked the specific checkboxes are disabled (keeping their own state),
  // and only the active side is granted on submit.
  isWildcardChecked(): boolean {
    return this.payload.kind === "predefined" && this.checkedPermissions.has(this.payload.wildcard_permission);
  }

  togglePermission(permission: string, isChecked: boolean): void {
    if (isChecked) this.checkedPermissions.add(permission);
    else this.checkedPermissions.delete(permission);
    m.redraw();
  }

  // The permissions the grant POST submits: the checked set minus any
  // specific permissions masked by an active wildcard (disabled inputs are
  // omitted from a native form submission; mirror that).
  submittedPermissions(): string[] {
    if (this.payload.kind !== "predefined" || !this.isWildcardChecked()) {
      return Array.from(this.checkedPermissions);
    }
    return [this.payload.wildcard_permission];
  }

  sharePathState(): { isOk: boolean; isHintShown: boolean } {
    if (this.payload.kind !== "file_sharing") return { isOk: true, isHintShown: false };
    const value = expandSharePathHome(this.sharePath.trim(), this.payload.home_dir);
    const isWithinRoots = isSharePathWithinRoots(value, this.payload.allowed_roots);
    return { isOk: value.length > 0 && isWithinRoots, isHintShown: value.length > 0 && !isWithinRoots };
  }

  isApproveEnabled(): boolean {
    if (this.isApproving) return false;
    return this.checkedPermissions.size > 0 && this.sharePathState().isOk;
  }

  async browseForSharePath(mode: "file" | "directory"): Promise<void> {
    const picker = window.minds?.showFilePicker;
    if (picker === undefined) return;
    try {
      const selected = await picker({ defaultPath: this.sharePath.trim(), mode });
      if (typeof selected === "string" && selected.length > 0) {
        this.sharePath = selected;
        m.redraw();
      }
    } catch {
      // User cancelled or the bridge errored -- keep the current path.
    }
  }

  // Bring a just-revealed notice (progress / error / manual credentials)
  // into view at the bottom of the detail pane -- on a tall request the
  // notice would otherwise be scrolled off-screen.
  private scrollNoticesIntoView(): void {
    const container = this.scrollContainer();
    if (container === null) return;
    try {
      container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
    } catch {
      container.scrollTop = container.scrollHeight;
    }
  }

  private grantUrl(): string {
    const requestId = (this.payload as { request_id?: string }).request_id ?? "";
    return `/requests/${encodeURIComponent(requestId)}/grant`;
  }

  async submitGrant(): Promise<void> {
    const resolvedId = this.listGlue.getSelectedId();
    this.isApproving = true;
    this.isDenyDisabled = true;
    this.errorMessage = null;
    this.manualCredentials = null;
    this.isProgressShown = true;
    m.redraw();
    this.scrollNoticesIntoView();

    const body = new FormData();
    this.submittedPermissions().forEach((permission) => body.append("permissions", permission));
    if (this.payload.kind === "workspace") body.append("target_scope", this.targetScope);
    if (this.payload.kind === "file_sharing") {
      body.append("file_path", this.sharePath.trim());
    }
    try {
      const response = await fetch(this.grantUrl(), { method: "POST", body, credentials: "same-origin" });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text !== "" ? text : `HTTP ${response.status}`);
      }
      const data = (await response.json()) as GrantResponse;
      if (data.outcome === "GRANTED" || data.outcome === "DENIED") {
        await this.listGlue.advanceAfterResolution(resolvedId);
        return;
      }
      this.isProgressShown = false;
      if (data.outcome === "NEEDS_MANUAL_CREDENTIALS") {
        this.manualCredentials = { message: data.message ?? "", command: data.set_credentials_example ?? "" };
        this.stopApproving();
        this.scrollNoticesIntoView();
        return;
      }
      // FAILED (or any other outcome): the approval did not complete and the
      // request stays pending server-side -- show the reason and re-enable
      // Approve so the user can retry; this is a failure, not a denial.
      this.errorMessage =
        data.message !== undefined && data.message !== ""
          ? data.message
          : data.outcome === "FAILED"
            ? "Approval failed; please try again."
            : "Authorization failed.";
      this.stopApproving();
      this.scrollNoticesIntoView();
    } catch (error) {
      this.isProgressShown = false;
      this.errorMessage = error instanceof Error && error.message !== "" ? error.message : String(error);
      this.stopApproving();
      this.scrollNoticesIntoView();
    }
  }

  private stopApproving(): void {
    this.isApproving = false;
    this.isDenyDisabled = false;
    m.redraw();
  }

  submitDeny(): void {
    const resolvedId = this.listGlue.getSelectedId();
    // Mark the card as denying immediately so any re-render before the
    // server has processed the deny shows it faded rather than letting the
    // user click it back into the dialog.
    if (resolvedId !== null) this.listGlue.markDenying(resolvedId);
    this.isApproving = true;
    this.isDenyDisabled = true;
    m.redraw();
    const denyUrl = this.grantUrl().replace(/\/grant$/, "/deny");
    // Fire-and-forget so the user doesn't wait for mngr message round trips;
    // keepalive lets the request finish even if the page transitions away.
    fetch(denyUrl, { method: "POST", credentials: "same-origin", keepalive: true }).catch(() => undefined);
    void this.listGlue.advanceAfterResolution(resolvedId);
  }
}

// -- Views -------------------------------------------------------------------

function detailHeader(displayName: m.Children, wsName: string, agentId: string, rationale: string): m.Children {
  return [
    m("h1", { class: "type-heading-lg text-primary pr-8" }, ["Permission request: ", displayName]),
    m(
      "div",
      { class: "minds-card p-4 mt-6" },
      m("div", [
        m("p", { class: "type-label text-primary mb-1" }, `${wsName !== "" ? wsName : agentId} says:`),
        m("p", { class: "type-body italic whitespace-pre-wrap" }, rationale),
      ]),
    ),
  ];
}

// The Approve/Deny footer shared by every actionable dialog. Approve starts
// disabled until a selection exists; while an approval is in flight BOTH
// buttons are disabled and the label swaps for a spinner + "Approving...".
function formFooter(controller: InboxDetailController): m.Children {
  return m("div", { class: "flex gap-2 mt-4 justify-end" }, [
    m(
      "button",
      {
        type: "button",
        id: "permissions-deny-btn",
        class: buttonClasses("danger"),
        disabled: controller.isDenyDisabled,
        onclick: () => controller.submitDeny(),
      },
      "Deny",
    ),
    m(
      "button",
      {
        type: "submit",
        id: "permissions-approve-btn",
        class: buttonClasses("success"),
        disabled: !controller.isApproveEnabled(),
      },
      [
        controller.isApproving ? m("span", { id: "permissions-approve-spinner" }, m(Spinner, { size: "sm", tone: "inverse" })) : null,
        m("span", { id: "permissions-approve-label" }, controller.isApproving ? "Approving…" : "Approve"),
      ],
    ),
  ]);
}

function permissionsForm(controller: InboxDetailController, body: m.Children): m.Children {
  return m(
    "form",
    {
      id: "permissions-form",
      class: "mt-6",
      onsubmit: (event: Event) => {
        event.preventDefault();
        void controller.submitGrant();
      },
    },
    [body, formFooter(controller)],
  );
}

// The notices below the form: the in-flight progress copy, the
// manual-credentials info box, and the failure notice.
function noticeStack(controller: InboxDetailController, progressBody: m.Children): m.Children {
  return [
    controller.isProgressShown
      ? m(
          "div",
          { id: "permissions-progress", class: "mt-4" },
          m("div", { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info" }, progressBody),
        )
      : null,
    controller.manualCredentials !== null
      ? m(
          "div",
          { id: "permissions-manual-credentials", class: "mt-4" },
          m("div", { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-info-surface)] text-info" }, [
            m("p", { class: "font-semibold" }, "Manual credential setup required"),
            m("p", { id: "permissions-manual-credentials-message", class: "mt-1" }, controller.manualCredentials.message),
            m("p", { class: "mt-2 type-body" }, [
              "Obtain credentials from the provider, replace any ",
              m("code", "<placeholders>"),
              " below with them, run the command in a terminal, then click Approve again:",
            ]),
            m(
              "pre",
              { class: "dark mt-2 bg-surface-primary text-secondary rounded-md p-3 type-helper overflow-x-auto" },
              m("code", { id: "permissions-manual-credentials-command" }, controller.manualCredentials.command),
            ),
          ]),
        )
      : null,
    controller.errorMessage !== null
      ? m(
          "div",
          { id: "permissions-error", class: "mt-4" },
          m("div", { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-important-surface)] text-important" }, [
            m("p", { class: "font-semibold" }, "Authorization failed"),
            m("p", { id: "permissions-error-message" }, controller.errorMessage),
          ]),
        )
      : null,
  ];
}

function permissionCheckboxLabel(
  controller: InboxDetailController,
  payload: PredefinedPermissionDetail,
  permission: string,
): m.Children {
  const label = permission === payload.wildcard_permission ? payload.wildcard_label : permission;
  const description = payload.description_by_permission_name[permission];
  const isWildcard = permission === payload.wildcard_permission;
  return m("label", { class: "flex items-start gap-2 cursor-pointer" }, [
    m("input", {
      type: "checkbox",
      name: "permissions",
      value: permission,
      class: "mt-1 shrink-0",
      "data-wildcard": isWildcard ? "true" : undefined,
      checked: controller.checkedPermissions.has(permission),
      disabled: !isWildcard && controller.isWildcardChecked(),
      onchange: (event: Event) => controller.togglePermission(permission, (event.target as HTMLInputElement).checked),
    }),
    m("span", { class: "min-w-0" }, [
      m("code", { class: "type-body font-mono text-primary" }, label),
      description !== undefined && description !== ""
        ? m("span", { class: "block type-body text-primary mt-0.5" }, description)
        : null,
    ]),
  ]);
}

const ADJUST_LINK_CLASSES =
  buttonClasses("ghost") + " !p-0 !bg-transparent !type-helper !text-accent hover:!bg-transparent hover:underline";

function predefinedDetail(controller: InboxDetailController, payload: PredefinedPermissionDetail): m.Children {
  if (payload.unknown_scope !== "") {
    // Deny-only variant: no catalog entry means there are no permissions to
    // offer; the only action that makes sense is Deny.
    return m("div", { class: "permissions-detail" }, [
      m("h1", { class: "type-heading-lg text-primary pr-8" }, "Unknown scope"),
      m("p", { class: "mt-2 text-secondary" }, [
        "The agent requested permissions under scope ",
        m("code", payload.unknown_scope),
        ", but this scope is not in the latchkey service catalog. The request can only be denied from here.",
      ]),
      m("div", { class: "flex gap-2 mt-6 justify-end" }, [
        m(
          "button",
          {
            type: "button",
            id: "permissions-deny-btn",
            class: buttonClasses("danger"),
            disabled: controller.isDenyDisabled,
            onclick: () => controller.submitDeny(),
          },
          "Deny",
        ),
      ]),
    ]);
  }
  const requestedPermissions = payload.permission_schemas.filter((permission) =>
    payload.checked_permissions.includes(permission),
  );
  const wsLabel = payload.ws_name !== "" ? payload.ws_name : payload.agent_id;
  return m("div", { class: "permissions-detail" }, [
    detailHeader(m("span", { class: "font-bold" }, payload.display_name), payload.ws_name, payload.agent_id, payload.rationale),
    permissionsForm(controller, [
      // Simple (default) view: a read-only summary of what Approve grants;
      // the full checkbox editor hides behind "Adjust".
      !controller.isEditorShown
        ? m("div", { id: "permissions-simple-view" }, [
            requestedPermissions.length > 0
              ? [
                  m(
                    "p",
                    { class: "type-body text-primary mb-3" },
                    `Approving will grant ${wsLabel} and its sibling agents the following permissions:`,
                  ),
                  m("div", { class: "minds-card p-4 space-y-3" }, [
                    ...requestedPermissions.map((permission) =>
                      m("div", [
                        m("div", { class: "flex items-center gap-2" }, [
                          m("span", { class: "shrink-0 text-success" }, "✓"),
                          m(
                            "code",
                            { class: "type-body font-mono text-primary" },
                            permission === payload.wildcard_permission ? payload.wildcard_label : permission,
                          ),
                        ]),
                        payload.description_by_permission_name[permission] !== undefined
                          ? m(
                              "span",
                              { class: "block type-body text-primary mt-0.5 ml-[26px]" },
                              payload.description_by_permission_name[permission],
                            )
                          : null,
                      ]),
                    ),
                    m(
                      "div",
                      { class: "flex justify-end border-t border-subtle pt-2" },
                      m(
                        "button",
                        {
                          type: "button",
                          id: "permissions-adjust-link",
                          class: ADJUST_LINK_CLASSES,
                          onclick: () => {
                            controller.isEditorShown = true;
                            m.redraw();
                          },
                        },
                        "Adjust",
                      ),
                    ),
                  ]),
                ]
              : [
                  m(
                    "div",
                    { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-warning-surface)] text-warning" },
                    [
                      "The agent did not request any specific permissions. Click ",
                      m("span", { class: "font-semibold" }, "Adjust"),
                      " below to choose what to grant.",
                    ],
                  ),
                  m(
                    "div",
                    { class: "flex justify-end mt-3" },
                    m(
                      "button",
                      {
                        type: "button",
                        id: "permissions-adjust-link",
                        class: ADJUST_LINK_CLASSES,
                        onclick: () => {
                          controller.isEditorShown = true;
                          m.redraw();
                        },
                      },
                      "Adjust",
                    ),
                  ),
                ],
          ])
        : m("div", { id: "permissions-editor-view" }, [
            m("h2", { class: "type-label text-primary mb-2" }, "Permissions to grant:"),
            m(
              "div",
              { class: "minds-card p-4 space-y-3" },
              payload.permission_schemas.map((permission) => permissionCheckboxLabel(controller, payload, permission)),
            ),
          ]),
    ]),
    noticeStack(
      controller,
      payload.will_open_browser
        ? [
            m("span", { class: "font-semibold" }, "Authenticating..."),
            ` Opening a browser window for you to sign in to ${payload.display_name}. ` +
              "The request will resolve automatically when the flow completes.",
          ]
        : m("span", { class: "font-semibold" }, "Granting permission..."),
    ),
  ]);
}

function fileSharingDetail(controller: InboxDetailController, payload: FileSharingPermissionDetail): m.Children {
  const wsLabel = payload.ws_name !== "" ? payload.ws_name : payload.agent_id;
  const pathState = controller.sharePathState();
  const hasPicker = window.minds?.showFilePicker !== undefined;
  const isWrite = payload.access === "WRITE";
  return m("div", { class: "permissions-detail" }, [
    detailHeader(
      m("code", { class: "code-pill" }, payload.file_path),
      payload.ws_name,
      payload.agent_id,
      payload.rationale,
    ),
    permissionsForm(controller, [
      m("p", { class: "type-body text-primary mb-3" }, [
        isWrite
          ? [`Approving will grant ${wsLabel} and its sibling agents `, m("strong", "read and write"), " access (including delete) to the file/directory below."]
          : [`Approving will grant ${wsLabel} and its sibling agents `, m("strong", "read-only"), " access to the file/directory below."],
        " You can edit the path before approving -- paste a different one or pick it with Browse.",
      ]),
      m("div", { class: "flex items-center gap-2 mb-1.5" }, [
        m("label", { for: "file-sharing-path-input", class: "type-label text-primary" }, "Path to share"),
        m(
          "span",
          {
            class:
              "shrink-0 type-section px-2 py-0.5 rounded-full border " +
              (isWrite
                ? "bg-warning/12 text-warning border-warning/30"
                : "bg-success/12 text-success border-success/30"),
          },
          payload.access_human_label,
        ),
      ]),
      m("input", {
        id: "file-sharing-path-input",
        name: "file_path",
        type: "text",
        class:
          "w-full leading-tight p-2 type-body border border-strong bg-surface-primary text-primary " +
          "placeholder:text-tertiary hover:border-stronger focus:border-stronger focus:outline-2 " +
          "focus:outline-offset-2 focus:outline-accent rounded-md font-mono",
        value: controller.sharePath,
        oninput: (event: Event) => {
          controller.sharePath = (event.target as HTMLInputElement).value;
          m.redraw();
        },
      }),
      pathState.isHintShown
        ? m(
            "p",
            { id: "file-sharing-path-hint", class: "mt-1.5 type-helper text-warning" },
            "This path isn't inside a folder Minds can share (your home folder or the system temporary " +
              "folder), so it can't be shared. Pick a path under one of those instead.",
          )
        : null,
      // Two single-purpose pickers (file vs folder): a combined dialog can't
      // select both on Linux/Windows. Hidden without the Electron bridge; in
      // a plain browser the user pastes a path instead.
      hasPicker
        ? m("div", { class: "mt-2 flex gap-2" }, [
            m(
              "button",
              {
                type: "button",
                id: "file-sharing-browse-file-btn",
                class: buttonClasses("secondary"),
                onclick: () => void controller.browseForSharePath("file"),
              },
              "Choose file…",
            ),
            m(
              "button",
              {
                type: "button",
                id: "file-sharing-browse-folder-btn",
                class: buttonClasses("secondary"),
                onclick: () => void controller.browseForSharePath("directory"),
              },
              "Choose folder…",
            ),
          ])
        : null,
    ]),
    noticeStack(controller, m("span", { class: "font-semibold" }, "Granting permission...")),
  ]);
}

function accountsDetail(controller: InboxDetailController, payload: InboxDetailPayload & { kind: "accounts" }): m.Children {
  const wsLabel = payload.ws_name !== "" ? payload.ws_name : payload.agent_id;
  return m("div", { class: "permissions-detail" }, [
    detailHeader(m("span", { class: "font-bold" }, "Account access"), payload.ws_name, payload.agent_id, payload.rationale),
    permissionsForm(controller, [
      m("p", { class: "type-body text-primary mb-3" }, [
        `Approving will let ${wsLabel} and its sibling agents `,
        m("strong", "list the accounts signed in on this device"),
        " (their ids and emails), so it can associate a workspace with one of them. It does not grant " +
          "access to any account's data.",
      ]),
    ]),
    noticeStack(controller, m("span", { class: "font-semibold" }, "Granting permission...")),
  ]);
}

function workspaceVerbCheckbox(controller: InboxDetailController, verb: WorkspacePermissionDetail["verbs"][number]): m.Children {
  return m("label", { class: "flex items-start gap-2 cursor-pointer" }, [
    m("input", {
      type: "checkbox",
      name: "permissions",
      value: verb.permission,
      class: "mt-1 shrink-0",
      checked: controller.checkedPermissions.has(verb.permission),
      onchange: (event: Event) => controller.togglePermission(verb.permission, (event.target as HTMLInputElement).checked),
    }),
    m("span", { class: "min-w-0" }, [
      m("code", { class: "type-body font-mono text-primary" }, verb.display_name),
      verb.description !== "" ? m("span", { class: "block type-body text-primary mt-0.5" }, verb.description) : null,
    ]),
  ]);
}

function workspaceDetail(controller: InboxDetailController, payload: WorkspacePermissionDetail): m.Children {
  const wsLabel = payload.ws_name !== "" ? payload.ws_name : payload.agent_id;
  const generalVerbs = payload.verbs.filter((verb) => !verb.is_targeted);
  const targetedVerbs = payload.verbs.filter((verb) => verb.is_targeted);
  return m("div", { class: "permissions-detail" }, [
    detailHeader(m("span", { class: "font-bold" }, payload.display_name), payload.ws_name, payload.agent_id, payload.rationale),
    permissionsForm(controller, [
      m(
        "p",
        { class: "type-body text-primary mb-3" },
        `Approving will grant ${wsLabel} and its sibling agents the selected cross-workspace permissions below.`,
      ),
      m("h2", { class: "type-label text-primary mb-2" }, "General permissions"),
      m("p", { class: "type-body text-primary mb-2" }, "These apply across all workspaces (current and future)."),
      m("div", { class: "minds-card p-4 space-y-3" }, generalVerbs.map((verb) => workspaceVerbCheckbox(controller, verb))),
      m("h2", { class: "type-label text-primary mt-6 mb-2" }, "Workspace-specific permissions"),
      payload.show_target_choice
        ? m("p", { class: "type-body text-primary mb-2" }, "These act on individual workspaces.")
        : m(
            "div",
            { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-warning-surface)] text-warning" },
            [
              "This request did not name a specific workspace, so any workspace-specific permissions you " +
                "grant will apply to ",
              m("span", { class: "font-semibold" }, "all workspaces"),
              " (current and future). Grant them only if you intend that broad access.",
            ],
          ),
      m("div", { class: "minds-card p-4 space-y-3" }, targetedVerbs.map((verb) => workspaceVerbCheckbox(controller, verb))),
      m("h2", { class: "type-label text-primary mt-6 mb-2" }, "Apply workspace-specific permissions to:"),
      m(
        "div",
        { class: "minds-card p-4 space-y-2" },
        [
          payload.show_target_choice
            ? m("label", { class: "flex items-start gap-2 cursor-pointer" }, [
                m("input", {
                  type: "radio",
                  name: "target_scope",
                  value: "selected",
                  class: "mt-1 shrink-0",
                  checked: controller.targetScope === "selected",
                  onchange: () => {
                    controller.targetScope = "selected";
                    m.redraw();
                  },
                }),
                m("span", { class: "min-w-0" }, [
                  m("span", { class: "type-body text-primary" }, "Only this workspace"),
                  m(
                    "code",
                    { class: "block type-body font-mono text-primary mt-0.5" },
                    payload.target_workspace_name !== "" ? payload.target_workspace_name : payload.target_workspace_id,
                  ),
                ]),
              ])
            : null,
          m("label", { class: "flex items-start gap-2 cursor-pointer" }, [
            m("input", {
              type: "radio",
              name: "target_scope",
              value: "all",
              class: "mt-1 shrink-0",
              checked: controller.targetScope === "all",
              onchange: () => {
                controller.targetScope = "all";
                m.redraw();
              },
            }),
            m("span", { class: "min-w-0" }, [
              m("span", { class: "type-body text-primary" }, "All workspaces"),
              m(
                "span",
                { class: "block type-body text-primary mt-0.5" },
                "Grant the selected actions for every workspace (current and future).",
              ),
            ]),
          ]),
        ],
      ),
    ]),
    noticeStack(controller, m("span", { class: "font-semibold" }, "Granting permission...")),
  ]);
}

export interface InboxDetailAttrs {
  controller: InboxDetailController;
}

export function InboxDetail(): m.Component<InboxDetailAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { payload } = controller;
      switch (payload.kind) {
        case "unavailable":
          return m("div", { class: "permissions-detail" }, [
            m("h1", { class: "type-heading text-primary" }, "This permission request is no longer available"),
            payload.message !== "" ? m("p", { class: "mt-2 text-secondary" }, payload.message) : null,
          ]);
        case "predefined":
          return predefinedDetail(controller, payload);
        case "file_sharing":
          return fileSharingDetail(controller, payload);
        case "accounts":
          return accountsDetail(controller, payload);
        case "workspace":
          return workspaceDetail(controller, payload);
      }
    },
  };
}
