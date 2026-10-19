// The connection modal: add-form / preparing / result views, and an edit mode
// that replaces an existing connection's credentials in place. A connection
// is one set of cloud credentials (aws, gcp, azure, or modal) owned by an
// imbue cloud account (synced to every device that unlocks it) or by this
// device's local profile. Its credentials are either pasted keys or a login
// installed on each device (an AWS profile, an az login, a Modal profile),
// in which case nothing secret is stored or synced. The preparing view
// deliberately has no way out -- `mngr <backend> prepare` is creating real
// cloud resources (modal only verifies its token) and must not look
// abandonable mid-flight.

import m from "mithril";
import type { UiConnectionEntry } from "../../../generated/ui";
import type {
  CloudAccountOption,
  ConnectionBackendOption,
} from "../../../models/create";
import type { LifecycleDeps } from "../../../models/backups";
import { browserLifecycleDeps } from "../../../models/backups";
import { ConnectionOperationWatcher } from "../../../models/connectionOperation";
import type { ConnectionOwnerOption } from "../../../models/providers";
import { LOCAL_PROFILE_USER_ID } from "../../../models/providers";
import { normalizeCreateApiError } from "./form-model";
import { alphaFeatureNotice } from "./ComputeNotices";
import { Button } from "../../components/Button";
import {
  FormLabel,
  Select,
  TextInput,
  Textarea,
} from "../../components/FormControls";
import { Modal } from "../../components/Modal";

type ModalView = "form" | "progress" | "result";

/** Where a connection's credentials come from, as the server spells it. */
export type CredentialSource = "pasted" | "local_profile";

/** One login on this machine, as `GET /api/v1/desktop/cloud-accounts/local-profiles` lists it. */
export interface LocalProfileOption {
  name: string;
  label: string;
  region: string | null;
  tenant_id: string | null;
  subscription_id: string | null;
  unusable_reason: string;
}

/** Fetches the logins on this machine for one backend; injected so tests need no network. */
export type LocalProfilesLoader = (backend: string) => Promise<LocalProfileOption[]>;

export async function fetchLocalProfiles(backend: string): Promise<LocalProfileOption[]> {
  const response = await fetch(
    `/api/v1/desktop/cloud-accounts/local-profiles?backend=${encodeURIComponent(backend)}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) throw new Error(`local profiles: HTTP ${response.status}`);
  const body = (await response.json()) as { profiles: LocalProfileOption[] };
  return body.profiles;
}

/**
 * `loader` plus a Mithril redraw once its listing settles: the listing lands
 * outside any event handler, so nothing else would render it. The state takes
 * the result in the continuation right after the promise settles, and
 * `m.redraw()` only schedules the next frame, so that frame shows the result.
 */
export function redrawingLocalProfilesLoader(loader: LocalProfilesLoader): LocalProfilesLoader {
  return (backend) => loader(backend).finally(() => m.redraw());
}

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
  /** Whether the selected backend lets a connection name a login on this device instead of pasting keys. */
  isLocalProfileSupported = true;
  credentialSource: CredentialSource = "pasted";
  /** The login a local-profile connection names: the AWS or Modal profile name. */
  awsProfile = "";
  modalProfile = "";
  /** The logins found on this machine for the selected backend, once loaded. */
  localProfiles: LocalProfileOption[] = [];
  isLocalProfilesLoading = false;
  localProfilesError = "";
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
  /** The prepare's output so far, as the operation's log streams it. */
  logLines: string[] = [];
  isLogOpen = false;
  private watcher: ConnectionOperationWatcher | null = null;

  /** The route accepted the request (202): follow the operation until it ends. */
  startOperation(
    operationId: string,
    deps: LifecycleDeps,
    onAccountAdded: (account: CloudAccountOption) => void,
  ): void {
    this.watcher?.stop();
    this.watcher = new ConnectionOperationWatcher(deps, operationId, {
      onLog: (line) => {
        this.logLines.push(line);
      },
      onDone: (account) => {
        this.watcher = null;
        this.finishWithAccount(account, onAccountAdded);
      },
      onFailed: (failure) => {
        this.watcher = null;
        this.finishWithFailure(failure);
      },
    });
    this.watcher.start();
  }

  /** The connection is registered (or updated): show the result and hand it to the form. */
  finishWithAccount(account: CloudAccountOption, onAccountAdded: (account: CloudAccountOption) => void): void {
    this.isPrepareInFlight = false;
    // An edit changes nothing the create form offers; the providers panel refreshes from the next frame.
    if (this.mode === "add") onAccountAdded(account);
    const where = account.is_region_pinned ? ` in ${account.region}` : "";
    const synced =
      account.owner_user_id === LOCAL_PROFILE_USER_ID
        ? "It stays on this device."
        : "It syncs to every device that unlocks this account.";
    this.resultHeading = this.mode === "add" ? "Connection ready" : "Connection updated";
    this.resultMessage =
      this.mode === "add"
        ? `"${account.alias}" is set up${where}. ${synced}`
        : `"${account.alias}" now uses the new credentials. ${synced}`;
    this.isBackShownOnResult = false;
    this.clearSecrets();
    this.view = "result";
  }

  /** The route, or the operation it started, refused: show why, with a way back to the form. */
  finishWithFailure(failure: Record<string, unknown>): void {
    this.isPrepareInFlight = false;
    this.resultHeading = this.mode === "add" ? "Connection setup failed" : "Connection update failed";
    this.resultMessage = connectionFailureMessage(
      failure,
      this.mode === "add" ? "Could not set up the connection." : "Could not update the connection.",
    );
    this.isBackShownOnResult = true;
    this.view = "result";
  }

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
    this.credentialSource = "pasted";
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
   * field starts empty (an update replaces the stored keys whole). A
   * local-profile connection opens on its login form, so this device's logins
   * are listed through `loadProfiles` right away.
   */
  openForEdit(
    entry: Pick<UiConnectionEntry, "instance" | "backend_label" | "alias" | "placement"> &
      Partial<Pick<UiConnectionEntry, "credential_source">>,
    backend: ConnectionBackendOption,
    owner: ConnectionOwnerOption,
    loadProfiles: LocalProfilesLoader,
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
    // A local-profile connection opens on the login it names, so a user who only
    // wants to switch profiles sees the current one; pasted keys are never shown back.
    if (entry.credential_source === "local_profile" && backend.is_local_profile_supported) {
      this.credentialSource = "local_profile";
      this.awsProfile = entry.placement.aws_profile ?? "";
      this.modalProfile = entry.placement.modal_profile ?? "";
      this.azureTenantId = entry.placement.tenant_id ?? "";
      this.azureSubscriptionId = entry.placement.subscription_id ?? "";
    } else {
      this.credentialSource = "pasted";
    }
    this.formError = "";
    this.view = "form";
    this.isOpen = true;
    this.refreshLocalProfiles(loadProfiles);
  }

  /**
   * Switch between pasting keys and naming a login on this device. Both forms
   * keep their values (toggling back shows what was typed); `buildRequestBody`
   * sends only the chosen source's fields.
   */
  selectCredentialSource(source: CredentialSource, loadProfiles: LocalProfilesLoader): void {
    if (source === "local_profile" && !this.isLocalProfileSupported) return;
    this.credentialSource = source;
    this.formError = "";
    this.refreshLocalProfiles(loadProfiles);
  }

  /** (Re)list this device's logins whenever the form is on the local-profile source: it opened there, or its backend changed. */
  refreshLocalProfiles(loadProfiles: LocalProfilesLoader): void {
    if (this.credentialSource !== "local_profile") return;
    void this.loadLocalProfiles(loadProfiles);
  }

  /** List this machine's logins for the selected backend; a failed listing leaves the free-text field. */
  async loadLocalProfiles(loadProfiles: LocalProfilesLoader): Promise<void> {
    const backend = this.backend;
    // The form may have moved on while the listing was in flight (another backend,
    // or back to pasted keys); a late result must not seed the fields it now shows.
    const isListingStillWanted = () => this.backend === backend && this.credentialSource === "local_profile";
    this.isLocalProfilesLoading = true;
    this.localProfilesError = "";
    try {
      const profiles = await loadProfiles(backend);
      if (!isListingStillWanted()) return;
      this.localProfiles = profiles;
      this.pickFirstUsableLocalProfile();
    } catch (error) {
      console.error("Failed to list this device's logins", error);
      if (isListingStillWanted()) {
        this.localProfiles = [];
        this.localProfilesError = "Could not read this device's logins; type the name below.";
      }
    } finally {
      if (isListingStillWanted()) this.isLocalProfilesLoading = false;
    }
  }

  /** Seed the login fields from the first usable listed login when nothing is chosen yet. */
  pickFirstUsableLocalProfile(): void {
    const first = this.localProfiles.find((profile) => profile.unusable_reason === "");
    if (first === undefined) return;
    if (this.backend === "aws" && this.awsProfile === "") this.selectLocalProfile(first);
    if (this.backend === "modal" && this.modalProfile === "") this.selectLocalProfile(first);
    if (this.backend === "azure" && this.azureSubscriptionId === "") this.selectLocalProfile(first);
  }

  /** Take a listed login: its name, and for Azure its tenant and subscription; an AWS region seeds the picker. */
  selectLocalProfile(profile: LocalProfileOption): void {
    if (this.backend === "aws") {
      this.awsProfile = profile.name;
      if (this.mode === "add" && profile.region) this.region = profile.region;
    } else if (this.backend === "modal") {
      this.modalProfile = profile.name;
    } else if (this.backend === "azure") {
      this.azureSubscriptionId = profile.subscription_id ?? profile.name;
      this.azureTenantId = profile.tenant_id ?? "";
    }
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
    this.isLocalProfileSupported = backend.is_local_profile_supported;
    if (!this.isLocalProfileSupported) this.credentialSource = "pasted";
    this.region = defaultRegion;
    this.localProfiles = [];
    this.localProfilesError = "";
    this.awsProfile = "";
    this.modalProfile = "";
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
    this.awsProfile = "";
    this.modalProfile = "";
    this.alias = "";
  }

  /**
   * The request body, or the message for a field still missing. An edit
   * carries the alias and the credentials only: the owner and the region are
   * the connection's own and the update route keeps them. A local-profile
   * connection sends the login it names in place of any key.
   */
  buildRequestBody(): Record<string, string> | string {
    const alias = this.alias.trim();
    if (!alias) return "Give the connection an alias.";
    const body: Record<string, string> = { alias, backend: this.backend };
    if (this.mode === "add") {
      body.owner = this.ownerUserId;
      if (this.isRegionPinned) body.region = this.region;
    }
    if (this.credentialSource === "local_profile") {
      body.credential_source = "local_profile";
      if (this.backend === "aws") {
        body.aws_profile = this.awsProfile.trim();
        if (!body.aws_profile) return "Name the AWS profile from ~/.aws/config to use.";
      } else if (this.backend === "azure") {
        body.azure_subscription_id = this.azureSubscriptionId.trim();
        body.azure_tenant_id = this.azureTenantId.trim();
        if (!body.azure_subscription_id || !body.azure_tenant_id) {
          return "The subscription id and tenant id of your az login are both required.";
        }
      } else if (this.backend === "modal") {
        body.modal_profile = this.modalProfile.trim();
        if (!body.modal_profile) return "Name the Modal profile from ~/.modal.toml to use.";
      } else {
        return `A ${this.backend} connection cannot use a login on this device.`;
      }
      return body;
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

export interface CloudAccountsModalAttrs {
  state: CloudAccountsModalState;
  regionOptionsByBackend: Record<string, string[]>;
  /** The backends a connection can be added for, as the form defaults list them. */
  backends: readonly ConnectionBackendOption[];
  ownerOptions: readonly ConnectionOwnerOption[];
  onAccountAdded(account: CloudAccountOption): void;
  /** Lists this device's logins for a backend; defaults to the API route. */
  loadLocalProfiles?: LocalProfilesLoader;
}

/** The credential-source switch's copy per backend: what a login on this device is called there. */
export function localProfileChoiceLabel(backend: string): string {
  switch (backend) {
    case "aws":
      return "Use an AWS profile on this device";
    case "azure":
      return "Use my az login on this device";
    case "modal":
      return "Use a Modal profile on this device";
    default:
      return "Use a login on this device";
  }
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
  deps: LifecycleDeps,
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
  state.logLines = [];
  state.isLogOpen = false;
  const target = state.requestTarget();
  fetch(target.url, {
    method: target.method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bodyOrError),
  })
    .then(async (response) => ({
      status: response.status,
      data: (await response.json().catch(() => ({}))) as Record<
        string,
        unknown
      >,
    }))
    .then((result) => {
      // The row and its block exist; the credentials are being validated on a
      // worker whose output streams into the progress view until it ends.
      if (result.status === 202 && typeof result.data.operation_id === "string") {
        state.startOperation(result.data.operation_id, deps, onAccountAdded);
        return;
      }
      state.finishWithFailure(result.data);
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

/** A login-name field backed by a datalist of the logins found on this device (free text stays allowed). */
function localProfileInput(
  state: CloudAccountsModalState,
  id: string,
  label: string,
  value: string,
  onValue: (next: string) => void,
  placeholder: string,
): m.Children {
  const listId = `${id}-list`;
  const unusable = state.localProfiles.find((profile) => profile.name === value && profile.unusable_reason !== "");
  return m("div", { class: "mt-3" }, [
    m(FormLabel, { target: id }, label),
    m(TextInput, {
      id,
      name: id,
      value,
      list: listId,
      placeholder,
      oninput: (event: InputEvent) => onValue((event.target as HTMLInputElement).value),
    }),
    m(
      "datalist",
      { id: listId },
      state.localProfiles.map((profile) => m("option", { value: profile.name }, profile.label)),
    ),
    unusable !== undefined
      ? m(
          "p",
          { id: `${id}-unusable`, class: "mt-1 type-helper text-important" },
          `This profile ${unusable.unusable_reason}.`,
        )
      : null,
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
        loadLocalProfiles: listLocalProfiles = fetchLocalProfiles,
      } = vnode.attrs;
      const loadLocalProfiles = redrawingLocalProfilesLoader(listLocalProfiles);
      const isLocalProfile = state.credentialSource === "local_profile";
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
                alphaFeatureNotice("ca-alpha-notice"),
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
                      "The connection keeps its owner, provider and region; paste the full set of new credentials, " +
                        "or switch it to a login on this device.",
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
                              state.refreshLocalProfiles(loadLocalProfiles);
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
                state.isLocalProfileSupported
                  ? m(
                      "div",
                      { id: "ca-credential-source", class: "mt-3 flex items-center justify-between gap-3" },
                      [
                        m(FormLabel, { target: "ca-credential-source-select", inline: true }, "Credentials"),
                        m(
                          Select,
                          {
                            id: "ca-credential-source-select",
                            name: "ca-credential-source",
                            width: "w-72",
                            value: state.credentialSource,
                            onchange: (event: Event) => {
                              state.selectCredentialSource(
                                (event.target as HTMLSelectElement).value as CredentialSource,
                                loadLocalProfiles,
                              );
                            },
                          },
                          [
                            m("option", { value: "pasted", selected: !isLocalProfile }, "Paste keys"),
                            m(
                              "option",
                              { value: "local_profile", selected: isLocalProfile },
                              localProfileChoiceLabel(state.backend),
                            ),
                          ],
                        ),
                      ],
                    )
                  : null,
                isLocalProfile
                  ? m("p", { id: "ca-local-profile-note", class: "mt-1 type-helper text-tertiary" }, [
                      "Nothing secret is stored or synced: the connection names the login, and each device ",
                      "that uses it needs that login set up (",
                      state.backend === "aws"
                        ? "aws configure --profile <name>"
                        : state.backend === "azure"
                          ? "az login"
                          : "modal token new --profile <name>",
                      "). ",
                      state.isLocalProfilesLoading ? "Looking for logins on this device…" : "",
                      state.localProfilesError,
                    ])
                  : null,
                isLocalProfile && state.backend === "aws"
                  ? m("div", { id: "ca-fields-aws-profile" }, [
                      localProfileInput(
                        state,
                        "ca-aws-profile",
                        "AWS profile",
                        state.awsProfile,
                        (next) => {
                          state.awsProfile = next;
                        },
                        "the [profile <name>] section of ~/.aws/config",
                      ),
                    ])
                  : null,
                isLocalProfile && state.backend === "modal"
                  ? m("div", { id: "ca-fields-modal-profile" }, [
                      localProfileInput(
                        state,
                        "ca-modal-profile",
                        "Modal profile",
                        state.modalProfile,
                        (next) => {
                          state.modalProfile = next;
                        },
                        "a section of ~/.modal.toml",
                      ),
                    ])
                  : null,
                isLocalProfile && state.backend === "azure"
                  ? m("div", { id: "ca-fields-azure-login" }, [
                      state.localProfiles.length > 0
                        ? m("div", { class: "mt-3" }, [
                            m(FormLabel, { target: "ca-azure-subscription-pick" }, "Subscription of your az login"),
                            m(
                              Select,
                              {
                                id: "ca-azure-subscription-pick",
                                name: "ca-azure-subscription-pick",
                                value: state.azureSubscriptionId,
                                onchange: (event: Event) => {
                                  const picked = state.localProfiles.find(
                                    (profile) => profile.name === (event.target as HTMLSelectElement).value,
                                  );
                                  if (picked !== undefined) state.selectLocalProfile(picked);
                                },
                              },
                              state.localProfiles.map((profile) =>
                                m(
                                  "option",
                                  { value: profile.name, selected: profile.name === state.azureSubscriptionId },
                                  profile.label,
                                ),
                              ),
                            ),
                          ])
                        : null,
                      labeledInput(
                        "ca-azure-login-subscription-id",
                        "Subscription ID",
                        state.azureSubscriptionId,
                        (next) => {
                          state.azureSubscriptionId = next;
                        },
                        { placeholder: "00000000-0000-0000-0000-000000000000" },
                      ),
                      labeledInput(
                        "ca-azure-login-tenant-id",
                        "Tenant ID",
                        state.azureTenantId,
                        (next) => {
                          state.azureTenantId = next;
                        },
                        { placeholder: "Entra directory (tenant) id" },
                      ),
                    ])
                  : null,
                !isLocalProfile && state.backend === "aws"
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
                !isLocalProfile && state.backend === "azure"
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
                !isLocalProfile && state.backend === "modal"
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
                      onclick: () =>
                        submitAccount(state, onAccountAdded, browserLifecycleDeps(() => m.redraw())),
                    },
                    isEdit
                      ? isLocalProfile
                        ? "Save & verify login"
                        : "Save & verify credentials"
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
                m(
                  "div",
                  { class: "mt-3" },
                  m(
                    Button,
                    {
                      id: "ca-log-toggle",
                      variant: "ghost",
                      extra: "!p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent hover:underline",
                      onclick: () => {
                        state.isLogOpen = !state.isLogOpen;
                      },
                    },
                    state.isLogOpen ? "Hide details" : "Show details",
                  ),
                ),
                state.isLogOpen
                  ? m(
                      "pre",
                      {
                        id: "ca-logs",
                        class: "mt-2 type-helper font-mono bg-fill-subtle rounded-md p-3 max-h-[22vh] overflow-y-auto",
                        onupdate: (vnode: m.VnodeDOM) => {
                          const element = vnode.dom as HTMLElement;
                          element.scrollTop = element.scrollHeight;
                        },
                      },
                      state.logLines.length > 0 ? state.logLines.join("\n") : "Waiting for the first line of output...",
                    )
                  : null,
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
