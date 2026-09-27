// The bring-your-own-key add-account modal (port of the create page's
// cloud-accounts modal): add-form / preparing / result views. The preparing
// view deliberately has no way out -- `mngr <backend> prepare` is creating
// real cloud resources and must not look abandonable mid-flight.

import m from "mithril";
import type { CloudAccountOption } from "../../../models/create";
import { normalizeCreateApiError } from "./form-model";
import { Button } from "../../components/Button";
import { FormLabel, Select, TextInput, Textarea } from "../../components/FormControls";
import { Modal } from "../../components/Modal";

type ModalView = "form" | "progress" | "result";

export class CloudAccountsModalState {
  isOpen = false;
  view: ModalView = "form";
  backend = "aws";
  alias = "";
  awsAccessKeyId = "";
  awsSecretAccessKey = "";
  gcpKeyJson = "";
  azureSubscriptionId = "";
  azureTenantId = "";
  azureClientId = "";
  azureClientSecret = "";
  region = "";
  formError = "";
  resultHeading = "";
  resultMessage = "";
  isBackShownOnResult = false;
  isPrepareInFlight = false;
  progressStartedAtMs = 0;

  openForBackend(backend: string, defaultRegionByBackend: Record<string, string>): void {
    this.backend = backend;
    this.region = defaultRegionByBackend[backend.toUpperCase()] ?? "";
    this.formError = "";
    this.view = "form";
    this.isOpen = true;
  }

  close(): void {
    if (this.isPrepareInFlight) return;
    this.isOpen = false;
  }

  clearSecrets(): void {
    this.awsAccessKeyId = "";
    this.awsSecretAccessKey = "";
    this.gcpKeyJson = "";
    this.azureSubscriptionId = "";
    this.azureTenantId = "";
    this.azureClientId = "";
    this.azureClientSecret = "";
    this.alias = "";
  }

  buildRequestBody(): Record<string, string> | string {
    const alias = this.alias.trim();
    if (!alias) return "Give the account an alias.";
    const body: Record<string, string> = { alias, backend: this.backend, region: this.region };
    if (this.backend === "aws") {
      body.aws_access_key_id = this.awsAccessKeyId.trim();
      body.aws_secret_access_key = this.awsSecretAccessKey.trim();
      if (!body.aws_access_key_id || !body.aws_secret_access_key) {
        return "Both the access key ID and secret access key are required.";
      }
    } else if (this.backend === "gcp") {
      body.gcp_service_account_key_json = this.gcpKeyJson.trim();
      if (!body.gcp_service_account_key_json) return "Paste the service-account key JSON.";
    } else {
      body.azure_subscription_id = this.azureSubscriptionId.trim();
      body.azure_tenant_id = this.azureTenantId.trim();
      body.azure_client_id = this.azureClientId.trim();
      body.azure_client_secret = this.azureClientSecret.trim();
      if (!body.azure_subscription_id || !body.azure_tenant_id || !body.azure_client_id || !body.azure_client_secret) {
        return "Subscription, tenant, client id, and client secret are all required.";
      }
    }
    return body;
  }
}

interface CloudAccountsModalAttrs {
  state: CloudAccountsModalState;
  regionOptionsByBackend: Record<string, string[]>;
  onAccountAdded(account: CloudAccountOption): void;
}

function submitAccount(state: CloudAccountsModalState, onAccountAdded: (account: CloudAccountOption) => void): void {
  state.formError = "";
  const bodyOrError = state.buildRequestBody();
  if (typeof bodyOrError === "string") {
    state.formError = bodyOrError;
    return;
  }
  state.isPrepareInFlight = true;
  state.view = "progress";
  state.progressStartedAtMs = Date.now();
  fetch("/api/v1/desktop/cloud-accounts", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bodyOrError),
  })
    .then(async (response) => ({
      ok: response.ok,
      data: (await response.json().catch(() => ({}))) as Record<string, unknown>,
    }))
    .then((result) => {
      state.isPrepareInFlight = false;
      if (result.ok && typeof result.data.name === "string") {
        const account: CloudAccountOption = {
          name: result.data.name,
          alias: String(result.data.alias ?? result.data.name),
          backend: String(result.data.backend ?? state.backend),
          region: String(result.data.region ?? state.region),
        };
        onAccountAdded(account);
        state.resultHeading = "Account ready";
        state.resultMessage = `"${account.alias}" is set up in ${account.region} and selected as the compute provider for this workspace.`;
        state.isBackShownOnResult = false;
        state.clearSecrets();
      } else {
        const error = normalizeCreateApiError(result.data);
        state.resultHeading = "Account setup failed";
        state.resultMessage = `${error.message || "Could not set up the account."}\n\nNothing was saved; fix the keys or permissions and try again.`;
        state.isBackShownOnResult = true;
      }
      state.view = "result";
      m.redraw();
    })
    .catch(() => {
      state.isPrepareInFlight = false;
      state.resultHeading = "Account setup failed";
      state.resultMessage = "Network error talking to minds. Nothing was saved; try again.";
      state.isBackShownOnResult = true;
      state.view = "result";
      m.redraw();
    });
}

function labeledInput(id: string, label: string, value: string, onValue: (next: string) => void, attrs: m.Attributes = {}): m.Children {
  return m("div", { class: "mt-3" }, [
    m(FormLabel, { target: id }, label),
    m(TextInput, {
      id,
      name: id,
      value,
      oninput: (event: InputEvent) => onValue((event.target as HTMLInputElement).value),
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
      const { state, regionOptionsByBackend, onAccountAdded } = vnode.attrs;
      const regionOptions = regionOptionsByBackend[state.backend.toUpperCase()] ?? [];
      const progressPercent = state.isPrepareInFlight
        ? Math.min(90, 90 * (1 - Math.exp(-((Date.now() - state.progressStartedAtMs) / 1000) / 12)))
        : 100;
      return m(
        Modal,
        { id: "cloud-accounts-modal", isOpen: state.isOpen, onClose: () => state.close(), size: "xl", cardExtra: "text-left" },
        [
          state.view === "form"
            ? m("div", { id: "ca-form-view" }, [
                m("span", { class: "type-heading text-primary" }, "Add a cloud account"),
                m("div", { class: "mt-3 flex items-center justify-between gap-3" }, [
                  m(FormLabel, { target: "ca-backend", inline: true }, "Provider"),
                  m(
                    Select,
                    {
                      id: "ca-backend",
                      name: "ca-backend",
                      width: "w-48",
                      value: state.backend,
                      onchange: (event: Event) => {
                        state.backend = (event.target as HTMLSelectElement).value;
                        state.region = regionOptionsByBackend[state.backend.toUpperCase()]?.[0] ?? "";
                      },
                    },
                    [
                      m("option", { value: "aws" }, "AWS"),
                      m("option", { value: "gcp" }, "GCP"),
                      m("option", { value: "azure" }, "Azure"),
                    ],
                  ),
                ]),
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
                      "Owner to the app. The region you pick below is permanent for this entry; add another ",
                      "entry with the same keys for another region.",
                    ])
                  : null,
                labeledInput("ca-alias", "Alias", state.alias, (next) => {
                  state.alias = next;
                }, { placeholder: "my cloud account" }),
                state.backend === "aws"
                  ? m("div", { id: "ca-fields-aws" }, [
                      labeledInput("ca-access-key-id", "Access key ID", state.awsAccessKeyId, (next) => {
                        state.awsAccessKeyId = next;
                      }, { placeholder: "AKIA..." }),
                      labeledInput("ca-secret-key", "Secret access key", state.awsSecretAccessKey, (next) => {
                        state.awsSecretAccessKey = next;
                      }, { type: "password", placeholder: "paste the secret shown once at key create attempt" }),
                    ])
                  : null,
                state.backend === "gcp"
                  ? m("div", { id: "ca-fields-gcp" }, [
                      m("div", { class: "mt-3" }, [
                        m(FormLabel, { target: "ca-gcp-key-json" }, "Service-account key JSON"),
                        m(Textarea, {
                          id: "ca-gcp-key-json",
                          name: "ca-gcp-key-json",
                          rows: 5,
                          extra: "font-mono",
                          placeholder: '{"type": "service_account", "project_id": "...", ...}',
                          value: state.gcpKeyJson,
                          oninput: (event: InputEvent) => {
                            state.gcpKeyJson = (event.target as HTMLTextAreaElement).value;
                          },
                        }),
                      ]),
                    ])
                  : null,
                state.backend === "azure"
                  ? m("div", { id: "ca-fields-azure" }, [
                      labeledInput("ca-azure-subscription-id", "Subscription ID", state.azureSubscriptionId, (next) => {
                        state.azureSubscriptionId = next;
                      }, { placeholder: "00000000-0000-0000-0000-000000000000" }),
                      labeledInput("ca-azure-tenant-id", "Tenant ID", state.azureTenantId, (next) => {
                        state.azureTenantId = next;
                      }, { placeholder: "Entra directory (tenant) id" }),
                      labeledInput("ca-azure-client-id", "Client ID", state.azureClientId, (next) => {
                        state.azureClientId = next;
                      }, { placeholder: "app registration (client) id" }),
                      labeledInput("ca-azure-client-secret", "Client secret", state.azureClientSecret, (next) => {
                        state.azureClientSecret = next;
                      }, { type: "password", placeholder: "the secret's Value (shown once)" }),
                    ])
                  : null,
                m("div", { class: "mt-3 flex items-center justify-between gap-3" }, [
                  m(FormLabel, { target: "ca-region", inline: true }, "Default region / zone"),
                  m(
                    Select,
                    {
                      id: "ca-region",
                      name: "ca-region",
                      width: "w-48",
                      value: state.region,
                      onchange: (event: Event) => {
                        state.region = (event.target as HTMLSelectElement).value;
                      },
                    },
                    regionOptions.map((region) => m("option", { value: region, selected: region === state.region }, region)),
                  ),
                ]),
                state.formError ? m("p", { id: "ca-form-error", class: "mt-3 type-helper text-important" }, state.formError) : null,
                m("div", { class: "flex justify-between mt-6" }, [
                  m(Button, { variant: "secondary", id: "ca-form-back-btn", onclick: () => state.close() }, "Cancel"),
                  m(
                    Button,
                    { variant: "primary", id: "ca-form-submit-btn", onclick: () => submitAccount(state, onAccountAdded) },
                    "Save & prepare account",
                  ),
                ]),
              ])
            : null,
          state.view === "progress"
            ? m("div", { id: "ca-progress-view" }, [
                m("span", { class: "type-heading text-primary" }, "Setting up your account..."),
                m("p", { class: "mt-2 type-helper text-tertiary" }, [
                  "Verifying the credentials and creating the network ingress + state storage. Usually under a ",
                  "minute, except a brand-new Azure subscription's first setup, which can take 10-15 minutes. ",
                  "Leave this open.",
                ]),
                m(
                  "div",
                  { class: "mt-4 h-1.5 bg-fill-subtle rounded-full overflow-hidden" },
                  m("div", {
                    id: "ca-bar-fill",
                    class: "h-full rounded-full bg-accent transition-[width] duration-300 ease-out",
                    style: `width: ${progressPercent.toFixed(0)}%`,
                  }),
                ),
              ])
            : null,
          state.view === "result"
            ? m("div", { id: "ca-result-view" }, [
                m("span", { id: "ca-result-heading", class: "type-heading text-primary" }, state.resultHeading),
                m(
                  "p",
                  {
                    id: "ca-result-message",
                    class: "mt-2 type-helper text-secondary whitespace-pre-wrap break-words max-h-48 overflow-y-auto",
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
                  m(Button, { variant: "primary", id: "ca-result-done-btn", onclick: () => state.close() }, "Done"),
                ]),
              ])
            : null,
        ],
      );
    },
  };
}
