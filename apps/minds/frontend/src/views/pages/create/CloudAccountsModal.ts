// The connection modal: add-form / preparing / result views, and an edit mode
// that replaces an existing connection's credentials in place. A connection
// is one set of cloud credentials (aws, gcp, azure, or modal) owned by an
// imbue cloud account (synced to every device that unlocks it) or by this
// device's local profile. The preparing view deliberately has no way out --
// `mngr <backend> prepare` is creating real cloud resources (modal only
// verifies its token) and must not look abandonable mid-flight.

import m from "mithril";
import type { UiConnectionEntry } from "../../../generated/ui";
import type {
  CloudAccountOption,
  ConnectionBackendOption,
} from "../../../models/create";
import type { ConnectionOwnerOption } from "../../../models/providers";
import { LOCAL_PROFILE_USER_ID } from "../../../models/providers";
import { normalizeCreateApiError } from "./form-model";
import { Button } from "../../components/Button";
import {
  FormLabel,
  Select,
  TextInput,
  Textarea,
} from "../../components/FormControls";
import { Modal } from "../../components/Modal";

type ModalView = "form" | "progress" | "result";

/** Adding a connection, or replacing an existing one's credentials (its id, owner, backend and region fixed). */
export type ConnectionModalMode = "add" | "edit";

/** The request a submit sends: the add route, or the connection's own update route. */
export interface ConnectionRequestTarget {
  method: "POST" | "PUT";
  url: string;
}

export class CloudAccountsModalState {
  isOpen = false;
  view: ModalView = "form";
  mode: ConnectionModalMode = "add";
  /** The instance name of the connection an edit replaces the credentials of; "" while adding. */
  editInstance = "";
  /** How the fixed owner and backend read in edit mode. */
  editOwnerLabel = "";
  editBackendLabel = "";
  backend = "aws";
  /** Whether the selected backend's region is fixed when the connection is added (the create form picks it for the rest). */
  isRegionPinned = true;
  ownerUserId = LOCAL_PROFILE_USER_ID;
  /** Why the preselected owner was replaced by this device only; "" when it was kept. */
  ineligibleOwnerNote = "";
  alias = "";
  awsAccessKeyId = "";
  awsSecretAccessKey = "";
  gcpKeyJson = "";
  azureSubscriptionId = "";
  azureTenantId = "";
  azureClientId = "";
  azureClientSecret = "";
  modalTokenId = "";
  modalTokenSecret = "";
  region = "";
  formError = "";
  resultHeading = "";
  resultMessage = "";
  isBackShownOnResult = false;
  isPrepareInFlight = false;
  progressStartedAtMs = 0;

  /**
   * Open the add form for `backend` under `owner`, when `ownerOptions` offers
   * it; an owner that cannot take a synced connection here (locked, or no
   * master password) is replaced by this device only, with a note saying so.
   */
  openForBackend(
    backend: ConnectionBackendOption,
    owner: ConnectionOwnerOption,
    defaultRegionByBackend: Record<string, string>,
    ownerOptions: readonly ConnectionOwnerOption[],
  ): void {
    this.mode = "add";
    this.editInstance = "";
    this.selectBackend(backend, defaultRegionByBackend[backend.name.toUpperCase()] ?? "");
    const isOwnerEligible = ownerOptions.some(
      (option) => option.userId === owner.userId,
    );
    this.ownerUserId = isOwnerEligible ? owner.userId : LOCAL_PROFILE_USER_ID;
    this.ineligibleOwnerNote = isOwnerEligible
      ? ""
      : `${owner.label || owner.userId} cannot own a synced connection on this device (locked, or no master ` +
        "password), so this connection stays on this device only.";
    this.formError = "";
    this.view = "form";
    this.isOpen = true;
  }

  /**
   * Open the form to replace `entry`'s credentials: its owner, backend and
   * region are fixed and shown, the alias is prefilled, and every credential
   * field starts empty (an update replaces the stored keys whole).
   */
  openForEdit(
    entry: Pick<UiConnectionEntry, "instance" | "backend_label" | "alias" | "placement">,
    backend: ConnectionBackendOption,
    owner: ConnectionOwnerOption,
  ): void {
    this.mode = "edit";
    this.editInstance = entry.instance;
    this.editOwnerLabel = owner.label || owner.userId;
    this.editBackendLabel = entry.backend_label;
    this.ownerUserId = owner.userId;
    this.ineligibleOwnerNote = "";
    this.selectBackend(backend, entry.placement.default_region ?? entry.placement.default_zone ?? "");
    this.clearSecrets();
    this.alias = entry.alias;
    this.formError = "";
    this.view = "form";
    this.isOpen = true;
  }

  /** Where a submit goes: the add route, or the edited connection's own update route. */
  requestTarget(): ConnectionRequestTarget {
    return this.mode === "add"
      ? { method: "POST", url: "/api/v1/desktop/cloud-accounts" }
      : {
          method: "PUT",
          url: `/api/v1/desktop/cloud-accounts/${encodeURIComponent(this.editInstance)}`,
        };
  }

  /** Switch the form to `backend`, starting its region from the server's default for it. */
  selectBackend(backend: ConnectionBackendOption, defaultRegion: string): void {
    this.backend = backend.name;
    this.isRegionPinned = backend.is_region_pinned;
    this.region = defaultRegion;
  }

  close(): void {
    if (this.isPrepareInFlight) return;
    this.isOpen = false;
    // A cancelled paste must not wait in memory to be pre-filled next time.
    this.clearSecrets();
  }

  clearSecrets(): void {
    this.awsAccessKeyId = "";
    this.awsSecretAccessKey = "";
    this.gcpKeyJson = "";
    this.azureSubscriptionId = "";
    this.azureTenantId = "";
    this.azureClientId = "";
    this.azureClientSecret = "";
    this.modalTokenId = "";
    this.modalTokenSecret = "";
    this.alias = "";
  }

  /**
   * The request body, or the message for a field still missing. An edit
   * carries the alias and the credentials only: the owner and the region are
   * the connection's own and the update route keeps them.
   */
  buildRequestBody(): Record<string, string> | string {
    const alias = this.alias.trim();
    if (!alias) return "Give the connection an alias.";
    const body: Record<string, string> = { alias, backend: this.backend };
    if (this.mode === "add") {
      body.owner = this.ownerUserId;
      if (this.isRegionPinned) body.region = this.region;
    }
    if (this.backend === "aws") {
      body.aws_access_key_id = this.awsAccessKeyId.trim();
      body.aws_secret_access_key = this.awsSecretAccessKey.trim();
      if (!body.aws_access_key_id || !body.aws_secret_access_key) {
        return "Both the access key ID and secret access key are required.";
      }
    } else if (this.backend === "gcp") {
      body.gcp_service_account_key_json = this.gcpKeyJson.trim();
      if (!body.gcp_service_account_key_json)
        return "Paste the service-account key JSON.";
    } else if (this.backend === "azure") {
      body.azure_subscription_id = this.azureSubscriptionId.trim();
      body.azure_tenant_id = this.azureTenantId.trim();
      body.azure_client_id = this.azureClientId.trim();
      body.azure_client_secret = this.azureClientSecret.trim();
      if (
        !body.azure_subscription_id ||
        !body.azure_tenant_id ||
        !body.azure_client_id ||
        !body.azure_client_secret
      ) {
        return "Subscription, tenant, client id, and client secret are all required.";
      }
    } else if (this.backend === "modal") {
      body.modal_token_id = this.modalTokenId.trim();
      body.modal_token_secret = this.modalTokenSecret.trim();
      if (!body.modal_token_id || !body.modal_token_secret) {
        return "Both the token ID and token secret are required.";
      }
    } else {
      return `Unknown backend ${this.backend}.`;
    }
    return body;
  }
}

interface CloudAccountsModalAttrs {
  state: CloudAccountsModalState;
  regionOptionsByBackend: Record<string, string[]>;
  /** The backends a connection can be added for, as the form defaults list them. */
  backends: readonly ConnectionBackendOption[];
  ownerOptions: readonly ConnectionOwnerOption[];
  onAccountAdded(account: CloudAccountOption): void;
}

/** The explanation shown when no signed-in account can own a synced connection here. */
const NO_ELIGIBLE_OWNER_MESSAGE =
  "Syncing a connection to your other devices needs a master password: set one in Settings and unlock the " +
  "account here. Until then the connection stays on this device only.";

/**
 * The result view's message for a failed add, chosen by the body's `code`
 * (the status alone is ambiguous: 409, 502 and 503 each cover two causes).
 * `fallback` names the operation for a body with no code this app knows.
 */
export function connectionFailureMessage(
  data: Record<string, unknown>,
  fallback: string,
): string {
  const serverMessage = normalizeCreateApiError(data, "").message;
  switch (data.code) {
    case "master_password_required":
      return (
        "Syncing this connection needs a master password: set one in Settings and unlock the account here, " +
        "or add it to this device only."
      );
    case "credentials_rejected":
      return [serverMessage, "Nothing was saved; fix the keys or permissions and try again."]
        .filter((part) => part !== "")
        .join("\n\n");
    case "sync_failed":
      return (
        "The credentials work, but the connection could not be saved to your account. Check your network and " +
        "try again, or add it to this device only."
      );
    case "account_listing_failed":
      return "Could not read the signed-in accounts; try again in a moment.";
    default:
      return [fallback, serverMessage].filter((part) => part !== "").join("\n\n");
  }
}

function submitAccount(
  state: CloudAccountsModalState,
  onAccountAdded: (account: CloudAccountOption) => void,
): void {
  state.formError = "";
  const bodyOrError = state.buildRequestBody();
  if (typeof bodyOrError === "string") {
    state.formError = bodyOrError;
    return;
  }
  state.isPrepareInFlight = true;
  state.view = "progress";
  state.progressStartedAtMs = Date.now();
  const target = state.requestTarget();
  fetch(target.url, {
    method: target.method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bodyOrError),
  })
    .then(async (response) => ({
      ok: response.ok,
      data: (await response.json().catch(() => ({}))) as Record<
        string,
        unknown
      >,
    }))
    .then((result) => {
      state.isPrepareInFlight = false;
      if (result.ok && typeof result.data.name === "string") {
        const account: CloudAccountOption = {
          name: result.data.name,
          alias: String(result.data.alias),
          backend: String(result.data.backend),
          region: String(result.data.region),
          owner_user_id: String(result.data.owner_user_id),
          is_region_pinned: result.data.is_region_pinned === true,
        };
        // An edit changes nothing the create form offers; the providers panel refreshes from the next frame.
        if (state.mode === "add") onAccountAdded(account);
        const where = account.is_region_pinned ? ` in ${account.region}` : "";
        const synced =
          account.owner_user_id === LOCAL_PROFILE_USER_ID
            ? "It stays on this device."
            : "It syncs to every device that unlocks this account.";
        state.resultHeading =
          state.mode === "add" ? "Connection ready" : "Connection updated";
        state.resultMessage =
          state.mode === "add"
            ? `"${account.alias}" is set up${where}. ${synced}`
            : `"${account.alias}" now uses the new credentials. ${synced}`;
        state.isBackShownOnResult = false;
        state.clearSecrets();
      } else {
        state.resultHeading =
          state.mode === "add"
            ? "Connection setup failed"
            : "Connection update failed";
        state.resultMessage = connectionFailureMessage(
          result.data,
          state.mode === "add"
            ? "Could not set up the connection."
            : "Could not update the connection.",
        );
        state.isBackShownOnResult = true;
      }
      state.view = "result";
      m.redraw();
    })
    .catch(() => {
      state.isPrepareInFlight = false;
      state.resultHeading = "Connection setup failed";
      state.resultMessage =
        "Network error talking to Imbue Studio. Nothing was saved; try again.";
      state.isBackShownOnResult = true;
      state.view = "result";
      m.redraw();
    });
}

function labeledInput(
  id: string,
  label: string,
  value: string,
  onValue: (next: string) => void,
  attrs: m.Attributes = {},
): m.Children {
  return m("div", { class: "mt-3" }, [
    m(FormLabel, { target: id }, label),
    m(TextInput, {
      id,
      name: id,
      value,
      oninput: (event: InputEvent) =>
        onValue((event.target as HTMLInputElement).value),
      ...attrs,
    }),
  ]);
}

export function CloudAccountsModal(): m.Component<CloudAccountsModalAttrs> {
  // Drives the time-eased progress bar while the prepare fetch is in flight:
  // progressPercent derives from Date.now(), and without a ticker nothing
  // would redraw for the whole 1-15 minute operation (CreatingPage pattern).
  let progressTimer: ReturnType<typeof setInterval> | null = null;

  function syncProgressTimer(state: CloudAccountsModalState): void {
    if (state.isPrepareInFlight && progressTimer === null) {
      progressTimer = setInterval(() => m.redraw(), 250);
    } else if (!state.isPrepareInFlight && progressTimer !== null) {
      clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  return {
    oncreate(vnode) {
      syncProgressTimer(vnode.attrs.state);
    },
    onupdate(vnode) {
      syncProgressTimer(vnode.attrs.state);
    },
    onremove() {
      if (progressTimer !== null) clearInterval(progressTimer);
      progressTimer = null;
    },
    view(vnode) {
      const {
        state,
        regionOptionsByBackend,
        backends,
        ownerOptions,
        onAccountAdded,
      } = vnode.attrs;
      const regionOptions =
        regionOptionsByBackend[state.backend.toUpperCase()] ?? [];
      const isEdit = state.mode === "edit";
      const isOnlyLocalOwner = ownerOptions.every(
        (option) => option.userId === LOCAL_PROFILE_USER_ID,
      );
      const progressPercent = state.isPrepareInFlight
        ? Math.min(
            90,
            90 *
              (1 -
                Math.exp(
                  -((Date.now() - state.progressStartedAtMs) / 1000) / 12,
                )),
          )
        : 100;
      return m(
        Modal,
        {
          id: "cloud-accounts-modal",
          isOpen: state.isOpen,
          onClose: () => state.close(),
          size: "xl",
          cardExtra: "text-left",
        },
        [
          state.view === "form"
            ? m("div", { id: "ca-form-view" }, [
                m(
                  "span",
                  { class: "type-heading text-primary" },
                  isEdit
                    ? "Update connection credentials"
                    : "Add a cloud connection",
                ),
                m(
                  "div",
                  { class: "mt-3 flex items-center justify-between gap-3" },
                  [
                    isEdit
                      ? m("span", { class: "type-label text-primary" }, "Owner")
                      : m(FormLabel, { target: "ca-owner", inline: true }, "Owner"),
                    isEdit
                      ? m(
                          "span",
                          { id: "ca-owner-fixed", class: "type-body text-secondary" },
                          state.editOwnerLabel,
                        )
                      : m(
                          Select,
                          {
                            id: "ca-owner",
                            name: "ca-owner",
                            width: "w-64",
                            value: state.ownerUserId,
                            onchange: (event: Event) => {
                              state.ownerUserId = (
                                event.target as HTMLSelectElement
                              ).value;
                              // The note explains the opening pick; a pick of the user's own needs no explaining.
                              state.ineligibleOwnerNote = "";
                            },
                          },
                          ownerOptions.map((option) =>
                            m(
                              "option",
                              {
                                value: option.userId,
                                selected: option.userId === state.ownerUserId,
                              },
                              option.label,
                            ),
                          ),
                        ),
                  ],
                ),
                isEdit
                  ? m(
                      "p",
                      { class: "mt-1 type-helper text-tertiary" },
                      "The connection keeps its owner, provider and region; paste the full set of new credentials.",
                    )
                  : state.ineligibleOwnerNote !== "" || isOnlyLocalOwner
                    ? m(
                        "p",
                        {
                          id: "ca-owner-note",
                          class: "mt-1 type-helper text-tertiary",
                        },
                        state.ineligibleOwnerNote || NO_ELIGIBLE_OWNER_MESSAGE,
                      )
                    : m(
                        "p",
                        { class: "mt-1 type-helper text-tertiary" },
                        "A connection owned by an account follows you to every device that unlocks it; " +
                          "one owned by this device stays here.",
                      ),
                m(
                  "div",
                  { class: "mt-3 flex items-center justify-between gap-3" },
                  [
                    isEdit
                      ? m("span", { class: "type-label text-primary" }, "Provider")
                      : m(
                          FormLabel,
                          { target: "ca-backend", inline: true },
                          "Provider",
                        ),
                    isEdit
                      ? m(
                          "span",
                          { id: "ca-backend-fixed", class: "type-body text-secondary" },
                          state.editBackendLabel,
                        )
                      : m(
                          Select,
                          {
                            id: "ca-backend",
                            name: "ca-backend",
                            width: "w-48",
                            value: state.backend,
                            onchange: (event: Event) => {
                              const name = (event.target as HTMLSelectElement)
                                .value;
                              const backend = backends.find(
                                (option) => option.name === name,
                              );
                              if (backend === undefined) return;
                              state.selectBackend(
                                backend,
                                regionOptionsByBackend[name.toUpperCase()]?.[0] ??
                                  "",
                              );
                            },
                          },
                          backends.map((backend) =>
                            m(
                              "option",
                              {
                                value: backend.name,
                                selected: backend.name === state.backend,
                              },
                              backend.label,
                            ),
                          ),
                        ),
                  ],
                ),
                state.backend === "aws"
                  ? m("p", { class: "mt-3 type-helper text-tertiary" }, [
                      "You need an ",
                      m("strong", "IAM user"),
                      " (not the root account) with the AmazonEC2FullAccess and AmazonS3FullAccess policies. ",
                      "In the AWS console: IAM, Users, Create user, attach those policies, Security credentials, ",
                      "Create access key. Paste the key pair below.",
                    ])
                  : null,
                state.backend === "gcp"
                  ? m("p", { class: "mt-3 type-helper text-tertiary" }, [
                      "You need a ",
                      m("strong", "service account"),
                      " in a project with billing enabled and the Compute Engine + Cloud Storage APIs turned on. ",
                      "Grant Compute Admin, Storage Admin, and Service Account User, then add a JSON key and ",
                      "paste the whole downloaded file below.",
                    ])
                  : null,
                state.backend === "azure"
                  ? m("p", { class: "mt-3 type-helper text-tertiary" }, [
                      "You need a ",
                      m("strong", "service principal with the Owner role"),
                      " on the subscription. Note the client + tenant ids, create a client secret, and assign ",
                      "Owner to the app. The region you pick below is permanent for this connection; add another ",
                      "connection with the same keys for another region.",
                    ])
                  : null,
                state.backend === "modal"
                  ? m("p", { class: "mt-3 type-helper text-tertiary" }, [
                      "You need a ",
                      m("strong", "Modal token"),
                      " (run `modal token new`, or create one in the Modal dashboard under Settings, API tokens). ",
                      "Modal sandboxes stop after about a day. ",
                      "The region is picked per machine when you create it.",
                    ])
                  : null,
                labeledInput(
                  "ca-alias",
                  "Alias",
                  state.alias,
                  (next) => {
                    state.alias = next;
                  },
                  { placeholder: "my cloud connection" },
                ),
                state.backend === "aws"
                  ? m("div", { id: "ca-fields-aws" }, [
                      labeledInput(
                        "ca-access-key-id",
                        "Access key ID",
                        state.awsAccessKeyId,
                        (next) => {
                          state.awsAccessKeyId = next;
                        },
                        { placeholder: "AKIA..." },
                      ),
                      labeledInput(
                        "ca-secret-key",
                        "Secret access key",
                        state.awsSecretAccessKey,
                        (next) => {
                          state.awsSecretAccessKey = next;
                        },
                        {
                          type: "password",
                          placeholder:
                            "paste the secret shown once at key creation",
                        },
                      ),
                    ])
                  : null,
                state.backend === "gcp"
                  ? m("div", { id: "ca-fields-gcp" }, [
                      m("div", { class: "mt-3" }, [
                        m(
                          FormLabel,
                          { target: "ca-gcp-key-json" },
                          "Service-account key JSON",
                        ),
                        m(Textarea, {
                          id: "ca-gcp-key-json",
                          name: "ca-gcp-key-json",
                          rows: 5,
                          extra: "font-mono",
                          placeholder:
                            '{"type": "service_account", "project_id": "...", ...}',
                          value: state.gcpKeyJson,
                          oninput: (event: InputEvent) => {
                            state.gcpKeyJson = (
                              event.target as HTMLTextAreaElement
                            ).value;
                          },
                        }),
                      ]),
                    ])
                  : null,
                state.backend === "azure"
                  ? m("div", { id: "ca-fields-azure" }, [
                      labeledInput(
                        "ca-azure-subscription-id",
                        "Subscription ID",
                        state.azureSubscriptionId,
                        (next) => {
                          state.azureSubscriptionId = next;
                        },
                        { placeholder: "00000000-0000-0000-0000-000000000000" },
                      ),
                      labeledInput(
                        "ca-azure-tenant-id",
                        "Tenant ID",
                        state.azureTenantId,
                        (next) => {
                          state.azureTenantId = next;
                        },
                        { placeholder: "Entra directory (tenant) id" },
                      ),
                      labeledInput(
                        "ca-azure-client-id",
                        "Client ID",
                        state.azureClientId,
                        (next) => {
                          state.azureClientId = next;
                        },
                        { placeholder: "app registration (client) id" },
                      ),
                      labeledInput(
                        "ca-azure-client-secret",
                        "Client secret",
                        state.azureClientSecret,
                        (next) => {
                          state.azureClientSecret = next;
                        },
                        {
                          type: "password",
                          placeholder: "the secret's Value (shown once)",
                        },
                      ),
                    ])
                  : null,
                state.backend === "modal"
                  ? m("div", { id: "ca-fields-modal" }, [
                      labeledInput(
                        "ca-modal-token-id",
                        "Token ID",
                        state.modalTokenId,
                        (next) => {
                          state.modalTokenId = next;
                        },
                        { placeholder: "ak-..." },
                      ),
                      labeledInput(
                        "ca-modal-token-secret",
                        "Token secret",
                        state.modalTokenSecret,
                        (next) => {
                          state.modalTokenSecret = next;
                        },
                        { type: "password", placeholder: "as-..." },
                      ),
                    ])
                  : null,
                state.isRegionPinned && isEdit
                  ? m(
                      "div",
                      { class: "mt-3 flex items-center justify-between gap-3" },
                      [
                        m("span", { class: "type-label text-primary" }, "Region / zone"),
                        m(
                          "span",
                          { id: "ca-region-fixed", class: "type-body text-secondary" },
                          state.region,
                        ),
                      ],
                    )
                  : null,
                state.isRegionPinned && !isEdit
                  ? m(
                      "div",
                      { class: "mt-3 flex items-center justify-between gap-3" },
                      [
                        m(
                          FormLabel,
                          { target: "ca-region", inline: true },
                          "Default region / zone",
                        ),
                        m(
                          Select,
                          {
                            id: "ca-region",
                            name: "ca-region",
                            width: "w-48",
                            value: state.region,
                            onchange: (event: Event) => {
                              state.region = (
                                event.target as HTMLSelectElement
                              ).value;
                            },
                          },
                          regionOptions.map((region) =>
                            m(
                              "option",
                              {
                                value: region,
                                selected: region === state.region,
                              },
                              region,
                            ),
                          ),
                        ),
                      ],
                    )
                  : null,
                state.formError
                  ? m(
                      "p",
                      {
                        id: "ca-form-error",
                        class: "mt-3 type-helper text-important",
                      },
                      state.formError,
                    )
                  : null,
                m("div", { class: "flex justify-between mt-6" }, [
                  m(
                    Button,
                    {
                      variant: "secondary",
                      id: "ca-form-back-btn",
                      onclick: () => state.close(),
                    },
                    "Cancel",
                  ),
                  m(
                    Button,
                    {
                      variant: "primary",
                      id: "ca-form-submit-btn",
                      onclick: () => submitAccount(state, onAccountAdded),
                    },
                    isEdit
                      ? "Save & verify credentials"
                      : "Save & prepare connection",
                  ),
                ]),
              ])
            : null,
          state.view === "progress"
            ? m("div", { id: "ca-progress-view" }, [
                m(
                  "span",
                  { class: "type-heading text-primary" },
                  isEdit
                    ? "Verifying the new credentials..."
                    : "Setting up your connection...",
                ),
                m(
                  "p",
                  { class: "mt-2 type-helper text-tertiary" },
                  state.backend === "modal"
                    ? "Verifying the token with Modal. Usually a few seconds. Leave this open."
                    : [
                        "Verifying the credentials and creating the network ingress + state storage. Usually under a ",
                        "minute, except a brand-new Azure subscription's first setup, which can take 10-15 minutes. ",
                        "Leave this open.",
                      ],
                ),
                m(
                  "div",
                  {
                    class:
                      "mt-4 h-1.5 bg-fill-subtle rounded-full overflow-hidden",
                  },
                  m("div", {
                    id: "ca-bar-fill",
                    class:
                      "h-full rounded-full bg-accent transition-[width] duration-300 ease-out",
                    style: `width: ${progressPercent.toFixed(0)}%`,
                  }),
                ),
              ])
            : null,
          state.view === "result"
            ? m("div", { id: "ca-result-view" }, [
                m(
                  "span",
                  {
                    id: "ca-result-heading",
                    class: "type-heading text-primary",
                  },
                  state.resultHeading,
                ),
                m(
                  "p",
                  {
                    id: "ca-result-message",
                    class:
                      "mt-2 type-helper text-secondary whitespace-pre-wrap break-words max-h-48 overflow-y-auto",
                  },
                  state.resultMessage,
                ),
                m("div", { class: "flex justify-end gap-3 mt-6" }, [
                  state.isBackShownOnResult
                    ? m(
                        Button,
                        {
                          variant: "secondary",
                          id: "ca-result-back-btn",
                          onclick: () => {
                            state.view = "form";
                          },
                        },
                        "Back to form",
                      )
                    : null,
                  m(
                    Button,
                    {
                      variant: "primary",
                      id: "ca-result-done-btn",
                      onclick: () => state.close(),
                    },
                    "Done",
                  ),
                ]),
              ])
            : null,
        ],
      );
    },
  };
}
