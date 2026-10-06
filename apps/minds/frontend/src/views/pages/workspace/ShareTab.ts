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
import { Spinner } from "../../components/Spinner";
import type {
  Grant,
  GrantAddKind,
  SharePanelModel,
} from "../../../models/sharePanel";
import { GRANT_ADD_KINDS, toGrantAddKind } from "../../../models/sharePanel";
import { navEntryClass, splitPane } from "../../components/SplitPane";

const JUST_ADDED_MS = 6000;

const COPY_FLASH_MS = 1200;

const SHARING_SENTENCE =
  "Sharing gives this workspace an address on the internet. " +
  "Only people granted access can open it.";

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

// Every row is the same height whatever it holds, so a long list scans as a
// single column.
const ROW_CLASS =
  "flex h-10 flex-none items-center gap-2 rounded-md border px-3";

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

// The word beside the switch keeps the width of its wider value in every
// state, so the switch stands still when the word comes or goes.
const PUBLISH_WORD_CLASS =
  "type-body text-secondary after:invisible after:block after:h-0 " +
  "after:content-['Yes']";

export interface ShareTabAttrs {
  share: SharePanelModel;
  workspaceName: string;
}

interface ShareTabLocalState {
  isConfirmOpen: boolean;
  /** Whether the app pane's inherited-permissions group is expanded. */
  isInheritedOpen: boolean;
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
          nav: renderTargetNav(share),
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
        m("h2", { class: "type-heading text-primary" }, "Enable sharing"),
        m("span", { class: "flex shrink-0 items-center gap-2" }, [
          share.isPublicationKnown
            ? renderPublishSwitch(share, local)
            : renderPublishPlaceholder(share),
          m(
            "span",
            { class: PUBLISH_WORD_CLASS },
            share.isPublicationKnown ? (share.isPublished ? "Yes" : "No") : "",
          ),
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
            publishWrite.message,
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
  return m("button", {
    id: "ws-share-publish-switch",
    type: "button",
    role: "switch",
    "aria-checked": share.isPublished ? "true" : "false",
    "aria-label": "Enable sharing",
    ...(isSettling ? { "aria-busy": "true" } : {}),
    class: isSettling
      ? "perm-switch shrink-0 is-settling"
      : "perm-switch shrink-0",
    onclick: () => {
      if (share.isPublished) void share.unpublish();
      else local.isConfirmOpen = true;
    },
  });
}

/** What stands where the switch will, while nothing says which way it should
 * point: a switch drawn off before the answer would be read as the answer. The
 * wait spins at the off side with no knob under it; a read that failed with
 * nothing to show leaves the bare track greyed out. */
function renderPublishPlaceholder(share: SharePanelModel): m.Children {
  const isChecking = share.isCheckingPublication;
  return m("span", {
    id: "ws-share-publish-unknown",
    role: "status",
    "aria-label": isChecking
      ? "Checking whether sharing is enabled"
      : "Sharing status unknown",
    ...(isChecking ? { "aria-busy": "true" } : {}),
    class: isChecking
      ? "perm-switch shrink-0 is-checking"
      : "perm-switch shrink-0 is-unknown",
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

function renderTargetNav(share: SharePanelModel): m.Children {
  return [
    renderNavEntry(
      share,
      share.wholeService,
      "Whole workspace",
      m(Icon16, { name: "panels-top-left", extra: "shrink-0" }),
    ),
    ...share.appTargets.map((target) =>
      renderNavEntry(share, target, target, renderAppIcon(share, target)),
    ),
  ];
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
      m("span", { class: "grow truncate" }, label),
      // An app registers its address when it starts, so one that has never run
      // has no link yet -- which is not the same as having no grants.
      target === share.wholeService || share.isLabelKnown(target)
        ? null
        : m(
            "span",
            { class: "shrink-0 type-helper text-tertiary" },
            "no link yet",
          ),
      m(
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
    m.trust(shareTargetIconMarkup(share.targetIcon(target), target, 16)),
  );
}

function renderTargetPane(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  const target = share.currentTarget;
  const isWhole = target === share.wholeService;
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
          : `Permissions for ${target}`,
      ),
    ]),
    m(
      "p",
      { class: "mt-1 shrink-0 type-helper text-tertiary" },
      isWhole
        ? WHOLE_SCOPE_LINE
        : `Permissions below apply only to the ${target} app.`,
    ),
    share.isPublished ? renderLinkSection(share, local) : null,
    renderAddRow(share),
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
  ];
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

/** The target's own grant list: the one part of the pane that scrolls.
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
      class:
        "mt-4 flex flex-1 min-h-0 flex-col gap-1.5 overflow-y-auto " +
        "[scrollbar-gutter:stable]",
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
 * One grant, as a row.
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
  return m(
    "div",
    {
      key: grant.key,
      "data-grant-row": grant.key,
      class:
        ROW_CLASS +
        " " +
        rowSurfaceClass(share, isDomain) +
        (isJustAdded(share, grant) ? " grant-row-added" : ""),
      // Nobody is told about a domain grant; the row admits people the granter
      // has never named.
      ...(isDomain
        ? {
            "data-tooltip":
              `People at ${grantee.value} will not hear about this ` +
              "unless you send them the link yourself",
          }
        : {}),
    },
    [
      renderGrantLead(share, grant),
      m(
        "span",
        {
          "data-grant-name": grant.key,
          class: "type-body text-primary whitespace-nowrap",
        },
        text.primary,
      ),
      text.secondary === null
        ? null
        : m(
            "span",
            { class: "type-body text-tertiary whitespace-nowrap" },
            text.secondary,
          ),
      m("span", { class: "grow" }),
      // Reserved for the invitation work: an outcome per row, and the control
      // that starts one. Both keep their height so filling them later does not
      // move the row.
      m("span", {
        "data-slot": "status",
        class: "flex h-6 shrink-0 items-center",
      }),
      m("span", {
        "data-slot": "action",
        class: "flex h-6 shrink-0 items-center",
      }),
      renderGrantState(share, grant),
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
              onclick: () => share.removeGrant(share.currentTarget, grant.key),
            },
            m(Icon16, { name: "close" }),
          )
        : // The row keeps the width the control would have taken, so an
          // inherited row lines up with the pane's own rows below it.
          m("span", { class: "h-6 w-6 shrink-0" }),
    ],
  );
}

/** A row's surface: its kind's while sharing is on, and one flat grey for every
 * kind while it is off. */
function rowSurfaceClass(share: SharePanelModel, isDomain: boolean): string {
  if (!share.isPublished) return ROW_INACTIVE_CLASS;
  return isDomain ? ROW_DOMAIN_CLASS : ROW_PERSON_CLASS;
}

function renderGrantState(share: SharePanelModel, grant: Grant): m.Children {
  const status = grant.status;
  if (status.state === "saving")
    return m(
      "span",
      {
        // The address beside this never yields: it cannot shrink below its own
        // text, so something has to give when a long one leaves no room. This
        // does, down to the spinner alone, rather than pushing the address out
        // of the row.
        class:
          "flex min-w-0 items-center gap-1.5 overflow-hidden " +
          "type-helper text-tertiary",
      },
      [
        m(Spinner, { size: "sm", extra: "shrink-0" }),
        m("span", { class: "min-w-0 truncate" }, SAVING_LABEL),
      ],
    );
  if (status.state !== "failed") return null;
  return m("span", { class: "flex min-w-0 items-center gap-1.5" }, [
    m(
      "span",
      {
        class: "min-w-0 truncate type-helper text-important",
        "data-tooltip": status.failureMessage,
      },
      status.failureMessage,
    ),
    m(
      Button,
      {
        variant: "ghost",
        size: "icon",
        onclick: () => share.retryGrant(share.currentTarget, grant.key),
      },
      "Retry",
    ),
  ]);
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
    return {
      primary: grantee.value,
      // Only a row the workspace has taken back can say this: until the account
      // lookup has answered, nobody knows whether they have signed up.
      secondary:
        grant.status.state === "settled" ? "(hasn't signed up yet)" : null,
    };
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
                    : `Copy the link to ${target}`,
                onclick: () => {
                  void copyLink(share, local, url);
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

async function copyLink(
  share: SharePanelModel,
  local: ShareTabLocalState,
  url: string,
): Promise<void> {
  const clipboard = navigator.clipboard;
  // The link is on screen and selectable either way, so a clipboard that
  // refuses costs the confirmation and nothing else.
  if (!clipboard) return;
  try {
    await clipboard.writeText(url);
  } catch {
    return;
  }
  local.copied = { target: share.currentTarget, atMs: share.nowMs() };
  if (local.copyFlashHandle !== null) share.cancel(local.copyFlashHandle);
  local.copyFlashHandle = share.schedule(() => {
    local.copyFlashHandle = null;
    m.redraw();
  }, COPY_FLASH_MS);
  m.redraw();
}
