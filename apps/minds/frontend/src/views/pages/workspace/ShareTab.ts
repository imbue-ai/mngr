// Sharing: one page per target (the whole workspace, or one app), each showing
// the link it opens, the people and agents it admits, and what they reach
// through it. One switch, on the whole workspace's page, publishes the
// workspace. Settings' Workspace & apps tab lists the targets in its rail
// (sharingNav) and draws the selected one's page here. Nothing here waits on a
// write: every control stays live while the model saves behind it.

import m from "mithril";
import { getAppContext } from "../../../app-context";
import { AskAgentModel } from "../../../models/assistChat";
import { recoveryRoute } from "../../../models/create";
import type { ErrorAction, UserFacingError } from "../../../models/errors";
import { fallbackErrorSentence, plainErrorText } from "../../../models/errors";
import { showErrorDialog } from "../../../models/errorDialog";
import type { ShareFailure, ShareRemedy } from "../../../models/shareFailures";
import {
  LINK_NOT_ANSWERING_ASSIST_DESCRIPTION,
  UNREADABLE_SHARING_LIST_ASSIST_DESCRIPTION,
} from "../../../models/shareFailures";
import { webLogin } from "../../../models/webLogin";
import { shareTargetIconMarkup } from "../../components/appIcon";
import { Button } from "../../components/Button";
import { CoachBubble } from "../../components/CoachBubble";
import { Collapsible } from "../../components/Collapsible";
import { Select, TextInput } from "../../components/FormControls";
import { Icon16 } from "../../components/Icon";
import { ErrorNotice } from "../../components/ErrorDisplay";
import {
  errorMark,
  InlineError,
  markedLine,
  richErrorText,
} from "../../components/errorDesign";
import { remedyActions } from "../../components/errorRemedies";
import {
  AskAgentFailure,
  askAgentActions,
} from "../../components/AskAgentButton";
import { CopyField } from "../../components/Layout";
import { Modal } from "../../components/Modal";
import { Notice } from "../../components/Notice";
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
import { RESEND_EMAIL_LABEL } from "../../../models/emailVerification";
import type { ResendOutcome } from "../../../models/emailVerification";
import { monogramLetters } from "../../../models/monogram";
import { navEntryClass } from "../../components/SplitPane";
import { QR_CODE_ROW_MIN_HEIGHT_CLASS, QrCode } from "../../components/QrCode";
import { serviceMark } from "../../components/ServiceMark";
import type { UiPermissionConnection } from "../../../generated/ui";
import { connectionSectionId } from "../../../models/workspacePermissions";
import { workspaceSwatch } from "../../shell/trail";
import { ownerSectionQuery } from "../settings/settings-route";
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

const WHOLE_SCOPE_LINE =
  "People you invite here can open every app in this workspace, and its " +
  "files, agent chats and terminal.";

const SHARING_OFF_LINE = "Sharing is off for this workspace.";
const NO_LINK_YET_TOOLTIP = "No link yet: this app gets one when it first starts.";
const TURN_ON_SHARING_LABEL = "Turn on sharing…";
const LINK_OFF_LINE = "The link appears here when sharing is on.";
const SECURE_LINK_LINE = "This is a secure link: only people you've granted access can open it.";
const WORKSPACE_CONNECTIONS_HEADING = "Invited users will have access to your connections";
const APP_CONNECTIONS_HEADING = "Invited users will have access to any of these connections that are used in the app:";
const APPS_HEADING = "Invited users will have access to these apps";

const SAVING_LABEL = "Securely granting access";

const ADD_OFF_TOOLTIP = "Permissions cannot be granted while sharing is off";

const ADD_UNKNOWN_TOOLTIP =
  "Permissions cannot be granted until the sharing status has loaded";

const STATUS_UNCHECKED_TOOLTIP = "Sharing status couldn't be checked.";
const STATUS_CHECKING_LINE = "Checking whether sharing is enabled";

const LIST_UNREAD_LINE = "The list of who has access couldn't be read.";

// Drawn on every target's page, whether or not sharing is on, so a reader
// learns the feature exists and what switch turns it on. The message carries
// the link of the target the page is on.
const MOBILE_LINK_LABEL = "Email me a Mobile Access Link";

const VISITING_AGENTS_HEADING = "Invite an agent";
const VISITING_AGENTS_WORKSPACE_SENTENCE =
  "Invite a coding agent running somewhere else (on your computer, in a cloud " +
  "agent product, on a collaborator's machine) to work in this workspace with " +
  "exactly this workspace's access, and nothing more.";
const VISITING_AGENTS_APP_SENTENCE =
  "Invite a coding agent running somewhere else to use only this app.";
const VISITING_AGENT_INVITE_LABEL = "Create invite";
const VISITING_AGENT_COPY_LABEL = "Copy the instructions";
const VISITING_AGENT_DONE_LABEL = "Done";
const VISITING_AGENT_REVOKE_LABEL = "Revoke";
const VISITING_AGENTS_EMPTY = "No agents have been invited.";
const VISITING_AGENT_INSTRUCTIONS_LEAD =
  "Paste these instructions to the agent. The code works once and expires in fifteen minutes.";

const MOBILE_LINK_OFF_TOOLTIP =
  "Enable sharing and web access to access this link on mobile";
const MOBILE_LINK_NO_APP_LINK_TOOLTIP =
  "This app gets its link the first time it starts.";

const MOBILE_LINK_COACH_TEXT = "Use this to open Studio on your phone.";

const SYNC_PENDING_NOTICE =
  "The permissions have not reached Imbue Cloud yet, so nobody can be " +
  "invited. They will be sent again in a moment.";

/** What each kind of grant is called at the add row, and the example it shows. */
const ADD_KIND_TEXT: Record<
  GrantAddKind,
  { readonly label: string; readonly placeholder: string }
> = {
  email: { label: "One person by email", placeholder: "name@example.com" },
  email_domain: { label: "Everyone at a domain", placeholder: "example.com" },
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
  /** The workspace's colour, for the whole workspace's title. */
  accent: string | null;
  /** The workspace's connections, which everyone invited reaches through what is shared; null while they are not
   * known (still loading, or unavailable), which leaves the reminder out. */
  connections: readonly UiPermissionConnection[] | null;
  /** Open a connection's own page (Permissions). */
  onOpenConnection: (sectionId: string) => void;
}

interface ShareTabLocalState {
  isConfirmOpen: boolean;
  /** Each "Ask the agent to fix it" button's state, by the id of the notice
   * it sits on, made on first use. */
  askAgentByNotice: Map<string, AskAgentModel>;
  /** Whether the app page's inherited-permissions group is expanded. */
  isInheritedOpen: boolean;
  /** The target whose list of who has access is unfolded; null while every one is folded, as each starts. */
  accessOpenTarget: string | null;
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

/** The rail entries of Sharing: the whole workspace, each app, then the system apps under a label of their own. */
export function sharingNav(share: SharePanelModel): m.Children {
  return [
    renderNavEntry(
      share,
      share.wholeService,
      "Full workspace",
      m(Icon16, { name: "panels-top-left", extra: "shrink-0" }),
    ),
    ...share.appTargets.map((target) => renderAppNavEntry(share, target)),
    ...(share.systemTargets.length === 0
      ? []
      : [
          m("div", { class: "px-2 pt-2 pb-0.5 type-helper text-tertiary" }, "System apps"),
          ...share.systemTargets.map((target) => renderAppNavEntry(share, target)),
        ]),
  ];
}

/** The selected target's Sharing page. */
export function ShareTab(): m.Component<ShareTabAttrs> {
  const local: ShareTabLocalState = {
    isConfirmOpen: false,
    askAgentByNotice: new Map(),
    isInheritedOpen: false,
    accessOpenTarget: null,
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
      return m("div", { class: "flex flex-1 min-h-0 flex-col" }, [
        renderTargetPage(share, local, vnode.attrs),
        renderConfirmDialog(share, local),
      ]);
    },
  };
}

/** What the page says and offers while sharing is off: the one line, and the way to turn it on from here. */
function renderOffBanner(share: SharePanelModel, local: ShareTabLocalState): m.Children {
  if (!share.isPublicationKnown || share.isPublished) return null;
  return m(
    "div",
    { id: "ws-share-off-notice", class: "mb-6 shrink-0" },
    m(Notice, { variant: "warn" }, [
      m("p", { class: "m-0" }, SHARING_OFF_LINE),
      m(
        Button,
        {
          id: "ws-share-turn-on",
          variant: "secondary",
          extra: "mt-2",
          onclick: () => {
            local.isConfirmOpen = true;
          },
        },
        TURN_ON_SHARING_LABEL,
      ),
    ]),
  );
}

/** What sharing is doing or could not do, above the page: the wait for a link, a failed publish or read, and the
 * note that the address moved. */
function renderShareNotices(share: SharePanelModel, local: ShareTabLocalState): m.Children {
  const publishWrite = share.publishWrite;
  const publishFailure =
    publishWrite.state === "failed" ? publishWrite.failure : null;
  // A workspace that can't be reached fails both the publish and the read
  // behind it, for one reason: the publish failure says so alone.
  const loadFailure =
    publishFailure?.isWorkspaceUnreachable === true ? null : share.loadFailure;
  const notices: m.Children[] = [
    share.isLinkBeingSetUp ? renderProvisioning(share, local) : null,
    publishFailure === null
      ? null
      : renderShareFailure(share, local, "ws-share-publish-error", publishFailure),
    loadFailure === null
      ? null
      : renderShareFailure(share, local, "ws-share-load-error", loadFailure),
    share.migratedDomainFrom === null
      ? null
      : m(
          Notice,
          { variant: "info" },
          "Sharing moved to a new address. Links you shared before no longer work.",
        ),
  ].filter((notice) => notice !== null);
  return notices.length === 0 ? null : m("div", { class: "mb-6 flex shrink-0 flex-col gap-2" }, notices);
}

/** The switch that publishes the workspace: last on the whole workspace's page, and the one place sharing is
 * turned off. */
function renderPublishWidget(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  return m(
    "section",
    { id: "ws-share-publish", class: "shrink-0 flex flex-col gap-2" },
    [
      m("div", { class: "flex items-center justify-between gap-4" }, [
        m("h2", { class: "type-label text-primary" }, ENABLE_SHARING_LABEL),
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
      m("p", { class: "type-helper text-tertiary" }, SHARING_SENTENCE),
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
    label: isChecking ? STATUS_CHECKING_LINE : "Sharing status unknown",
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

function renderProvisioning(
  share: SharePanelModel,
  local: ShareTabLocalState,
): m.Children {
  if (share.isLinkDeadlinePassed)
    return m(
      "div",
      { id: "ws-share-provisioning", class: "flex flex-col gap-2" },
      [
        m(ErrorNotice, {
          id: "ws-share-link-not-answering",
          error: { message: "This link still isn't answering.", details: "" },
          actions: [
            republishAction(share, "Try again"),
            ...shareAskAgentActions(
              share,
              local,
              "ws-share-link-not-answering",
              LINK_NOT_ANSWERING_ASSIST_DESCRIPTION,
            ),
          ],
          isBugReportOffered: true,
          workspaceAgentId: share.workspaceAgentId,
          footer: askAgentFailure(share, local, "ws-share-link-not-answering"),
        }),
      ],
    );
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
  const gatewayError = share.gatewayError ?? "";
  if (share.isProvisioningHalted) {
    return m(ErrorNotice, {
      id: "ws-share-gateway-trouble",
      error: {
        message: fallbackErrorSentence(
          "This workspace couldn't finish setting up its link",
        ),
        details: gatewayError,
      },
      actions: [republishAction(share, "Try again")],
      isBugReportOffered: true,
    });
  }
  if (share.gatewayState !== "retrying" || share.gatewayFailedAttemptCount < 1)
    return null;
  const nextRetry = share.gatewayNextRetryAt
    ? new Date(share.gatewayNextRetryAt).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      })
    : null;
  // It retries on its own, so there is nothing to press and nothing to report.
  return m(ErrorNotice, {
    id: "ws-share-gateway-trouble",
    error: {
      message: nextRetry
        ? `Setting up the link hit a problem. The workspace tries again at ${nextRetry}.`
        : "Setting up the link hit a problem. The workspace tries again shortly.",
      details: gatewayError,
    },
    tone: "warn",
    isBugReportOffered: false,
  });
}

/** Turn sharing off and on again, which brings a link up afresh. */
function republishAction(share: SharePanelModel, label: string): ErrorAction {
  return { label, onclick: () => void share.republish() };
}

/** The buttons a share failure offers, in the order it names them. */
function shareRemedyActions(
  share: SharePanelModel,
  local: ShareTabLocalState,
  noticeId: string,
  remedies: readonly ShareRemedy[],
  remedyPath = "",
  onAskAgentFailed?: (
    error: UserFacingError,
    isBugReportOffered: boolean,
  ) => void,
): ErrorAction[] {
  const agentId = share.workspaceAgentId;
  const actions: ErrorAction[] = [];
  for (const remedy of remedies) {
    switch (remedy) {
      case "retry":
        actions.push({
          label: "Try again",
          onclick: () => void share.load(),
        });
        break;
      case "retry_publication":
        actions.push({
          label: "Try again",
          onclick: () => void share.retryPublication(),
        });
        break;
      case "reload_visiting_agents":
        actions.push({
          label: "Try again",
          onclick: () => void share.reloadVisitingAgents(),
        });
        break;
      case "start_workspace":
        actions.push({
          label: "Start workspace",
          // Recovery starts it and comes back here.
          onclick: () =>
            m.route.set(recoveryRoute(agentId, m.route.get() ?? "", "start")),
        });
        break;
      case "update_workspace":
        actions.push({
          label: "Update workspace",
          onclick: () => getAppContext().shell.openUpdatesTab(agentId),
        });
        break;
      case "sign_in":
        actions.push({
          label: "Sign in",
          onclick: () => void webLogin.start(),
        });
        break;
      case "open_account_settings":
        actions.push({
          label: "Open account settings",
          onclick: () => getAppContext().shell.openSettings(ownerSectionQuery(agentId)),
        });
        break;
      case "restart_sharing":
        actions.push(republishAction(share, "Restart sharing"));
        break;
      case "review_shared_workspaces":
        if (remedyPath !== "")
          actions.push({
            label: "Review shared workspaces",
            onclick: () => m.route.set(remedyPath),
          });
        break;
      case "RESTART_APP":
      case "UPDATE_APP":
      case "ASK_AGENT":
        actions.push(
          ...remedyActions(remedy, {
            askAgent: () =>
              shareAskAgentActions(
                share,
                local,
                noticeId,
                UNREADABLE_SHARING_LIST_ASSIST_DESCRIPTION,
                onAskAgentFailed,
              ),
          }),
        );
        break;
    }
  }
  return actions;
}

/** "Ask the agent to fix it" on the notice `noticeId`, keeping its state per
 * notice. `onFailed` shows a failed start where no notice draws it (a dialog
 * that closed as the button was pressed); a notice draws it in its footer
 * (askAgentFailure). */
function shareAskAgentActions(
  share: SharePanelModel,
  local: ShareTabLocalState,
  noticeId: string,
  description: string,
  onFailed?: (error: UserFacingError, isBugReportOffered: boolean) => void,
): ErrorAction[] {
  const model = askAgentModel(local, noticeId);
  const workspaceAgentId = share.workspaceAgentId;
  const actions = askAgentActions({
    id: askAgentButtonId(noticeId),
    model,
    workspaceAgentId,
    description,
    label: "Ask the agent to fix it",
  });
  if (onFailed === undefined) return actions;
  return actions.map((action) => ({
    ...action,
    onclick: () =>
      void model.start({ workspaceAgentId, description }).then(() => {
        if (model.error !== null)
          onFailed(model.error, model.isFailureReportable);
      }),
  }));
}

function askAgentButtonId(noticeId: string): string {
  return `${noticeId}-ask-agent`;
}

/** Why the agent asked for on the notice `noticeId` could not be started. */
function askAgentFailure(
  share: SharePanelModel,
  local: ShareTabLocalState,
  noticeId: string,
): m.Children {
  return m(AskAgentFailure, {
    id: askAgentButtonId(noticeId),
    model: askAgentModel(local, noticeId),
    workspaceAgentId: share.workspaceAgentId,
    isBoxed: false,
  });
}

function askAgentModel(
  local: ShareTabLocalState,
  noticeId: string,
): AskAgentModel {
  let model = local.askAgentByNotice.get(noticeId);
  if (model === undefined) {
    model = new AskAgentModel({
      onStarted: (agentId) => getAppContext().shell.enterWorkspace(agentId),
    });
    local.askAgentByNotice.set(noticeId, model);
  }
  return model;
}

/** A share failure in place: our sentence, the buttons that fit it, Report a
 * bug when it asks for one, and the route's account behind See details. */
function renderShareFailure(
  share: SharePanelModel,
  local: ShareTabLocalState,
  id: string,
  failure: ShareFailure,
): m.Children {
  return m("div", { class: "flex flex-col" }, [
    m(ErrorNotice, {
      id,
      error: failure.error,
      actions: shareRemedyActions(
        share,
        local,
        id,
        failure.remedies,
        failure.remedyPath,
      ),
      isBugReportOffered: failure.isBugReportOffered,
      workspaceAgentId: share.workspaceAgentId,
      footer: failure.remedies.includes("ASK_AGENT")
        ? askAgentFailure(share, local, id)
        : null,
    }),
  ]);
}

/** The key a card's error dialog's buttons keep their state under. */
const SHARE_FAILURE_DIALOG_ID = "ws-share-failure-dialog";

/** A failure shown in a card too small for it, opened in the error dialog. */
function openShareFailureDialog(
  share: SharePanelModel,
  local: ShareTabLocalState,
  failure: ShareFailure,
): void {
  // The dialog closes as its button is pressed, so a failed agent start
  // comes back as a dialog of its own, offering a report as AskAgentFailure
  // does.
  const showAskAgentFailure = (
    error: UserFacingError,
    isBugReportOffered: boolean,
  ): void =>
    showErrorDialog({
      error,
      isBugReportOffered,
      workspaceAgentId: share.workspaceAgentId,
    });
  showErrorDialog({
    error: failure.error,
    actions: shareRemedyActions(
      share,
      local,
      SHARE_FAILURE_DIALOG_ID,
      failure.remedies,
      failure.remedyPath,
      showAskAgentFailure,
    ),
    isBugReportOffered: failure.isBugReportOffered,
    workspaceAgentId: share.workspaceAgentId,
  });
}

function renderAppNavEntry(share: SharePanelModel, target: string): m.Children {
  return renderNavEntry(
    share,
    target,
    share.targetDisplayName(target),
    renderAppIcon(share, target),
  );
}

function renderNavEntry(
  share: SharePanelModel,
  target: string,
  label: string,
  icon: m.Children,
): m.Children {
  const isSelected = target === share.currentTarget;
  // An app registers its address when it starts, so one that has never run
  // has no link yet -- which is not the same as having no grants. Both this
  // and the count come from the document, so until one lands the row says
  // neither: an unregistered label and an empty list look exactly like a
  // document that has not arrived.
  const isLinkMissing =
    !share.isSharingDocumentLoading && target !== share.wholeService && !share.isLabelKnown(target);
  const count = share.isSharingDocumentLoading ? 0 : share.grantCount(target);
  return m(
    "button",
    {
      type: "button",
      "data-share-target": target,
      "aria-pressed": isSelected ? "true" : "false",
      class: navEntryClass(isSelected),
      // The rail is narrow, so what an entry has to say beyond its name is a tooltip rather than more text.
      ...(isLinkMissing ? { "data-share-no-link": "true", "data-tooltip": NO_LINK_YET_TOOLTIP } : {}),
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
      // How many people the target admits, once there are any.
      share.isSharingDocumentLoading
        ? m(Skeleton, { extra: "h-5 w-5 shrink-0" })
        : count > 0
          ? m("span", { "data-share-count": target, class: COUNT_BADGE_CLASS }, String(count))
          : null,
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

function pageDivider(): m.Children {
  return m("hr", { class: "my-6 shrink-0 border-t border-subtle" });
}

function renderTargetPage(
  share: SharePanelModel,
  local: ShareTabLocalState,
  attrs: ShareTabAttrs,
): m.Children {
  const target = share.currentTarget;
  const isWhole = target === share.wholeService;
  const name = isWhole ? attrs.workspaceName : targetTitle(share, target);
  const connections = renderConnections(attrs.connections, isWhole, attrs.onOpenConnection);
  return [
    renderOffBanner(share, local),
    m("div", { class: "shrink-0 flex items-center gap-3" }, [
      isWhole
        ? workspaceSwatch(attrs.accent, { size: "md" })
        : m("span", { class: "shrink-0 inline-flex" }, m.trust(shareTargetIconMarkup(share.targetIcon(target), share.targetDisplayName(target), 24))),
      m(
        "h2#ws-share-title",
        { class: "type-heading-lg text-primary truncate" },
        isWhole ? `Share ${name}` : `Share the ${name} app`,
      ),
    ]),
    m("div", { class: "mt-6 shrink-0" }, [renderShareNotices(share, local), renderLinkSection(share, local, name, isWhole)]),
    pageDivider(),
    m("section", { id: "ws-share-invite", class: "shrink-0" }, [
      m("h2", { class: "type-label text-primary" }, "Invite others"),
      m(
        "p",
        { class: "mt-0.5 type-helper text-tertiary" },
        isWhole ? WHOLE_SCOPE_LINE : `People you invite here can open only the ${name} app.`,
      ),
      renderAddRow(share, local),
      share.isGrantsSyncPending
        ? m(
            "div",
            { id: "ws-share-sync-notice", class: "mt-3 shrink-0" },
            m(Notice, { variant: "warn" }, SYNC_PENDING_NOTICE),
          )
        : null,
      renderAccessList(share, local, name),
    ]),
    pageDivider(),
    renderVisitingAgentsSection(share, local, target),
    connections === null ? null : [pageDivider(), connections],
    isWhole && share.perAppTargets.length > 0 ? [pageDivider(), renderAppGrid(share)] : null,
    isWhole ? [pageDivider(), renderPublishWidget(share, local)] : null,
  ];
}

/** Who has access, folded until asked for, so the sections under it stay in view. */
function renderAccessList(share: SharePanelModel, local: ShareTabLocalState, name: string): m.Children {
  const target = share.currentTarget;
  const count =
    share.grantCount(target) + (target === share.wholeService ? 0 : share.inheritedGrants().length);
  // An unread list is not an empty one, and has no count to fold behind: the list says what is wrong itself.
  if (count === 0 || !share.isGrantListKnown) return renderGrantList(share, local);
  const isOpen = local.accessOpenTarget === target;
  return m(
    Collapsible,
    {
      id: "ws-share-access",
      isOpen,
      onToggle: () => {
        local.accessOpenTarget = isOpen ? null : target;
      },
      extra: "mt-4 flex shrink-0 flex-col",
      triggerExtra: "flex w-full items-center gap-1 rounded-md py-1 text-left type-helper text-secondary hover:text-primary",
      panelExtra: "flex flex-col",
      summary: `${count} ${count === 1 ? "has" : "have"} access to “${name}”`,
    },
    renderGrantList(share, local),
  );
}

/** The workspace's connections, as a reminder of what an invitation can reach: everyone invited to the workspace
 * reaches all of them, and everyone invited to an app reaches whichever of them the app uses. Each opens its own
 * page under Permissions. Null while they are not known, or there are none. */
function renderConnections(
  connections: readonly UiPermissionConnection[] | null,
  isWhole: boolean,
  onOpenConnection: (sectionId: string) => void,
): m.Children {
  if (connections === null || connections.length === 0) return null;
  return m("section", { id: "ws-share-connections", class: "shrink-0" }, [
    m("h2", { class: "type-label text-primary" }, isWhole ? WORKSPACE_CONNECTIONS_HEADING : APP_CONNECTIONS_HEADING),
    m(
      "div",
      { class: "mt-3 grid grid-cols-5 gap-2" },
      connections.map((connection) =>
        m(
          "button",
          {
            type: "button",
            "data-share-connection": connectionSectionId(connection),
            class:
              "flex flex-col items-center gap-1.5 rounded-lg p-2 cursor-pointer bg-transparent border-0 " +
              "hover:bg-fill-hover focus-visible:outline-2 focus-visible:outline-accent",
            onclick: () => onOpenConnection(connectionSectionId(connection)),
          },
          [
            m(
              "span",
              {
                class:
                  "w-11 h-11 inline-flex items-center justify-center rounded-[14px] ring-1 ring-subtle bg-surface-primary",
              },
              serviceMark(
                connection.service_name,
                "w-6 h-6",
                connection.is_connected ? "brand" : "muted",
                m(Icon16, { name: "globe" }),
              ),
            ),
            m("span", { class: "type-helper text-primary text-center leading-tight" }, [
              connection.display_name,
              connection.show_account_label
                ? m("span", { class: "block text-tertiary" }, connection.account_label)
                : null,
            ]),
          ],
        ),
      ),
    ),
  ]);
}

/** The workspace's apps, which everyone invited to the whole workspace can open. Each opens its own Sharing page. */
function renderAppGrid(share: SharePanelModel): m.Children {
  return m("section", { id: "ws-share-apps", class: "shrink-0" }, [
    m("h2", { class: "type-label text-primary" }, APPS_HEADING),
    m(
      "div",
      { class: "mt-3 grid grid-cols-5 gap-2" },
      share.perAppTargets.map((target) =>
        m(
          "button",
          {
            type: "button",
            "data-share-app": target,
            class:
              "flex flex-col items-center gap-1.5 rounded-lg p-2 cursor-pointer bg-transparent border-0 " +
              "hover:bg-fill-hover focus-visible:outline-2 focus-visible:outline-accent",
            onclick: () => share.selectTarget(target),
          },
          [
            m(
              "span",
              { class: "inline-flex" },
              m.trust(shareTargetIconMarkup(share.targetIcon(target), share.targetDisplayName(target), 44)),
            ),
            m("span", { class: "type-helper text-primary text-center leading-tight" }, targetTitle(share, target)),
          ],
        ),
      ),
    ),
  ]);
}

/** Why the add row (and the mobile-link button) cannot be used, for its
 * tooltip. */
function unavailableTooltip(share: SharePanelModel): string {
  if (!share.isPublicationKnown)
    return share.isCheckingPublication
      ? ADD_UNKNOWN_TOOLTIP
      : STATUS_UNCHECKED_TOOLTIP;
  if (!share.isPublished) return ADD_OFF_TOOLTIP;
  return LIST_UNREAD_LINE;
}

/** The kind is chosen beside the value, so the two cannot drift apart. */
function renderAddRow(share: SharePanelModel, local: ShareTabLocalState): m.Children {
  const target = share.currentTarget;
  const row = share.addRow(target);
  const offAttrs = share.canAdd
    ? {}
    : {
        "aria-disabled": "true",
        "data-tooltip": unavailableTooltip(share),
      };
  return m("div", { id: "ws-share-add", class: "mt-3 shrink-0" }, [
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
            "aria-label": "Who to invite",
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
            submitAddRow(share, local);
          },
        }),
        m(
          Button,
          {
            id: "ws-share-add-btn",
            variant: "secondary",
            ...offAttrs,
            onclick: () => submitAddRow(share, local),
          },
          [m(Icon16, { name: "plus" }), "Add"],
        ),
      ],
    ),
    row.refusalMessage === null
      ? null
      : m(
          "div",
          { id: "ws-share-refusal", class: "mt-1.5" },
          m(InlineError, { message: row.refusalMessage }),
        ),
  ]);
}

function submitAddRow(share: SharePanelModel, local: ShareTabLocalState): void {
  if (!share.canAdd) return;
  const target = share.currentTarget;
  const row = share.addRow(target);
  // The list unfolds for the row being added: its saving, its invitation, or its refusal is what was asked for.
  local.accessOpenTarget = target;
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
  if (grants.length === 0 && inherited.length === 0) {
    // An unread list is not an empty one; with no document at all the pane
    // says so, and with an unreadable list the notice above already does.
    if (!share.isGrantListKnown) {
      if (share.isSharingDocumentLoading)
        return m(
          SkeletonRegion,
          { label: "Loading who has access", extra: "mt-4 shrink-0" },
          m(Skeleton, { extra: "h-4 w-56" }),
        );
      return share.isSharingDocumentKnown
        ? null
        : m(
            "p",
            {
              id: "ws-share-list-unread",
              class: "mt-6 shrink-0 type-body text-tertiary",
            },
            LIST_UNREAD_LINE,
          );
    }
    // A list known to be empty has nothing to say that the empty page does not.
    return null;
  }
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
      ...grants.map((grant) => renderGrantRow(share, local, grant)),
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
    inherited.map((grant) => renderGrantRow(share, local, grant, false)),
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
  local: ShareTabLocalState,
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
      renderGrantDetails(share, local, grant, access, invitation),
      renderInvitationRefusal(share, local, grant, invitation),
    ],
  );
}

/** A row's surface: its kind's while sharing is on, and one flat grey for every
 * kind while it is off. */
function rowSurfaceClass(share: SharePanelModel, isDomain: boolean): string {
  if (share.isPublicationKnown && !share.isPublished) return ROW_INACTIVE_CLASS;
  return isDomain ? ROW_DOMAIN_CLASS : ROW_PERSON_CLASS;
}

/** What is known about the card's person, as short facts joined by dots, or
 * the card's own save state while it has one. */
function renderGrantDetails(
  share: SharePanelModel,
  local: ShareTabLocalState,
  grant: Grant,
  access: WorkspaceAccess | null,
  invitation: InvitationStatusIndicator,
): m.Children {
  const pieces = grantDetailPieces(share, local, grant, access, invitation);
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
  local: ShareTabLocalState,
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
  if (status.state === "failed") {
    // A refused entry says why in its own words and is refused again if sent
    // again, so the way out is to take it off; any other failure can be retried.
    const label = status.isRefused
      ? status.failure.error.message
      : status.failedOperation === "add"
        ? "Couldn't add"
        : "Couldn't remove — still has access";
    return [
      m(
        "span",
        {
          "data-grant-failure": grant.key,
          class: "inline-flex items-baseline gap-[0.33em] text-important",
          // A tooltip is a plain attribute: it carries the sentence, not its
          // markup.
          "data-tooltip": plainErrorText(status.failure.error.message),
        },
        [errorMark("1em"), label],
      ),
      seeFailureDetailsButton(share, local, status.failure),
      status.isRefused
        ? textButton("Remove", () =>
            share.removeGrant(share.currentTarget, grant.key),
          )
        : textButton("Retry", () =>
            share.retryGrant(share.currentTarget, grant.key),
          ),
    ].filter((piece) => piece !== null);
  }
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
  share: SharePanelModel,
  local: ShareTabLocalState,
  grant: Grant,
  state: InvitationStatusIndicator,
): m.Children {
  if (state.kind !== "refused") return null;
  return m(
    "div",
    { "data-invite-refusal": grant.key, class: CARD_DETAIL_CLASS },
    refusalLine(
      share,
      local,
      state.failure,
      state.unverifiedEmail,
      state.resendOutcome,
      (email) => void share.resendVerification(grant.key, email),
      { "data-resend-verification": grant.key },
    ),
  );
}

/** A sentence, then its "See details" when it has one. */
function withSeeDetails(
  sentence: m.Children,
  seeDetails: m.Children,
): m.Children {
  return seeDetails === null ? sentence : [sentence, " ", seeDetails];
}

/** "See details" in a card, opening the failure whole in the error dialog,
 * with its details and buttons; null when the sentence is all there is. */
function seeFailureDetailsButton(
  share: SharePanelModel,
  local: ShareTabLocalState,
  failure: ShareFailure,
): m.Children {
  const hasMore =
    failure.error.details !== "" ||
    failure.remedies.length > 0 ||
    failure.isBugReportOffered;
  if (!hasMore) return null;
  return textButton(
    "See details",
    () => openShareFailureDialog(share, local, failure),
    { "data-failure-details": "" },
  );
}

/** A refusal's marked line, with the resend when it is waiting on a verified
 * email. A resend's own note reads as plain news unless the send failed. */
function refusalLine(
  share: SharePanelModel,
  local: ShareTabLocalState,
  failure: ShareFailure,
  unverifiedEmail: string | undefined,
  resendOutcome: ResendOutcome | undefined,
  onResend: (email: string) => void,
  attrs: Record<string, string>,
): m.Children {
  const resend =
    unverifiedEmail === undefined
      ? null
      : [" ", textButton(RESEND_EMAIL_LABEL, () => onResend(unverifiedEmail), attrs)];
  if (resendOutcome !== undefined && resendOutcome !== "failed")
    return m("span", { class: "type-helper text-secondary" }, [
      failure.error.message,
      resend,
    ]);
  return markedLine(
    { size: "helper", isInline: true },
    withSeeDetails(
      [richErrorText(failure.error.message), resend],
      seeFailureDetailsButton(share, local, failure),
    ),
  );
}

function reinviteButton(share: SharePanelModel, grant: Grant): m.Children {
  return textButton(
    "Resend invite",
    () => void share.invite(share.currentTarget, grant.key),
    { "data-invite": grant.key, "aria-label": "Resend invite" },
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
    monogramLetters(grantText(share, grant).primary),
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

function isJustAdded(share: SharePanelModel, grant: Grant): boolean {
  const addedAtMs = grant.addedAtMs;
  return addedAtMs !== null && share.nowMs() - addedAtMs < JUST_ADDED_MS;
}

/** The target's link, as text to copy and as a code for a phone to scan. Drawn whether or not sharing is on, so a
 * reader sees where the link will be. */
function renderLinkSection(
  share: SharePanelModel,
  local: ShareTabLocalState,
  name: string,
  isWhole: boolean,
): m.Children {
  const target = share.currentTarget;
  // The link is shown only once it can actually be opened: a workspace still
  // being brought up has an address that answers nothing yet, and an app that
  // has not registered one has no address at all.
  const url =
    !share.isPublished || share.isAwaitingLink || share.isAwaitingLabel(target)
      ? null
      : share.targetUrl(target);
  const copied = local.copied;
  const isCopied =
    copied !== null &&
    copied.target === target &&
    share.nowMs() - copied.atMs < COPY_FLASH_MS;
  return m("section", { id: "ws-share-open-elsewhere", class: "shrink-0" }, [
    m("h2", { class: "type-label text-primary" }, `Open “${name}” on another device`),
    m(
      "p",
      { class: "mt-0.5 type-helper text-tertiary" },
      isWhole
        ? "Access your workspace from your phone or another computer."
        : "Access the app from your phone or another computer.",
    ),
    m("div", { class: "mt-3 flex items-start gap-4" }, [
      m(QrCode, { value: url, label: `QR code for the link to ${name}` }),
      m("div", { class: "flex-1 min-w-0" }, [
        // As tall as the code beside it, with the link at its head and the button at its foot: the button's
        // bottom edge is the code's. What the button reports goes under both.
        m("div", { class: `${QR_CODE_ROW_MIN_HEIGHT_CLASS} flex flex-col justify-between gap-1.5` }, [
          m("div", { class: "flex flex-col gap-1.5" }, [
            m(
              "div",
              // Present exactly while the workspace is published: what watches for a link to exist (the smoke
              // journey) waits on this id arriving and leaving.
              { id: share.isPublished ? "ws-share-link" : undefined, class: "flex items-center gap-2" },
              url !== null
                ? [
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
                        "aria-label": isWhole ? "Copy the link" : `Copy the link to ${name}`,
                        onclick: () => {
                          void copyToClipboardWithFlash(share, local, share.currentTarget, url);
                        },
                      },
                      m(Icon16, { name: isCopied ? "check" : "copy" }),
                    ),
                  ]
                : share.isCheckingPublication
                  ? // Neither "off" nor "preparing" is known to be true until Imbue Cloud has answered.
                    m("div", { id: "ws-share-link-checking", class: LINK_BOX_CLASS }, [
                      m(Spinner, { size: "sm", extra: "shrink-0" }),
                      STATUS_CHECKING_LINE,
                    ])
                  : share.isPublished
                    ? renderLinkWait(share, local, target)
                    : m(
                      "p",
                      {
                        id: "ws-share-link-off",
                        class:
                          "flex-1 min-w-0 flex items-center h-9 px-3 rounded-md border border-dashed border-default type-helper text-tertiary",
                      },
                      LINK_OFF_LINE,
                    ),
            ),
            url === null ? null : m("p", { class: "type-helper text-tertiary" }, SECURE_LINK_LINE),
          ]),
          renderMobileLinkControl(share),
        ]),
        renderMobileLinkStatus(share, local, share.mobileLinkState),
      ]),
    ]),
  ]);
}

/** What stands in the link's place while there is none to show: the wait
 * while the link is being set up, and otherwise why there is none. */
function renderLinkWait(
  share: SharePanelModel,
  local: ShareTabLocalState,
  target: string,
): m.Children {
  if (share.isAwaitingLink || target === share.wholeService) {
    if (share.isLinkDeadlinePassed)
      return m(
        "div",
        { class: LINK_BOX_CLASS },
        "The link isn't answering yet",
      );
    return m("div", { class: LINK_BOX_CLASS }, [
      m(Spinner, { size: "sm", extra: "shrink-0" }),
      "Preparing the link",
    ]);
  }
  // An app registers its address when it first starts, so nothing here is
  // under way: say what brings the link.
  if (share.isWorkspaceStopped)
    return m(
      "div",
      { id: "ws-share-app-no-link", class: "flex flex-col items-start gap-2" },
      [
        m(
          "p",
          { class: "type-body text-secondary" },
          "Start this workspace to get this app's link.",
        ),
        ...shareRemedyActions(share, local, "ws-share-app-no-link", [
          "start_workspace",
        ]).map((action) =>
          m(
            Button,
            { variant: "secondary", onclick: action.onclick },
            action.label,
          ),
        ),
      ],
    );
  return m(
    "p",
    { id: "ws-share-app-no-link", class: "type-body text-secondary" },
    "This app gets its link the first time it starts. Open it in this workspace, then come back.",
  );
}

/** The control that emails the signed-in account the link of the target the
 * page is on. */
function renderMobileLinkControl(share: SharePanelModel): m.Children {
  // Dimmed the way the add row dims, so a control that cannot be pressed is
  // legible as one without the reader having to hover for the tooltip. The
  // tooltip says why only when it is not this panel's own send in flight.
  const offTooltip = !share.isPublicationKnown
    ? share.isCheckingPublication
      ? STATUS_CHECKING_LINE
      : STATUS_UNCHECKED_TOOLTIP
    : !share.isPublished
      ? MOBILE_LINK_OFF_TOOLTIP
      : share.isMobileLinkAwaitingAppLink
        ? MOBILE_LINK_NO_APP_LINK_TOOLTIP
        : null;
  const offAttrs = share.canSendMobileAccessLink
    ? {}
    : {
        "aria-disabled": "true",
        ...(offTooltip === null ? {} : { "data-tooltip": offTooltip }),
        extra: "opacity-40",
      };
  return m(
    "div",
    // A flex box, so the button is laid out as its item rather than on a line: a line box would stand a
    // fraction of a pixel taller than the button and lift its bottom edge off the code's. The bubble hangs
    // from this box, which is as tall as the button and starts where it does.
    { id: "ws-share-mobile-link", class: "relative shrink-0 flex" },
    [
      m(
        Button,
        {
          id: "ws-share-mobile-link-btn",
          variant: "secondary",
          size: "xs",
          ...offAttrs,
          onclick: () => {
            // A press answers the bubble, whether or not the link can be sent.
            share.dismissCoachMark();
            void share.sendMobileAccessLink();
          },
        },
        [m(Icon16, { name: "mail" }), MOBILE_LINK_LABEL],
      ),
      share.coachMark === "mobile-access"
        ? m(CoachBubble, {
            id: "ws-share-mobile-link-coach",
            text: MOBILE_LINK_COACH_TEXT,
            onDismiss: () => share.dismissCoachMark(),
          })
        : null,
    ],
  );
}

function renderMobileLinkStatus(
  share: SharePanelModel,
  local: ShareTabLocalState,
  state: MobileLinkState,
): m.Children {
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
      if (state.unverifiedEmail === undefined && state.resendOutcome === undefined)
        return renderShareFailure(
          share,
          local,
          "ws-share-mobile-link-error",
          state.failure,
        );
      return m(
        "div",
        { "data-mobile-link-refusal": "", class: "mt-1.5" },
        refusalLine(
          share,
          local,
          state.failure,
          state.unverifiedEmail,
          state.resendOutcome,
          (email) => void share.resendVerificationForMobileLink(email),
          { "data-resend-verification": "mobile-link" },
        ),
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
  const loadAgentsOnceFirstReadIsIn = (): void => {
    if (!share.isSharingDocumentLoading) void share.ensureVisitingAgentsLoaded();
  };
  return m(
    "section",
    {
      id: "ws-share-agents",
      class: "shrink-0",
      // Listing them is an exec into the workspace, which would only compete with the read the page is waiting
      // on: it starts once that read has something to draw, or has failed.
      oncreate: loadAgentsOnceFirstReadIsIn,
      onupdate: loadAgentsOnceFirstReadIsIn,
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
      m("div", { class: "mt-3 flex flex-col gap-2" }, [
        m(
          Button,
          {
            id: "ws-share-agents-invite",
            extra: "self-start",
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
          ? renderShareFailure(
              share,
              local,
              "ws-share-agents-invite-error",
              inviteState.failure,
            )
          : null,
      ]),
      inviteState.state === "minted" && inviteState.target === target
        ? renderVisitingAgentInstructions(share, local, inviteState)
        : null,
      renderVisitingAgentList(share, local, isWhole),
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
  local: ShareTabLocalState,
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
  const loadFailure = share.visitingAgentsLoadFailure;
  if (loadState === "failed" && loadFailure !== null) {
    return m(
      "div",
      { class: "mt-3" },
      renderShareFailure(share, local, "ws-share-agents-error", loadFailure),
    );
  }
  // The whole-workspace pane lists every agent; an app pane only those scoped to that app.
  const agents = share.visitingAgentList.filter(
    (agent) =>
      isWhole ||
      (Array.isArray(agent.scope) && agent.scope.includes(share.currentTarget)),
  );
  const revokeFailure = share.visitingAgentRevokeFailure;
  return m(
    "div",
    { id: "ws-share-agents-list", class: "mt-3 flex flex-col gap-2" },
    [
      agents.length === 0
        ? m("p", { class: "type-helper text-tertiary" }, VISITING_AGENTS_EMPTY)
        : agents.map((agent) => renderVisitingAgentRow(share, agent)),
      revokeFailure === null
        ? null
        : renderShareFailure(
            share,
            local,
            "ws-share-agents-revoke-error",
            revokeFailure,
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
        monogramLetters(agent.name),
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
