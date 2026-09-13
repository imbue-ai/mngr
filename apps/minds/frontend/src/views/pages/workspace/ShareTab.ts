// The Share machine tab: per-target nav (apps + whole machine), the grants
// editor (owner row + removable entries + add box), the share link pill with
// copy confirmation, and the provisioning notice. View over ShareModel.

import m from "mithril";
import { shareTargetIconMarkup } from "../../components/appIcon";
import { Button } from "../../components/Button";
import { Icon16 } from "../../components/Icon";
import { Notice } from "../../components/Notice";
import { Spinner } from "../../components/Spinner";
import { TextInput } from "../../components/FormControls";
import type { ShareModel } from "../../../models/workspaceOptions";
import { navEntryClass, splitPane } from "../../components/SplitPane";

const COPY_FLASH_MS = 1200;

export interface ShareTabAttrs {
  share: ShareModel;
  workspaceName: string;
}

interface ShareTabLocalState {
  addEntryDraft: string;
  isCopyConfirmed: boolean;
  copyFlashTimer: number | null;
}

export function ShareTab(): m.Component<ShareTabAttrs> {
  const local: ShareTabLocalState = {
    addEntryDraft: "",
    isCopyConfirmed: false,
    copyFlashTimer: null,
  };

  return {
    onremove() {
      if (local.copyFlashTimer !== null)
        window.clearTimeout(local.copyFlashTimer);
    },
    view(vnode) {
      const { share } = vnode.attrs;
      const isWhole = share.currentTarget === shareWholeService(share);

      return splitPane({
        navLabel: "Share targets",
        nav: renderTargetNav(share, local),
        content: [
          m("div", { class: "flex items-center gap-2" }, [
            m(
              "span",
              { class: "shrink-0 text-primary" },
              m(Icon16, {
                name: isWhole ? "panels-top-left" : "box",
                size: "lg",
              }),
            ),
            m(
              "h2",
              { class: "type-heading text-primary" },
              isWhole ? "Whole machine" : share.currentTarget,
            ),
          ]),
          m(
            "p",
            { class: "mt-1 type-helper text-tertiary" },
            isWhole
              ? "Give access to everything in this machine, including every app."
              : "Give access only to this app on its own.",
          ),
          share.status === "loading" || share.status === "idle"
            ? m("p", { class: "mt-6 type-body text-secondary" }, [
                m(Spinner, { size: "sm", extra: "mr-1" }),
                " Loading sharing status...",
              ])
            : null,
          share.errorMessage
            ? m("div", { class: "mt-4" }, [
                m(Notice, { variant: "warn" }, share.errorMessage),
                share.isRetryOffered
                  ? m(
                      "div",
                      { class: "mt-2" },
                      m(
                        Button,
                        {
                          variant: "secondary",
                          onclick: () => void share.load(),
                        },
                        "Try again",
                      ),
                    )
                  : null,
              ])
            : null,
          share.status === "ready" ? renderEditor(share, local) : null,
        ],
        extra: "mt-8",
      });
    },
  };
}

function shareWholeService(share: ShareModel): string {
  return share.wholeService;
}

function renderTargetNav(
  share: ShareModel,
  local: ShareTabLocalState,
): m.Children {
  const wholeService = shareWholeService(share);
  const appServices = share.knownTargets.filter(
    (target) => target !== wholeService,
  );

  const targetButton = (
    target: string,
    label: string,
    icon: m.Children,
  ): m.Children =>
    m(
      "button",
      {
        type: "button",
        "data-share-target": target,
        "aria-pressed": target === share.currentTarget ? "true" : "false",
        class: navEntryClass(target === share.currentTarget),
        onclick: () => {
          local.addEntryDraft = "";
          cancelCopyFlash(local);
          share.selectTarget(target);
        },
      },
      [icon, m("span", { class: "truncate" }, label)],
    );

  // Each app wears the icon it registered (sanitized) or its monogram --
  // exactly how the workspace itself draws it -- so the share list reads as
  // the same apps the user already knows.
  const appIcon = (service: string): m.Children =>
    m(
      "span",
      { class: "shrink-0 inline-flex" },
      m.trust(shareTargetIconMarkup(share.targetIcon(service), service, 16)),
    );

  return [
    appServices.length > 0
      ? [
          m(
            "div",
            { class: "flex flex-col gap-0.5" },
            appServices.map((service) => targetButton(service, service, appIcon(service))),
          ),
          m("div", { class: "my-1.5 h-px bg-subtle" }),
        ]
      : null,
    targetButton(wholeService, "Whole machine", m(Icon16, { name: "panels-top-left", extra: "shrink-0" })),
  ];
}

function renderEditor(
  share: ShareModel,
  local: ShareTabLocalState,
): m.Children {
  const target = share.currentTarget;
  const state = share.targetState(target);
  const pending = share.pendingKind(target);
  const isDisabling = pending === "disable";
  const url = share.targetUrl(target);
  const ownerEmail = shareOwnerEmail(share);

  return m("div", { class: "mt-6 flex flex-col gap-6" }, [
    m("section", [
      m(
        "h3",
        { class: "type-body font-semibold text-primary" },
        "Who are you sharing with?",
      ),
      m("div", { id: "ws-share-emails", class: "mt-3 flex flex-col gap-1.5" }, [
        ownerEmail ? renderAclRow(share, ownerEmail, true) : null,
        ...state.entries.map((entry) => renderAclRow(share, entry, false)),
      ]),
      m("div", { class: "mt-2 flex items-center gap-2" }, [
        m(TextInput, {
          id: "ws-share-new-email",
          name: "ws_share_new_email",
          placeholder: "Add email, or a domain to admit everyone at it",
          extra: "flex-1",
          value: local.addEntryDraft,
          disabled: !share.isEditorEditable,
          oninput: (event: InputEvent) => {
            local.addEntryDraft = (event.target as HTMLInputElement).value;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addDraftEntry(share, local);
            }
          },
        }),
        m(
          Button,
          {
            id: "ws-share-add-btn",
            variant: local.addEntryDraft.trim() ? "primary" : "secondary",
            disabled: !share.isEditorEditable,
            onclick: () => addDraftEntry(share, local),
          },
          "Add",
        ),
      ]),
    ]),

    m("section", [
      m(
        "h3",
        { class: "type-body font-semibold text-primary mb-3" },
        "Share link",
      ),
      !state.isEnabled && !isDisabling
        ? m(
            "div",
            { id: "ws-share-enable-row", class: "flex items-center gap-3" },
            [
              m(
                Button,
                {
                  id: "ws-share-enable-btn",
                  variant: "primary",
                  disabled: pending !== null,
                  onclick: () => void share.enable(local.addEntryDraft),
                },
                pending === "enable" ? "Enabling..." : "Enable sharing",
              ),
              pending === "enable"
                ? m(
                    "span",
                    {
                      id: "ws-share-enable-status",
                      class: "type-helper text-tertiary",
                    },
                    [
                      m(Spinner, { size: "sm", extra: "mr-1" }),
                      " Registering the share link...",
                    ],
                  )
                : null,
            ],
          )
        : null,
      state.isEnabled && !isDisabling
        ? m(
            "div",
            {
              id: "ws-share-url-row",
              class: "flex items-center gap-2 flex-wrap",
            },
            [
              m(
                "button",
                {
                  id: "ws-share-url-btn",
                  type: "button",
                  class:
                    "inline-flex items-center gap-2 max-w-full rounded-full border border-default " +
                    "bg-fill-subtle px-3 py-1.5 type-body font-mono text-primary cursor-pointer " +
                    "hover:bg-fill-hover transition-colors",
                  style: local.isCopyConfirmed
                    ? "border-color: var(--c-success); background-color: var(--c-success-surface);"
                    : "",
                  "aria-label": "Copy the share link",
                  onclick: () => void copyShareUrl(share, local),
                },
                [
                  m("span", { id: "ws-share-url", class: "truncate" }, url),
                  m(Icon16, {
                    name: local.isCopyConfirmed ? "check" : "copy",
                    extra: local.isCopyConfirmed
                      ? "shrink-0 text-primary"
                      : "shrink-0 text-tertiary",
                  }),
                ],
              ),
              m(
                Button,
                {
                  variant: "secondary",
                  disabled: pending !== null,
                  onclick: () => void share.disable(),
                },
                "Stop sharing",
              ),
            ],
          )
        : null,
      isDisabling
        ? m(
            "p",
            {
              id: "ws-share-busy",
              class: "flex items-center gap-2 type-body text-secondary",
            },
            [
              m(Spinner, { size: "sm" }),
              "Stopping sharing and revoking the link...",
            ],
          )
        : null,
      pending === "emails"
        ? m(
            "p",
            { class: "flex items-center gap-2 type-body text-secondary mt-2" },
            [m(Spinner, { size: "sm" }), "Updating who can open this link..."],
          )
        : null,
      share.isAwaitingLink(target) && !isDisabling
        ? m(
            "div",
            { id: "ws-share-provisioning", class: "mt-3" },
            m(Notice, { variant: "info" }, [
              m(
                "p",
                "The link is not live yet -- setting it up usually takes under a minute:",
              ),
              renderProvisioningChecklist(share),
            ]),
          )
        : null,
    ]),
  ]);
}

interface ProvisioningStep {
  label: string;
  isDone: boolean;
}

// The provisioning checklist shown while the link is not yet live: each step's
// signal comes from the readiness poll (certificate issuance and a fresh
// tunnel login from the connector's share status; the end-to-end check is the
// probe itself, which dismisses this whole notice when it succeeds).
function renderProvisioningChecklist(share: ShareModel): m.Children {
  const steps: ProvisioningStep[] = [
    { label: "Share link registered", isDone: true },
    { label: "TLS certificate issued", isDone: share.isCertIssued },
    { label: "Tunnel connected to the relay", isDone: share.isTunnelConnected },
    { label: "Link answers end to end", isDone: false },
  ];
  const firstNotDoneIdx = steps.findIndex((step) => !step.isDone);
  return m(
    "ul",
    { id: "ws-share-provisioning-steps", class: "mt-2 flex flex-col gap-1" },
    steps.map((step, idx) =>
      m(
        "li",
        {
          class: "flex items-center gap-2",
          "data-step-done": step.isDone ? "true" : "false",
        },
        [
          step.isDone
            ? m(Icon16, { name: "check", extra: "shrink-0 text-primary" })
            : idx === firstNotDoneIdx
              ? m(Spinner, { size: "sm", extra: "shrink-0" })
              : m(
                  "span",
                  {
                    class:
                      "inline-block w-4 shrink-0 text-center text-tertiary",
                  },
                  "-",
                ),
          m(
            "span",
            {
              class:
                step.isDone || idx === firstNotDoneIdx ? "" : "text-tertiary",
            },
            step.label,
          ),
        ],
      ),
    ),
  );
}

function shareOwnerEmail(share: ShareModel): string {
  return share.ownerEmail;
}

function renderAclRow(
  share: ShareModel,
  entry: string,
  isOwner: boolean,
): m.Children {
  const isEmail = entry.includes("@");
  return m(
    "div",
    {
      class:
        "flex items-center justify-between gap-2 rounded-md border border-subtle bg-fill-subtle px-3 py-2",
    },
    [
      m("span", { class: "type-body text-primary truncate" }, [
        entry,
        isOwner ? m("span", { class: "text-tertiary" }, " (you)") : null,
        !isOwner && !isEmail
          ? m("span", { class: "text-tertiary" }, " (anyone at this domain)")
          : null,
      ]),
      !isOwner
        ? m(
            "button",
            {
              type: "button",
              class:
                "shrink-0 inline-flex h-6 w-6 items-center justify-center rounded-md text-tertiary " +
                "hover:bg-fill-hover hover:text-important cursor-pointer transition-colors",
              "aria-label": `Remove ${entry}`,
              disabled: !share.isEditorEditable,
              onclick: () => share.removeEntry(entry),
            },
            m(Icon16, { name: "close" }),
          )
        : null,
    ],
  );
}

function addDraftEntry(share: ShareModel, local: ShareTabLocalState): void {
  const entry = local.addEntryDraft.trim();
  if (!entry) return;
  share.addEntry(entry);
  local.addEntryDraft = "";
}

async function copyShareUrl(
  share: ShareModel,
  local: ShareTabLocalState,
): Promise<void> {
  const url = share.targetUrl(share.currentTarget);
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
  } catch (error) {
    share.errorMessage =
      "Could not copy the link: " +
      (error instanceof Error ? error.message : String(error));
    m.redraw();
    return;
  }
  local.isCopyConfirmed = true;
  if (local.copyFlashTimer !== null) window.clearTimeout(local.copyFlashTimer);
  local.copyFlashTimer = window.setTimeout(() => {
    local.copyFlashTimer = null;
    local.isCopyConfirmed = false;
    m.redraw();
  }, COPY_FLASH_MS);
  m.redraw();
}

function cancelCopyFlash(local: ShareTabLocalState): void {
  if (local.copyFlashTimer !== null) window.clearTimeout(local.copyFlashTimer);
  local.copyFlashTimer = null;
  local.isCopyConfirmed = false;
}
