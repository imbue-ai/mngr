// The share panel: one switch publishes the whole workspace, and each target
// shows the link it opens and the people it admits. Nothing here waits on a
// write: every control stays live while the model saves behind it.

import m from "mithril";
import { shareTargetIconMarkup } from "../../components/appIcon";
import { Button } from "../../components/Button";
import { Collapsible } from "../../components/Collapsible";
import { FormLabel, Select, TextInput } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { CopyField } from "../../components/Layout";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
import { routeLinkAttrs } from "../../components/route-link";
import { Skeleton, SkeletonRegion } from "../../components/Skeleton";
import { Spinner } from "../../components/Spinner";
import type {
  Grant,
  GrantAddKind,
  InvitationStatusIndicator,
  MobileLinkState,
  SharePanelModel,
  VisitingAgentInviteState,
  WorkspaceAccess,
} from "../../../models/sharePanel";
import type { VisitingAgentEntry } from "../../../models/workspaceOptions";
import { GRANT_ADD_KINDS, toGrantAddKind } from "../../../models/sharePanel";
import { formatRelativeAgo } from "../../../models/backups";
import { navEntryClass, splitPane } from "../../components/SplitPane";
import {
  renderSwitch,
  renderSwitchPlaceholder,
  renderSwitchWord,
} from "../../components/Switch";

const JUST_ADDED_MS = 6000;

const COPY_FLASH_MS = 1200;

// Exported for the create form's toggle, which throws this same switch for a
// just-created workspace: the two must read alike.
export const ENABLE_SHARING_LABEL = "Enable sharing and web access";

export const SHARING_SENTENCE =
  "Sharing gives this workspace an address on the internet, which allows you " +
  "to access it over the web and on mobile. No one else will get access to " +
  "your workspace unless you explicitly grant them access.";

const PROVISIONING_SENTENCE =
  "Generating a secure link. This takes a while because we want to protect " +
  "you from bad actors on the internet.";

const CONFIRM_BODY =
  "Anyone granted access to this workspace will be able to see and change " +
  "everything within it, including all files, agent chats and terminal. If " +
  "you only grant access to an app, they will be able to access all data " +
  "that app provides to them.";

const OFF_NOTICE_WORKSPACE =
  "This workspace cannot be accessed while sharing is off. " +
  "Your list of permissions is preserved but inactive.";

const OFF_NOTICE_APP =
  "This app cannot be accessed while sharing is off. " +
  "Your list of permissions is preserved but inactive.";

const WHOLE_SCOPE_LINE =
  "Permissions below apply to every app in this workspace, and also grant " +
  "access to files, agent chats and terminal.";

const SAVING_LABEL = "Securely granting access";

const ADD_OFF_TOOLTIP = "Permissions cannot be granted while sharing is off";

const ADD_UNKNOWN_TOOLTIP =
  "Permissions cannot be granted until the sharing status has loaded";

// The control is drawn only on the whole workspace's pane, because the message
// carries that link and no app's. It is drawn whether or not sharing is on, so
// a reader learns the feature exists and what switch turns it on.
const MOBILE_LINK_LABEL = "Email me a Mobile Access Link";

const VISITING_AGENTS_HEADING = "Visiting agents";
const VISITING_AGENTS_WORKSPACE_SENTENCE =
  "Invite a coding agent running somewhere else (on your computer, in a cloud " +
  "agent product, on a collaborator's machine) to work in this workspace with " +
  "exactly this workspace's access, and nothing more.";
const VISITING_AGENTS_APP_SENTENCE =
  "Invite a coding agent running somewhere else to use only this app.";
const VISITING_AGENT_INVITE_LABEL = "Invite an agent";
const VISITING_AGENT_COPY_LABEL = "Copy the instructions";
const VISITING_AGENT_DONE_LABEL = "Done";
const VISITING_AGENT_REVOKE_LABEL = "Revoke";
const VISITING_AGENTS_EMPTY = "No agents have been invited.";
const VISITING_AGENTS_RETRY_LABEL = "Try again";
const VISITING_AGENT_INSTRUCTIONS_LEAD =
  "Paste these instructions to the agent. The code works once and expires in fifteen minutes.";

const MOBILE_LINK_OFF_TOOLTIP =
  "Enable sharing and web access to access this link on mobile";

const SYNC_PENDING_NOTICE =
  "The permissions have not reached Imbue Cloud yet, so nobody can be " +
  "invited. They will be sent again in a moment.";

/** What each kind of grant is called at the add row, and the example it shows. */
const ADD_KIND_TEXT: Record<
  GrantAddKind,
  { readonly label: string; readonly placeholder: string }
> = {
  email: { label: "one person by email", placeholder: "name@example.com" },
  email_domain: { label: "everyone at a domain", placeholder: "example.com" },
};

// The same box CopyField draws, for the wait that stands in its place: the
// field must not drop in and push the pane down when the link arrives.
const LINK_BOX_CLASS =
  "flex flex-1 min-w-0 items-center gap-2 rounded-md border border-default " +
  "bg-fill-subtle px-3 py-2 type-body text-tertiary";

// A one-line row, for an entry with nothing to say under its name.
const ROW_CLASS =
  "flex h-10 flex-none items-center gap-2 overflow-hidden rounded-md border px-3";

// A person's card. Its first line is the same height on every card, so a long
// list scans as a single column; the lines under it wrap rather than cut
// anything short.
const CARD_CLASS =
  "flex flex-none flex-col gap-0.5 overflow-hidden rounded-md border px-3 py-2";

const CARD_NAME_LINE_CLASS = "flex h-6 items-center gap-2";

// pl-8 is the lead's width plus the line's gap, which starts the words where
// the name starts.
const CARD_DETAIL_CLASS = "pl-8 type-helper";

// A domain row's outline is mixed from its surface, so the two read as one
// mark.
const ROW_DOMAIN_CLASS =
  "bg-[var(--c-info-surface)] border-[color-mix(in_srgb,var(--c-info)_28%,transparent)]";

const ROW_PERSON_CLASS = "bg-fill-subtle border-subtle";

// While sharing is off a row admits nobody, so every kind is drawn alike:
// flat, grey and dim, as the notice above the list says. The lead box and
// the row's own colors ride along under the filter.
const ROW_INACTIVE_CLASS = "bg-fill-subtle border-subtle opacity-60 grayscale";

const COUNT_BADGE_CLASS =
  "shrink-0 inline-flex min-w-5 items-center justify-center rounded-md " +
  "bg-fill-subtle px-1.5 type-helper font-semibold text-secondary";

const ROW_LEAD_CLASS =
  "shrink-0 inline-flex h-6 w-6 items-center justify-center rounded-full";

export interface ShareTabAttrs {
  share: SharePanelModel;
  workspaceName: string;
}

interface ShareTabLocalState {
  isConfirmOpen: boolean;
  /** Whether the app pane's inherited-permissions group is expanded. */
  isInheritedOpen: boolean;
  /** Whether the nav's System group is expanded; null until the reader toggles
   * it, while it follows `isSystemGroupNeeded`. */
  isSystemOpen: boolean | null;
  copied: CopiedLink | null;
  copyFlashHandle: ScheduleHandle | null;
}

/** The link last copied, so the check sits on its own control and leaves on its
 * own once the flash has run, whether or not the scheduled redraw arrives. */
interface CopiedLink {
  readonly target: string;
  /** By the panel's clock. */
  readonly atMs: number;
}

type ScheduleHandle = ReturnType<SharePanelModel["schedule"]>;

export function ShareTab(): m.Component<ShareTabAttrs> {
  const local: ShareTabLocalState = {
    isConfirmOpen: false,
    isInheritedOpen: false,
    isSystemOpen: null,
    copied: null,
    copyFlashHandle: null,
  };

  return {
    onremove(vnode) {
      if (local.copyFlashHandle !== null)
        vnode.attrs.share.cancel(local.copyFlashHandle);
    },
    view(vnode) {
      const { share } = vnode.attrs;
      return m("div", { class: "mt-6 flex flex-1 min-h-0 flex-col gap-6" }, [
        renderPublishWidget(share, local),
        m("hr", { class: "border-t border-default" }),
        splitPane({
          navLabel: "Share targets",
          nav: renderTargetNav(share, local),
          content: renderTargetPane(share, local),
          contentExtra: "flex flex-col",
        }),
        renderConfirmDialog(share, local),
      ]);
    },
  };
}

function renderPublishWidget(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const publishWrite = share.publishWrite;
  return m(
    "section",
    { id: "ws-share-publish", class: "shrink-0 flex flex-col gap-2" },
    [
      m("div", { class: "flex items-center justify-between gap-4" }, [
        m("h2", { class: "type-heading text-primary" }, ENABLE_SHARING_LABEL),
        m("span", { class: "flex shrink-0 items-center gap-2" }, [
          share.isPublicationKnown
            ? renderPublishSwitch(share, local)
            : renderPublishPlaceholder(share),
          renderSwitchWord({
            id: "ws-share-publish-word",
            isOn: share.isPublicationKnown ? share.isPublished : null,
          }),
        ]),
      ]),
      m("p", { class: "type-body text-secondary" }, SHARING_SENTENCE),
      share.isPublished && !share.isLive ? renderProvisioning(share) : null,
      publishWrite.state === "failed"
        ? m(
            "p",
            {
              id: "ws-share-publish-error",
              class: "type-helper text-important",
            },
            [
              publishWrite.message,
              publishWrite.remedyPath !== ""
                ? [
                    " ",
                    m(
                      "a",
                      {
                        id: "ws-share-publish-remedy",
                        class: "underline",
                        ...routeLinkAttrs(publishWrite.remedyPath),
                      },
                      "Review shared workspaces",
                    ),
                  ]
                : null,
            ],
          )
        : null,
      share.loadErrorMessage === null
        ? null
        : m(Notice, { variant: "warn" }, share.loadErrorMessage),
      share.migratedDomainFrom === null
        ? null
        : m(
            Notice,
            { variant: "info" },
            "Sharing moved to a new address. Links you shared before no longer work.",
          ),
    ],
  );
}

/** The switch. It stays live through a toggle and through a fresh read of the
 * value on screen; either wait spins in its knob rather than beside it. */
function renderPublishSwitch(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const publishWrite = share.publishWrite;
  const isSettling =
    share.isRevalidating ||
    publishWrite.state === "publishing" ||
    publishWrite.state === "unpublishing";
  return renderSwitch({
    id: "ws-share-publish-switch",
    isOn: share.isPublished,
    label: ENABLE_SHARING_LABEL,
    isWaiting: isSettling,
    onFlip: (isOn) => {
      if (isOn) local.isConfirmOpen = true;
      else void share.unpublish();
    },
  });
}

function renderPublishPlaceholder(share: SharePanelModel): m.Children {
  const isChecking = share.isCheckingPublication;
  return renderSwitchPlaceholder({
    id: "ws-share-publish-unknown",
    isChecking,
    label: isChecking
      ? "Checking whether sharing is enabled"
      : "Sharing status unknown",
  });
}

function renderConfirmDialog(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  return m(
    Modal,
    {
      isOpen: local.isConfirmOpen,
      onClose: () => {
        local.isConfirmOpen = false;
      },
    },
    [
      m("h2", { class: "type-heading text-primary mb-3" }, "Enable sharing?"),
      m("p", { class: "type-body text-secondary mb-4" }, CONFIRM_BODY),
      m("div", { class: "flex justify-end gap-3" }, [
        m(
          Button,
          {
            id: "ws-share-publish-cancel",
            variant: "secondary",
            onclick: () => {
              local.isConfirmOpen = false;
            },
          },
          "Cancel",
        ),
        m(
          Button,
          {
            id: "ws-share-publish-confirm",
            variant: "primary",
            onclick: () => {
              local.isConfirmOpen = false;
              void share.publish();
            },
          },
          "Enable sharing",
        ),
      ]),
    ],
  );
}

function renderProvisioning(share: SharePanelModel): m.Children {
  return m(
    "div",
    { id: "ws-share-provisioning", class: "flex flex-col gap-2" },
    [
      m("p", { class: "type-body text-secondary" }, PROVISIONING_SENTENCE),
      m("p", { class: "flex items-center gap-2 type-body text-primary" }, [
        m(Spinner, { size: "sm", extra: "shrink-0" }),
        share.activeProvisioningStep.label,
      ]),
      m(
        "p",
        { class: "type-helper text-tertiary" },
        "People can be added while the link is being prepared.",
      ),
      renderGatewayTrouble(share),
    ],
  );
}

function renderGatewayTrouble(share: SharePanelModel): m.Children {
  // Gateway errors often end in their own period; do not add a second one.
  const gatewayError = share.gatewayError?.replace(/\.\s*$/, "") ?? null;
  if (share.isProvisioningHalted) {
    return m(
      "p",
      { id: "ws-share-gateway-trouble", class: "type-helper text-important" },
      [
        gatewayError
          ? `The workspace could not set up its certificate or tunnel: ${gatewayError}. `
          : "The workspace could not set up its certificate or tunnel. ",
        "Turn sharing off and on again to retry.",
      ],
    );
  }
  if (share.gatewayState !== "retrying" || share.gatewayFailedAttemptCount < 1)
    return null;
  const nextRetry = share.gatewayNextRetryAt
    ? new Date(share.gatewayNextRetryAt).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      })
    : null;
  return m(
    "p",
    { id: "ws-share-gateway-trouble", class: "type-helper text-warning" },
    [
      gatewayError
        ? `The last attempt failed: ${gatewayError}. `
        : "The last attempt failed. ",
      nextRetry
        ? `The workspace retries at ${nextRetry}.`
        : "The workspace will retry shortly.",
    ],
  );
}

function renderTargetNav(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  return [
    renderNavEntry(
      share,
      share.wholeService,
      "Whole workspace",
      m(Icon16, { name: "panels-top-left", extra: "shrink-0" }),
    ),
    ...share.appTargets.map((target) => renderAppNavEntry(share, target)),
    ...(share.systemTargets.length === 0
      ? []
      : [renderSystemGroup(share, local)]),
  ];
}

function renderAppNavEntry(share: SharePanelModel, target: string): m.Children {
  return renderNavEntry(
    share,
    target,
    share.targetDisplayName(target),
    renderAppIcon(share, target),
  );
}

/**
 * The system apps, behind one row that opens them. Each is a target like any
 * other, but none is meant to be shared on its own by default, so they stay out
 * of the way of the apps.
 */
function renderSystemGroup(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const isOpen = local.isSystemOpen ?? isSystemGroupNeeded(share);
  return m(
    Collapsible,
    {
      id: "ws-share-system",
      isOpen,
      onToggle: () => {
        local.isSystemOpen = !isOpen;
      },
      extra: "mt-1 flex flex-col",
      triggerExtra:
        "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left " +
        "type-helper text-tertiary hover:text-secondary",
      panelExtra: "flex flex-col",
      summary: "System apps",
    },
    share.systemTargets.map((target) => renderAppNavEntry(share, target)),
  );
}

/** Whether the System group has to start open: a system app is selected (a
 * deep link to one), or one already admits someone, which a closed group would
 * keep out of sight. */
function isSystemGroupNeeded(share: SharePanelModel): boolean {
  return share.systemTargets.some(
    (target) => target === share.currentTarget || share.grantCount(target) > 0,
  );
}

function renderNavEntry(
  share: SharePanelModel,
  target: string,
  label: string,
  icon: m.Children,
): m.Children {
  const isSelected = target === share.currentTarget;
  return m(
    "button",
    {
      type: "button",
      "data-share-target": target,
      "aria-pressed": isSelected ? "true" : "false",
      class: navEntryClass(isSelected),
      onclick: () => share.selectTarget(target),
    },
    [
      icon,
      m("span", { class: "grow truncate" }, [
        label,
        // Two apps may be called the same; their own names tell the rows apart.
        // The space is the text's own: a margin is not one, and a reader would
        // otherwise announce the two names run together.
        share.isDisplayNameAmbiguous(target)
          ? m(
              "span",
              { class: "ml-1.5 type-helper text-tertiary" },
              ` ${target}`,
            )
          : null,
      ]),
      // An app registers its address when it starts, so one that has never run
      // has no link yet -- which is not the same as having no grants. Both
      // this and the count come from the document, so until one lands the row
      // says neither: an unregistered label and an empty list look exactly
      // like a document that has not arrived.
      share.isSharingDocumentLoading ||
      target === share.wholeService ||
      share.isLabelKnown(target)
        ? null
        : m(
            "span",
            { class: "shrink-0 type-helper text-tertiary" },
            "no link yet",
          ),
      share.isSharingDocumentLoading
        ? m(Skeleton, { extra: "h-5 w-5 shrink-0" })
        : m(
            "span",
            { "data-share-count": target, class: COUNT_BADGE_CLASS },
            String(share.grantCount(target)),
          ),
    ],
  );
}

// The icon the app registered, or its monogram, drawn as the workspace draws
// it.
function renderAppIcon(share: SharePanelModel, target: string): m.Children {
  return m(
    "span",
    { class: "shrink-0 inline-flex" },
    m.trust(
      shareTargetIconMarkup(
        share.targetIcon(target),
        share.targetDisplayName(target),
        16,
      ),
    ),
  );
}

/** What to call a target in prose: what the workspace calls it, and its own name
 * too when another target is called the same. */
function targetTitle(share: SharePanelModel, target: string): string {
  const shown = share.targetDisplayName(target);
  return share.isDisplayNameAmbiguous(target) ? `${shown} (${target})` : shown;
}

function renderTargetPane(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const target = share.currentTarget;
  const isWhole = target === share.wholeService;
  if (share.isSharingDocumentLoading) return renderTargetPaneSkeleton();
  return [
    m("div", { class: "shrink-0 flex items-center gap-2" }, [
      isWhole
        ? m(Icon16, {
            name: "panels-top-left",
            size: "lg",
            extra: "shrink-0 text-primary",
          })
        : renderAppIcon(share, target),
      m(
        "h2",
        { class: "type-heading text-primary" },
        isWhole
          ? "Permissions for the whole workspace"
          : `Permissions for ${targetTitle(share, target)}`,
      ),
    ]),
    m(
      "p",
      { class: "mt-1 shrink-0 type-helper text-tertiary" },
      isWhole
        ? WHOLE_SCOPE_LINE
        : `Permissions below apply only to the ${targetTitle(share, target)} app.`,
    ),
    share.isPublished ? renderLinkSection(share, local) : null,
    isWhole ? renderMobileLinkControl(share) : null,
    renderAddRow(share),
    share.isGrantsSyncPending
      ? m(
          "div",
          { id: "ws-share-sync-notice", class: "mt-3 shrink-0" },
          m(Notice, { variant: "warn" }, SYNC_PENDING_NOTICE),
        )
      : null,
    share.isPublished || !hasAnyGrant(share)
      ? null
      : m(
          "div",
          { id: "ws-share-off-notice", class: "shrink-0" },
          m(
            Notice,
            { variant: "info" },
            isWhole ? OFF_NOTICE_WORKSPACE : OFF_NOTICE_APP,
          ),
        ),
    renderGrantList(share, local),
    renderVisitingAgentsSection(share, local, target),
  ];
}

/**
 * The pane while no document has arrived: the same blocks in the same places,
 * so the layout does not jump when the real pane replaces it.
 *
 * Every row below stands for one the pane draws -- heading, scope line, link,
 * the mobile-link control, the add row, the list -- because drawing any of
 * them for real would state a default as a fact.
 */
function renderTargetPaneSkeleton(): m.Children {
  return m(
    SkeletonRegion,
    { label: "Loading permissions", extra: "flex flex-col" },
    [
      m("div", { class: "shrink-0 flex items-center gap-2" }, [
        m(Skeleton, { extra: "h-5 w-5 shrink-0" }),
        m(Skeleton, { extra: "h-5 w-64" }),
      ]),
      m("div", { class: "mt-2 shrink-0 flex flex-col gap-1.5" }, [
        m(Skeleton, { extra: "h-3 w-full" }),
        m(Skeleton, { extra: "h-3 w-2/5" }),
      ]),
      m("div", { class: "mt-6 shrink-0 flex flex-col gap-1.5" }, [
        m(Skeleton, { extra: "h-4 w-12" }),
        m("div", { class: "flex items-center gap-2" }, [
          m(Skeleton, { extra: "h-10 flex-1" }),
          m(Skeleton, { extra: "h-10 w-10 shrink-0" }),
        ]),
        m(Skeleton, { extra: "h-3 w-72" }),
      ]),
      m(Skeleton, { extra: "mt-4 h-9 w-64 shrink-0" }),
      m("div", { class: "mt-6 shrink-0 flex flex-col gap-1.5" }, [
        m(Skeleton, { extra: "h-4 w-36" }),
        m("div", { class: "flex items-center gap-2" }, [
          m(Skeleton, { extra: "h-10 w-52 shrink-0" }),
          m(Skeleton, { extra: "h-10 flex-1" }),
          m(Skeleton, { extra: "h-10 w-20 shrink-0" }),
        ]),
      ]),
      m(Skeleton, { extra: "mt-6 h-12 shrink-0" }),
    ],
  );
}

/** Whether anyone is granted anything anywhere in this workspace. An app's own
 * list can be empty while the whole workspace's grants still admit people to it. */
function hasAnyGrant(share: SharePanelModel): boolean {
  return share.knownTargets.some((target) => share.grantCount(target) > 0);
}

/** The kind is chosen beside the value, so the two cannot drift apart. */
function renderAddRow(share: SharePanelModel): m.Children {
  const target = share.currentTarget;
  const row = share.addRow(target);
  const offAttrs = share.canAdd
    ? {}
    : {
        "aria-disabled": "true",
        "data-tooltip": share.isPublicationKnown
          ? ADD_OFF_TOOLTIP
          : ADD_UNKNOWN_TOOLTIP,
      };
  return m("section", { id: "ws-share-add", class: "mt-6 shrink-0" }, [
    m(FormLabel, { target: "ws-share-add-value" }, "Grant permission to"),
    m(
      "div",
      {
        class: share.canAdd
          ? "flex items-center gap-2"
          : "flex items-center gap-2 opacity-40",
      },
      [
        m(
          Select,
          {
            id: "ws-share-add-kind",
            name: "ws_share_add_kind",
            width: "w-52",
            value: row.kind,
            ...offAttrs,
            onchange: (event: Event) => {
              row.kind = toGrantAddKind(
                (event.target as HTMLSelectElement).value,
              );
              row.refusalMessage = null;
            },
          },
          GRANT_ADD_KINDS.map((kind) =>
            m("option", { value: kind }, ADD_KIND_TEXT[kind].label),
          ),
        ),
        m(TextInput, {
          id: "ws-share-add-value",
          name: "ws_share_add_value",
          extra:
            row.refusalMessage === null
              ? "flex-1 min-w-0"
              : "flex-1 min-w-0 !border-important focus:!outline-important",
          placeholder: ADD_KIND_TEXT[row.kind].placeholder,
          value: row.value,
          ...offAttrs,
          oninput: (event: InputEvent) => {
            row.value = (event.target as HTMLInputElement).value;
            row.refusalMessage = null;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            submitAddRow(share);
          },
        }),
        m(
          Button,
          {
            id: "ws-share-add-btn",
            variant: "secondary",
            ...offAttrs,
            onclick: () => submitAddRow(share),
          },
          [m(Icon16, { name: "plus" }), "Add"],
        ),
      ],
    ),
    row.refusalMessage === null
      ? null
      : m(
          "p",
          {
            id: "ws-share-refusal",
            class: "mt-1.5 type-helper text-important",
          },
          row.refusalMessage,
        ),
  ]);
}

function submitAddRow(share: SharePanelModel): void {
  if (!share.canAdd) return;
  const target = share.currentTarget;
  const row = share.addRow(target);
  share.addGrant(target, row.kind, row.value);
}

/** The target's own grant list.
 *
 * It never scrolls on its own; the whole right column does. A list scrolling
 * inside that flex column is the one block its siblings can squeeze, down to
 * no height.
 *
 * An app's pane leads with what the whole workspace already admits, collapsed,
 * because those permissions apply here too and a reader deciding who can reach
 * this app has to count them in.
 */
function renderGrantList(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const grants = share.grantsFor(share.currentTarget);
  const inherited =
    share.currentTarget === share.wholeService ? [] : share.inheritedGrants();
  if (grants.length === 0 && inherited.length === 0)
    return m(
      "p",
      {
        id: "ws-share-empty",
        // mt-6 keeps the box clear of the tooltip the disabled add row shows.
        class:
          "mt-6 shrink-0 rounded-md border border-dashed border-default p-3 " +
          "type-body text-tertiary",
      },
      "Nobody has been granted access yet.",
    );
  return m(
    "div",
    {
      id: "ws-share-grants",
      class: "mt-4 flex shrink-0 flex-col gap-1.5",
    },
    // Every row carries a key, and Mithril wants all of a list's children
    // keyed or none of them: the inherited group is built in rather than left
    // as a hole beside them.
    [
      ...(inherited.length === 0
        ? []
        : [renderInheritedGroup(share, local, inherited)]),
      ...grants.map((grant) => renderGrantRow(share, grant)),
    ],
  );
}

/**
 * The inherited permissions, behind one row that opens them.
 *
 * The header sticks while the group it names is on screen and leaves with it,
 * which is what `sticky` does once the scrolling ancestor is this wrapper
 * rather than the list: the rows it labels cannot scroll away from their
 * label, and the reader is never left with a heading for rows that are gone.
 */
function renderInheritedGroup(
  share: SharePanelModel,
  local: ShareTabLocalState,
  inherited: readonly Grant[],
): m.Children {
  const isOpen = local.isInheritedOpen;
  return m(
    Collapsible,
    {
      key: "inherited",
      id: "ws-share-inherited",
      isOpen,
      onToggle: () => {
        local.isInheritedOpen = !local.isInheritedOpen;
      },
      extra: "flex flex-none flex-col gap-1.5",
      triggerExtra:
        ROW_CLASS +
        " sticky top-0 z-10 w-full bg-surface-primary " +
        "border-subtle text-left type-helper text-tertiary hover:text-secondary",
      // A rail down the group, and rows set in beside it: an inherited row is
      // the whole workspace's, and nothing else in the list is, so it has to
      // be legible as a different kind of row even once the header it hangs
      // from has scrolled away.
      //
      // The three add to the 24px (12 + 2 + 10) that, with the row's own
      // padding, begins its contents under the summary's first letter rather
      // than under the marker.
      panelExtra:
        "ml-3 flex flex-col gap-1.5 border-l-2 border-strong pl-[10px]",
      summary: inheritedSummary(inherited.length),
    },
    inherited.map((grant) => renderGrantRow(share, grant, false)),
  );
}

function inheritedSummary(count: number): string {
  const noun = count === 1 ? "permission" : "permissions";
  return `${count} ${noun} inherited from the whole workspace`;
}

/**
 * One grant, as a card: who it names on the first line, and what is known
 * about them on the lines under it.
 *
 * `isOwned` is false for a row this pane only inherits: it belongs to the
 * whole workspace, and the controls that would change it are left off rather
 * than drawn dead. Revoking it is the whole workspace's pane to do, which is
 * also the only place the change would read as what it is.
 */
function renderGrantRow(
  share: SharePanelModel,
  grant: Grant,
  isOwned: boolean = true,
): m.Children {
  const grantee = grant.grantee;
  const isDomain = grantee.kind === "email_domain";
  const text = grantText(share, grant);
  const invitation = share.invitationStatusIndicator(
    share.currentTarget,
    grant,
  );
  // An inherited row's grant, and so its visits, belong to the whole workspace.
  const access = share.workspaceAccessFor(
    isOwned ? share.currentTarget : share.wholeService,
    grant,
  );
  return m(
    "div",
    {
      key: grant.key,
      "data-grant-row": grant.key,
      class:
        CARD_CLASS +
        " " +
        rowSurfaceClass(share, isDomain) +
        (isJustAdded(share, grant) ? " grant-row-added" : ""),
    },
    [
      m("div", { class: CARD_NAME_LINE_CLASS }, [
        renderGrantLead(share, grant),
        // The name takes the line's slack and truncates under pressure, so the
        // remove control to its right always keeps its place.
        m(
          "span",
          {
            class: "min-w-0 flex-1 flex items-baseline gap-2 overflow-hidden",
          },
          [
            m(
              "span",
              {
                "data-grant-name": grant.key,
                class:
                  "min-w-0 max-w-full shrink-0 truncate type-body text-primary",
              },
              text.primary,
            ),
            // The email gives way first: the name is who the card is about.
            text.secondary === null
              ? null
              : m(
                  "span",
                  {
                    "data-grant-note": grant.key,
                    class: "min-w-0 truncate type-body text-tertiary",
                  },
                  text.secondary,
                ),
          ],
        ),
        isOwned
          ? m(
              "button",
              {
                type: "button",
                class:
                  "shrink-0 inline-flex h-6 w-6 items-center justify-center rounded-md text-tertiary " +
                  "hover:bg-fill-hover hover:text-important cursor-pointer transition-colors",
                "aria-label": isDomain
                  ? `Remove anyone at ${grantee.value}`
                  : `Remove ${text.primary}`,
                onclick: () =>
                  share.removeGrant(share.currentTarget, grant.key),
              },
              m(Icon16, { name: "close" }),
            )
          : // The row keeps the width the control would have taken, so an
            // inherited row lines up with the pane's own rows below it.
            m("span", { class: "h-6 w-6 shrink-0" }),
      ]),
      renderGrantDetails(share, grant, access, invitation),
      renderInvitationRefusal(grant, invitation),
    ],
  );
}

/** A row's surface: its kind's while sharing is on, and one flat grey for every
 * kind while it is off. */
function rowSurfaceClass(share: SharePanelModel, isDomain: boolean): string {
  if (!share.isPublished) return ROW_INACTIVE_CLASS;
  return isDomain ? ROW_DOMAIN_CLASS : ROW_PERSON_CLASS;
}

/** What is known about the card's person, as short facts joined by dots, or
 * the card's own save state while it has one. */
function renderGrantDetails(
  share: SharePanelModel,
  grant: Grant,
  access: WorkspaceAccess | null,
  invitation: InvitationStatusIndicator,
): m.Children {
  const pieces = grantDetailPieces(share, grant, access, invitation);
  if (pieces.length === 0) return null;
  // Each dot is bound to the fact after it, so a wrap never strands one at the
  // end of a line.
  const line = pieces.map((piece, index) =>
    index === 0
      ? piece
      : m("span", { class: "inline-flex items-center gap-1.5" }, [
          m("span", { "aria-hidden": "true" }, "·"),
          piece,
        ]),
  );
  return m(
    "div",
    {
      "data-grant-details": grant.key,
      class:
        CARD_DETAIL_CLASS +
        " flex flex-wrap items-center gap-x-1.5 text-tertiary",
    },
    line,
  );
}

function grantDetailPieces(
  share: SharePanelModel,
  grant: Grant,
  access: WorkspaceAccess | null,
  invitation: InvitationStatusIndicator,
): m.Children[] {
  const status = grant.status;
  if (status.state === "saving")
    return [
      m("span", { class: "flex items-center gap-1.5" }, [
        m(Spinner, { size: "sm", extra: "shrink-0" }),
        SAVING_LABEL,
      ]),
    ];
  if (status.state === "failed")
    return [
      m("span", { class: "text-important" }, status.failureMessage),
      textButton("Retry", () =>
        share.retryGrant(share.currentTarget, grant.key),
      ),
    ];
  const grantee = grant.grantee;
  // Nobody is told about a domain grant; the card admits people the granter
  // has never named.
  if (grantee.kind === "email_domain")
    return [
      m(
        "span",
        `People at ${grantee.value} will not hear about this unless you ` +
          "send them the link yourself.",
      ),
    ];
  const pieces: m.Children[] = [];
  if (access !== null) pieces.push(renderWorkspaceAccess(access));
  const invitationStatus = renderInvitationStatus(invitation);
  if (invitationStatus !== null) pieces.push(invitationStatus);
  if (isReinviteOffered(invitation)) pieces.push(reinviteButton(share, grant));
  return pieces;
}

/** Where the person's invitation stands, or null when the details have nothing
 * to say about it: none was asked for, a refusal is said in full on a line of
 * its own, and someone who joined is already said to have opened the
 * workspace. The first invitation is automatic, so there is no first "Invite"
 * control. */
function renderInvitationStatus(state: InvitationStatusIndicator): m.Children {
  switch (state.kind) {
    case "inviting":
      return m(
        "span",
        { "data-slot": "invitation", class: "flex items-center gap-1.5" },
        [m(Spinner, { size: "sm", extra: "shrink-0" }), "Inviting"],
      );
    case "invited":
      return m(
        "span",
        {
          "data-slot": "invitation",
          ...(state.at === null
            ? {}
            : { "data-tooltip": `Invited ${localeTime(state.at)}` }),
        },
        state.at === null
          ? "Invited"
          : `Invited ${formatRelativeAgo(state.at, Date.now())}`,
      );
    case "could_not_invite":
      return m(
        "span",
        { "data-slot": "invitation", class: "text-warning" },
        "Could not invite",
      );
    case "none":
    case "refused":
    case "joined":
      return null;
  }
}

function isReinviteOffered(state: InvitationStatusIndicator): boolean {
  return (
    (state.kind === "invited" ||
      state.kind === "could_not_invite" ||
      state.kind === "refused") &&
    state.canReinvite
  );
}

/** A refusal is written as a sentence, so it gets a line of its own, where it
 * wraps rather than being cut short. */
function renderInvitationRefusal(
  grant: Grant,
  state: InvitationStatusIndicator,
): m.Children {
  if (state.kind !== "refused") return null;
  return m(
    "p",
    {
      "data-invite-refusal": grant.key,
      class: CARD_DETAIL_CLASS + " text-important",
    },
    state.message,
  );
}

function reinviteButton(share: SharePanelModel, grant: Grant): m.Children {
  return textButton(
    "Invite again",
    () => void share.invite(share.currentTarget, grant.key),
    { "data-invite": grant.key, "aria-label": "Invite again" },
  );
}

/** A control set in a card's details, as words among the other words. */
function textButton(
  label: string,
  onclick: () => void,
  attrs: Record<string, string> = {},
): m.Children {
  return m(
    "button",
    {
      type: "button",
      class: "cursor-pointer font-semibold text-primary hover:underline",
      onclick,
      ...attrs,
    },
    label,
  );
}

/** Whether the card's person has opened the workspace, with the time of their
 * latest visit on hover once they have. */
function renderWorkspaceAccess(access: WorkspaceAccess): m.Children {
  const text = workspaceAccessText(access, Date.now());
  return m(
    "span",
    {
      "data-grant-access": access.kind,
      ...(access.kind === "accessed" && access.at !== null
        ? { "data-tooltip": `Accessed ${localeTime(access.at)}` }
        : {}),
    },
    text,
  );
}

function workspaceAccessText(access: WorkspaceAccess, nowMs: number): string {
  switch (access.kind) {
    case "not_a_user":
      return "Hasn't signed up for Imbue Studio yet";
    case "never":
      return "Never accessed your workspace";
    case "accessed": {
      const ago = access.at === null ? "" : formatRelativeAgo(access.at, nowMs);
      return ago === ""
        ? "Accessed your workspace"
        : `Accessed your workspace ${ago}`;
    }
  }
}

function localeTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

/** The box every row leads with, so the names line up down the list. */
function renderGrantLead(share: SharePanelModel, grant: Grant): m.Children {
  const grantee = grant.grantee;
  if (grantee.kind === "email_domain")
    return m(
      "span",
      {
        class:
          ROW_LEAD_CLASS +
          " border border-dashed border-strong text-tertiary type-helper",
      },
      "@",
    );
  if (grantee.kind === "email")
    return m(
      "span",
      {
        class:
          ROW_LEAD_CLASS + " border border-dashed border-strong text-tertiary",
      },
      m(Icon16, { name: "user-plus", size: "sm" }),
    );
  const record = share.identityFor(grantee.userId);
  const pictureUrl = record?.profile_picture_url ?? null;
  if (pictureUrl !== null)
    return m(
      "span",
      { class: ROW_LEAD_CLASS + " bg-fill-active overflow-hidden" },
      m("img", { src: pictureUrl, alt: "", class: "h-6 w-6 object-cover" }),
    );
  return m(
    "span",
    { class: ROW_LEAD_CLASS + " bg-fill-active text-secondary type-helper" },
    monogram(grantText(share, grant).primary),
  );
}

function grantText(
  share: SharePanelModel,
  grant: Grant,
): { primary: string; secondary: string | null } {
  const grantee = grant.grantee;
  if (grantee.kind === "email_domain")
    return { primary: `Anyone at ${grantee.value}`, secondary: null };
  if (grantee.kind === "email")
    return { primary: grantee.value, secondary: null };
  const record = share.identityFor(grantee.userId);
  const email = record?.email ?? grantee.value;
  const name = record?.display_name ?? "";
  if (name !== "") return { primary: name, secondary: email };
  return { primary: email ?? grantee.userId, secondary: null };
}

function monogram(text: string): string {
  const words = text.split(/\s+/).filter((word) => word !== "");
  if (words.length === 0) return "";
  const initials =
    words.length === 1 ? words[0].slice(0, 1) : words[0][0] + words[1][0];
  return initials.toUpperCase();
}

function isJustAdded(share: SharePanelModel, grant: Grant): boolean {
  const addedAtMs = grant.addedAtMs;
  return addedAtMs !== null && share.nowMs() - addedAtMs < JUST_ADDED_MS;
}

/** Absent while publishing is off, because there is nothing to open. */
function renderLinkSection(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const target = share.currentTarget;
  // The link is shown only once it can actually be opened: a workspace still
  // being brought up has an address that answers nothing yet, and an app that
  // has not registered one has no address at all.
  const url =
    share.isAwaitingLink || share.isAwaitingLabel(target)
      ? null
      : share.targetUrl(target);
  const copied = local.copied;
  const isCopied =
    copied !== null &&
    copied.target === target &&
    share.nowMs() - copied.atMs < COPY_FLASH_MS;
  return m("section", { id: "ws-share-link", class: "mt-6 shrink-0" }, [
    m("p", { class: "type-label text-primary" }, "Link"),
    m(
      "div",
      { class: "mt-1.5 flex items-center gap-2" },
      url === null
        ? m("div", { class: LINK_BOX_CLASS }, [
            m(Spinner, { size: "sm", extra: "shrink-0" }),
            "Preparing the link",
          ])
        : [
            m(CopyField, {
              value: url,
              extra: isCopied
                ? "flex-1 min-w-0 border-success bg-[var(--c-success-surface)]"
                : "flex-1 min-w-0",
              "aria-label": "The link to this target",
            }),
            m(
              Button,
              {
                id: "ws-share-copy",
                variant: "secondary",
                size: "icon",
                "aria-label":
                  target === share.wholeService
                    ? "Copy the link"
                    : `Copy the link to ${targetTitle(share, target)}`,
                onclick: () => {
                  void copyToClipboardWithFlash(
                    share,
                    local,
                    share.currentTarget,
                    url,
                  );
                },
              },
              m(Icon16, { name: isCopied ? "check" : "copy" }),
            ),
          ],
    ),
    url === null
      ? null
      : m(
          "p",
          { class: "mt-1.5 type-helper text-tertiary" },
          "Only people granted permission can open this link.",
        ),
  ]);
}

/** The control that emails the signed-in account this workspace's link, and
 * the one line of status it leaves behind. */
function renderMobileLinkControl(share: SharePanelModel): m.Children {
  // Dimmed the way the add row dims, so a control that cannot be pressed is
  // legible as one without the reader having to hover for the tooltip.
  const offAttrs = share.canSendMobileAccessLink
    ? {}
    : {
        "aria-disabled": "true",
        "data-tooltip": MOBILE_LINK_OFF_TOOLTIP,
        extra: "opacity-40",
      };
  return m("div", { id: "ws-share-mobile-link", class: "mt-4 shrink-0" }, [
    m(
      Button,
      {
        id: "ws-share-mobile-link-btn",
        variant: "secondary",
        ...offAttrs,
        onclick: () => void share.sendMobileAccessLink(),
      },
      [m(Icon16, { name: "mail" }), MOBILE_LINK_LABEL],
    ),
    renderMobileLinkStatus(share.mobileLinkState),
  ]);
}

function renderMobileLinkStatus(state: MobileLinkState): m.Children {
  switch (state.state) {
    case "idle":
      return null;
    case "sending":
      return m(
        "p",
        { class: "mt-1.5 flex items-center gap-2 type-helper text-tertiary" },
        [m(Spinner, { size: "sm", extra: "shrink-0" }), "Sending"],
      );
    case "sent":
      return m(
        "p",
        { class: "mt-1.5 type-helper text-success" },
        `Sent to ${state.recipientEmail}`,
      );
    case "refused":
      return m(
        "p",
        { class: "mt-1.5 type-helper text-important" },
        state.message,
      );
  }
}

/** Write `text` to the clipboard and flash the check on the control keyed `target`. */
async function copyToClipboardWithFlash(
  share: SharePanelModel,
  local: ShareTabLocalState,
  target: string,
  text: string,
): Promise<void> {
  const clipboard = navigator.clipboard;
  // The text is on screen and selectable either way, so a clipboard that
  // refuses costs the confirmation and nothing else.
  if (!clipboard) return;
  try {
    await clipboard.writeText(text);
  } catch {
    return;
  }
  local.copied = { target, atMs: share.nowMs() };
  if (local.copyFlashHandle !== null) share.cancel(local.copyFlashHandle);
  local.copyFlashHandle = share.schedule(() => {
    local.copyFlashHandle = null;
    m.redraw();
  }, COPY_FLASH_MS);
  m.redraw();
}

// Visiting agents.

/** The section inviting outside coding agents into the target, and listing those already in. */
function renderVisitingAgentsSection(
  share: SharePanelModel,
  local: ShareTabLocalState,
  target: string,
): m.Children {
  const isWhole = target === share.wholeService;
  const inviteState = share.visitingAgentInviteState;
  return m(
    "section",
    {
      id: "ws-share-agents",
      class: "mt-6 shrink-0",
      oncreate: () => void share.ensureVisitingAgentsLoaded(),
    },
    [
      m("p", { class: "type-label text-primary" }, VISITING_AGENTS_HEADING),
      m(
        "p",
        { class: "mt-1 type-helper text-tertiary" },
        isWhole
          ? VISITING_AGENTS_WORKSPACE_SENTENCE
          : VISITING_AGENTS_APP_SENTENCE,
      ),
      m("div", { class: "mt-3 flex items-center gap-2" }, [
        m(
          Button,
          {
            id: "ws-share-agents-invite",
            variant: "secondary",
            "aria-disabled":
              inviteState.state === "minting" ? "true" : undefined,
            onclick: () => void share.inviteVisitingAgent(target),
          },
          [
            inviteState.state === "minting"
              ? m(Spinner, { size: "sm", extra: "shrink-0" })
              : m(Icon16, { name: "user-plus" }),
            VISITING_AGENT_INVITE_LABEL,
          ],
        ),
        inviteState.state === "refused" && inviteState.target === target
          ? m(
              "p",
              {
                id: "ws-share-agents-invite-error",
                class: "type-helper text-important",
              },
              inviteState.message,
            )
          : null,
      ]),
      inviteState.state === "minted" && inviteState.target === target
        ? renderVisitingAgentInstructions(share, local, inviteState)
        : null,
      renderVisitingAgentList(share, isWhole),
    ],
  );
}

function renderVisitingAgentInstructions(
  share: SharePanelModel,
  local: ShareTabLocalState,
  inviteState: Extract<VisitingAgentInviteState, { state: "minted" }>,
): m.Children {
  const { invite } = inviteState;
  const copied = local.copied;
  const isCopied =
    copied !== null &&
    copied.target === `agent:${invite.code}` &&
    share.nowMs() - copied.atMs < COPY_FLASH_MS;
  return m("div", { id: "ws-share-agents-instructions", class: "mt-3" }, [
    m(
      "p",
      { class: "type-helper text-secondary" },
      VISITING_AGENT_INSTRUCTIONS_LEAD,
    ),
    m("textarea", {
      id: "ws-share-agents-instructions-text",
      class:
        "mt-2 w-full h-48 rounded-md border border-default bg-fill-subtle p-2 " +
        "font-mono type-helper text-primary",
      readonly: true,
      "aria-label": "Instructions for the agent",
      value: invite.instructions,
    }),
    m("div", { class: "mt-2 flex items-center gap-2" }, [
      m(
        Button,
        {
          id: "ws-share-agents-copy",
          variant: "primary",
          onclick: () => {
            void copyToClipboardWithFlash(
              share,
              local,
              `agent:${invite.code}`,
              invite.instructions,
            );
          },
        },
        [
          m(Icon16, { name: isCopied ? "check" : "copy" }),
          VISITING_AGENT_COPY_LABEL,
        ],
      ),
      m(
        Button,
        {
          id: "ws-share-agents-done",
          variant: "secondary",
          onclick: () => share.dismissVisitingAgentInvite(),
        },
        VISITING_AGENT_DONE_LABEL,
      ),
      m(
        "span",
        { class: "type-helper text-tertiary" },
        `Code ${invite.code}, expires ${localeTime(invite.expires_at)}`,
      ),
    ]),
  ]);
}

function renderVisitingAgentList(
  share: SharePanelModel,
  isWhole: boolean,
): m.Children {
  const loadState = share.visitingAgentsLoadState;
  if (loadState === "idle" || loadState === "loading") {
    return m(
      "p",
      {
        id: "ws-share-agents-loading",
        class: "mt-3 flex items-center gap-2 type-helper text-tertiary",
      },
      [
        m(Spinner, { size: "sm", extra: "shrink-0" }),
        "Reading the visiting agents",
      ],
    );
  }
  if (loadState === "failed") {
    // The read runs in the workspace, so a stopped one fails it; the control is
    // how the list comes back once the workspace runs again.
    return m(
      "div",
      {
        id: "ws-share-agents-error",
        class: "mt-3 flex items-center gap-2",
      },
      [
        m(
          "p",
          { class: "min-w-0 type-helper text-important" },
          share.visitingAgentsLoadError ?? "",
        ),
        m(
          Button,
          {
            id: "ws-share-agents-retry",
            variant: "secondary",
            onclick: () => void share.reloadVisitingAgents(),
          },
          VISITING_AGENTS_RETRY_LABEL,
        ),
      ],
    );
  }
  // The whole-workspace pane lists every agent; an app pane only those scoped to that app.
  const agents = share.visitingAgentList.filter(
    (agent) =>
      isWhole ||
      (Array.isArray(agent.scope) && agent.scope.includes(share.currentTarget)),
  );
  const revokeError = share.visitingAgentRevokeErrorMessage;
  return m(
    "div",
    { id: "ws-share-agents-list", class: "mt-3 flex flex-col gap-2" },
    [
      agents.length === 0
        ? m("p", { class: "type-helper text-tertiary" }, VISITING_AGENTS_EMPTY)
        : agents.map((agent) => renderVisitingAgentRow(share, agent)),
      revokeError === null
        ? null
        : m(
            "p",
            {
              id: "ws-share-agents-revoke-error",
              class: "type-helper text-important",
            },
            revokeError,
          ),
    ],
  );
}

function visitingAgentScopeText(agent: VisitingAgentEntry): string {
  if (Array.isArray(agent.scope)) return agent.scope.join(", ");
  return agent.scope === "workspace" ? "whole workspace" : agent.scope;
}

function visitingAgentGranterText(agent: VisitingAgentEntry): string {
  return agent.granter.startsWith("agent:")
    ? "invited by another agent"
    : "invited by a person";
}

function renderVisitingAgentRow(
  share: SharePanelModel,
  agent: VisitingAgentEntry,
): m.Children {
  const isRevoking = share.isRevokingVisitingAgent(agent.agent_id);
  const lastSeen =
    agent.last_seen_at === null
      ? "never connected"
      : `seen ${formatRelativeAgo(agent.last_seen_at, share.nowMs())}`;
  return m(
    "div",
    {
      class: `${ROW_CLASS} ${ROW_PERSON_CLASS}`,
      "data-agent-id": agent.agent_id,
    },
    [
      m(
        "span",
        {
          class: `${ROW_LEAD_CLASS} bg-fill-subtle type-helper font-semibold text-secondary`,
        },
        monogram(agent.name),
      ),
      m(
        "span",
        { class: "min-w-0 flex-1 truncate type-body text-primary" },
        agent.name,
      ),
      m("span", { class: COUNT_BADGE_CLASS }, visitingAgentScopeText(agent)),
      m(
        "span",
        { class: "shrink-0 type-helper text-tertiary" },
        `${visitingAgentGranterText(agent)}, ${lastSeen}`,
      ),
      m(
        Button,
        {
          variant: "secondary",
          "aria-label": `Revoke ${agent.name}`,
          "aria-disabled": isRevoking ? "true" : undefined,
          onclick: () => void share.revokeVisitingAgent(agent.agent_id),
        },
        isRevoking
          ? m(Spinner, { size: "sm", extra: "shrink-0" })
          : VISITING_AGENT_REVOKE_LABEL,
      ),
    ],
  );
}
