// The create form (port of Create.jinja + its inline script): two preset
// cards backed by an advanced view whose selects are always the source of
// truth on submit. Submission POSTs the existing /api/v1/workspaces front
// door and routes to /creating/<operation_id>. DOM ids match the legacy form
// (the e2e workspace runner drives them).

import m from "mithril";
import type { CreateFormDefaults } from "../../models/create";
import { fetchCreateFormDefaults } from "../../models/create";
import { Button, ButtonSubmit } from "../components/Button";
import { FormLabel, Select, TextInput, Textarea } from "../components/FormControls";
import { Link } from "../components/Link";
import { PageNarrowContainer } from "../components/Layout";
import { CloudAccountsModal, CloudAccountsModalState } from "./create/CloudAccountsModal";
import { PresetCards } from "./create/PresetCards";
import type { PresetName } from "./create/form-model";
import { CreateFormModel, normalizeCreateApiError } from "./create/form-model";

export const CreatePage: m.ClosureComponent = () => {
  const model = new CreateFormModel();
  const byokModal = new CloudAccountsModalState();
  let hostNameDebounce: ReturnType<typeof setTimeout> | null = null;
  let availabilitySequence = 0;

  function scheduleHostNameValidation(): void {
    if (hostNameDebounce !== null) clearTimeout(hostNameDebounce);
    hostNameDebounce = setTimeout(() => {
      model.validateHostNameLive();
      const trimmed = model.hostName.trim();
      if (trimmed === "" || model.hostNameState === "invalid") {
        m.redraw();
        return;
      }
      const url = model.hostNameAvailabilityUrl();
      const sequence = ++availabilitySequence;
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then((response) => (response.ok ? (response.json() as Promise<{ available?: boolean }>) : null))
        .then((data) => {
          if (sequence !== availabilitySequence || data === null) return;
          model.applyAvailabilityVerdict(url, data.available !== false);
          m.redraw();
        })
        .catch(() => undefined);
    }, 300);
  }

  function submit(event: SubmitEvent): void {
    event.preventDefault();
    if (model.imbueCloudNeedsAccount()) {
      if ((model.defaults?.accounts.length ?? 0) > 0) {
        model.isAccountErrorShown = true;
      } else {
        // Signed out entirely: sign-in lives on the Accounts page now (the
        // overlay sign-in modal is the accounts tranche's surface).
        m.route.set("/accounts");
      }
      return;
    }
    if (!model.validateHostNameFormatForSubmit()) {
      model.isAdvancedOpen = true;
      return;
    }
    model.isSubmitting = true;
    model.submitError = "";
    model.submitErrorField = "";
    fetch("/api/v1/workspaces", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(model.submitBody()),
    })
      .then(async (response) => ({
        status: response.status,
        data: (await response.json().catch(() => ({}))) as Record<string, unknown>,
      }))
      .then((result) => {
        if (result.status === 202 && typeof result.data.operation_id === "string") {
          m.route.set(`/creating/${result.data.operation_id}`);
          return;
        }
        const error = normalizeCreateApiError(result.data);
        if (error.redirectUrl) {
          window.location.href = error.redirectUrl;
          return;
        }
        model.isSubmitting = false;
        model.submitError = error.message;
        model.submitErrorField = error.field;
        if (error.field) model.isAdvancedOpen = true;
        m.redraw();
      })
      .catch(() => {
        model.isSubmitting = false;
        model.submitError = "Could not reach the server. Please try again.";
        m.redraw();
      });
  }

  function fieldRing(fieldId: string): string {
    return model.submitErrorField === fieldId ? " !ring-1 ring-important" : "";
  }

  function advancedView(defaults: CreateFormDefaults): m.Children {
    const regionOptions = model.regionOptions();
    const instanceTypeOptions = model.instanceTypeOptions();
    const byokPinnedRegion = model.byokPinnedRegion();
    return m("div", { id: "advanced-view", class: model.isAdvancedOpen ? "mt-8" : "hidden mt-8" }, [
      m("div", { class: "mx-auto max-w-[560px] flex flex-col gap-4" }, [
        m("div", [
          m(FormLabel, { target: "host_name" }, "Name"),
          m("p", { class: "mb-1 type-helper text-tertiary" }, "Leave empty to name it automatically"),
          m(TextInput, {
            id: "host_name",
            name: "host_name",
            placeholder: "my-machine",
            value: model.hostName,
            extra: model.hostNameError ? "!border-important focus:!outline-important" : "",
            oninput: (event: InputEvent) => {
              model.hostName = (event.target as HTMLInputElement).value;
              scheduleHostNameValidation();
            },
          }),
          model.hostNameError
            ? m("p", { id: "host-name-error", class: "mt-1 type-helper text-important" }, model.hostNameError)
            : m("p", { id: "host-name-error", class: "hidden" }),
        ]),
        m("div", [
          m("div", { class: "flex items-start justify-between gap-3" }, [
            m("div", [
              m(FormLabel, { target: "launch_mode", inline: true }, "Compute provider"),
              model.launchValue === "MODAL"
                ? m("p", { id: "modal-direct-note", class: "mt-1 type-helper text-tertiary" }, [
                    "Modal creates sandboxes from this machine using your own Modal token. Sandboxes are ",
                    "ephemeral (~1 day) -- testing only.",
                  ])
                : null,
            ]),
            m(
              Select,
              {
                id: "launch_mode",
                name: "launch_mode",
                width: "w-48",
                extra: fieldRing("launch_mode"),
                value: model.launchValue,
                onchange: (event: Event) => {
                  const value = (event.target as HTMLSelectElement).value;
                  if (value.startsWith("ADD:")) {
                    byokModal.openForBackend(value.slice(4), defaults.region_selected_by_launch_mode);
                    // Revert the select to the previous real selection.
                    (event.target as HTMLSelectElement).value = model.launchValue;
                    return;
                  }
                  model.launchValue = value;
                  model.selectedPreset = null;
                  scheduleHostNameValidation();
                },
              },
              [
                defaults.launch_modes.map((mode) =>
                  m(
                    "option",
                    {
                      value: mode,
                      selected: model.launchValue === mode,
                      disabled: mode === "IMBUE_CLOUD" && model.accountId === "" && defaults.accounts.length === 0,
                    },
                    mode === "MODAL" ? "Modal (1-day ephemeral)" : mode.toLowerCase(),
                  ),
                ),
                defaults.byok_clouds_enabled
                  ? [
                      defaults.cloud_accounts.map((account) =>
                        m(
                          "option",
                          { value: `BYOK:${account.name}`, selected: model.launchValue === `BYOK:${account.name}` },
                          `${account.alias} (${account.backend.toUpperCase()} account)`,
                        ),
                      ),
                      m("option", { value: "ADD:aws" }, "add AWS account…"),
                      m("option", { value: "ADD:gcp" }, "add GCP account…"),
                      m("option", { value: "ADD:azure" }, "add Azure account…"),
                    ]
                  : null,
              ],
            ),
          ]),
          model.accountId === "" && model.launchValue === "IMBUE_CLOUD"
            ? m(
                "p",
                { id: "launch-mode-account-error", class: "mt-1 type-helper text-warning text-right" },
                "imbue_cloud requires a selected account.",
              )
            : null,
          byokPinnedRegion !== ""
            ? m(
                "p",
                { id: "byok-region-note", class: "mt-1 type-helper text-tertiary" },
                `Runs in ${byokPinnedRegion} — fixed when this account was added. For a different region, add ` +
                  "another account entry with the same keys.",
              )
            : null,
        ]),
        m("div", [
          m("div", { class: "flex items-center justify-between gap-3" }, [
            m(FormLabel, { target: "backup_provider", inline: true }, "Backup provider"),
            m(
              Select,
              {
                id: "backup_provider",
                name: "backup_provider",
                width: "w-48",
                extra: fieldRing("backup_provider"),
                value: model.backupProvider,
                onchange: (event: Event) => {
                  model.backupProvider = (event.target as HTMLSelectElement).value;
                  model.selectedPreset = null;
                },
              },
              defaults.backup_providers.map((provider) =>
                m(
                  "option",
                  {
                    value: provider,
                    selected: model.backupProvider === provider,
                    disabled: provider === "IMBUE_CLOUD" && model.accountId === "" && defaults.accounts.length === 0,
                  },
                  provider === "API_KEY" ? "manual" : provider.toLowerCase(),
                ),
              ),
            ),
          ]),
          model.accountId === "" && model.backupProvider === "IMBUE_CLOUD"
            ? m(
                "p",
                { id: "backup-provider-account-error", class: "mt-1 type-helper text-warning text-right" },
                "imbue_cloud requires a selected account.",
              )
            : null,
          model.backupProvider === "API_KEY"
            ? m("div", { id: "backup-api-key-row", class: "mt-2" }, [
                m(FormLabel, { target: "backup_api_key_env" }, "restic environment"),
                m("p", { class: "mb-1 type-helper text-tertiary" }, [
                  "Written verbatim to restic.env. Don't set RESTIC_PASSWORD -- minds assigns each machine its ",
                  "own. See the ",
                  m(
                    Link,
                    {
                      href: "https://restic.readthedocs.io/en/stable/040_backup.html#environment-variables",
                      target: "_blank",
                      rel: "noopener",
                    },
                    "restic docs",
                  ),
                  ".",
                ]),
                m(Textarea, {
                  id: "backup_api_key_env",
                  name: "backup_api_key_env",
                  rows: 6,
                  extra: "font-mono",
                  value: model.backupApiKeyEnv,
                  oninput: (event: InputEvent) => {
                    model.backupApiKeyEnv = (event.target as HTMLTextAreaElement).value;
                  },
                }),
              ])
            : null,
        ]),
        regionOptions.length > 0
          ? m("div", { id: "region-row" }, [
              m("div", { class: "flex items-start justify-between gap-3" }, [
                m("div", [
                  m(FormLabel, { target: "region", inline: true }, "Region"),
                  m(
                    "p",
                    { class: "mt-1 type-helper text-tertiary" },
                    "Where the machine is created. If a region is out of capacity, try another.",
                  ),
                ]),
                m(
                  Select,
                  {
                    id: "region",
                    name: "region",
                    width: "w-48",
                    extra: fieldRing("region"),
                    value: model.selectedRegion(),
                    onchange: (event: Event) => {
                      model.setRegion((event.target as HTMLSelectElement).value);
                      scheduleHostNameValidation();
                    },
                  },
                  regionOptions.map((region) =>
                    m("option", { value: region, selected: region === model.selectedRegion() }, region),
                  ),
                ),
              ]),
            ])
          : null,
        instanceTypeOptions.length > 0
          ? m("div", { id: "instance-type-row" }, [
              m("div", { class: "flex items-start justify-between gap-3" }, [
                m("div", [
                  m(FormLabel, { target: "instance_type", inline: true }, "Machine size"),
                  m(
                    "p",
                    { class: "mt-1 type-helper text-tertiary" },
                    "Billed to your account while the machine runs. 8 GB is the tested default.",
                  ),
                ]),
                m(
                  Select,
                  {
                    id: "instance_type",
                    name: "instance_type",
                    width: "w-80",
                    value: model.selectedInstanceType(),
                    onchange: (event: Event) => {
                      model.setInstanceType((event.target as HTMLSelectElement).value);
                    },
                  },
                  instanceTypeOptions.map(([value, label]) =>
                    m("option", { value, selected: value === model.selectedInstanceType() }, label),
                  ),
                ),
              ]),
            ])
          : null,
        model.isRuntimeShown()
          ? m("div", { id: "runtime-row" }, [
              m("div", { class: "flex items-center justify-between gap-3" }, [
                m(FormLabel, { target: "runtime", inline: true }, "Container runtime"),
                m(
                  Select,
                  {
                    id: "runtime",
                    name: "runtime",
                    width: "w-48",
                    value: model.runtime,
                    onchange: (event: Event) => {
                      model.runtime = (event.target as HTMLSelectElement).value;
                    },
                  },
                  defaults.docker_runtimes.map((runtime) =>
                    m("option", { value: runtime, selected: runtime === model.runtime }, runtime.toLowerCase()),
                  ),
                ),
              ]),
            ])
          : null,
        m("hr", { class: "border-t border-dashed border-default my-2" }),
        m("div", [
          m(FormLabel, { target: "git_url" }, "Repository"),
          m("p", { class: "mb-1 type-helper text-tertiary" }, "Git URL or local path"),
          m(TextInput, {
            id: "git_url",
            name: "git_url",
            placeholder: "https://github.com/user/repo.git",
            required: true,
            extra: fieldRing("git_url"),
            value: model.gitUrl,
            oninput: (event: InputEvent) => {
              model.gitUrl = (event.target as HTMLInputElement).value;
            },
          }),
        ]),
        m("div", [
          m(FormLabel, { target: "branch" }, "Branch"),
          m("p", { class: "mb-1 type-helper text-tertiary" }, "Leave empty for latest version"),
          m(TextInput, {
            id: "branch",
            name: "branch",
            placeholder: "latest tag",
            value: model.branch,
            oninput: (event: InputEvent) => {
              model.branch = (event.target as HTMLInputElement).value;
            },
          }),
        ]),
      ]),
    ]);
  }

  return {
    oninit() {
      const retryId = m.route.param("retry") ?? null;
      fetchCreateFormDefaults(retryId)
        .then((defaults) => {
          model.applyDefaults(defaults);
          m.redraw();
        })
        .catch(() => {
          model.loadError = "Could not load the create form. Please try again.";
          m.redraw();
        });
      // Deep-links that pre-fill a repo/branch open the advanced view.
      const gitUrl = m.route.param("git_url");
      const branch = m.route.param("branch");
      if (gitUrl) model.gitUrl = gitUrl;
      if (branch) model.branch = branch;
      if (gitUrl || branch) {
        model.isAdvancedOpen = true;
        model.selectedPreset = null;
      }
    },
    onremove() {
      if (hostNameDebounce !== null) clearTimeout(hostNameDebounce);
    },
    view() {
      const defaults = model.defaults;
      return m(
        PageNarrowContainer,
        { padding: "form", maxWidth: "max-w-[720px]" },
        defaults === null
          ? m("p", { class: "type-helper text-tertiary text-center pt-24" }, model.loadError || "Loading…")
          : m(
              "form",
              {
                id: "create-form",
                onsubmit: submit,
                // Native validation cannot surface a bubble on a control the
                // simple view keeps hidden (Chrome blocks the submit with a
                // console-only "not focusable" error), so reveal the advanced
                // view whenever a control in it is flagged invalid.
                oncreate: (vnode: { dom: Element }) => {
                  vnode.dom.addEventListener(
                    "invalid",
                    () => {
                      if (!model.isAdvancedOpen) {
                        model.isAdvancedOpen = true;
                        m.redraw();
                      }
                    },
                    true,
                  );
                },
              },
              [
                m("div", { class: "text-center mb-12" }, [
                  m("p", { class: "type-label uppercase tracking-wide text-secondary" }, "Create a machine"),
                  m("h1", { class: "type-heading-lg text-primary mt-1" }, "Where should it run?"),
                ]),
                model.submitError
                  ? m(
                      "p",
                      { id: "create-error", role: "alert", class: "mb-6 type-helper text-important text-center" },
                      model.submitError,
                    )
                  : m("p", { id: "create-error", class: "hidden" }),
                m(
                  "div",
                  {
                    id: "simple-view",
                    role: "radiogroup",
                    "aria-label": "Where to run your machine",
                    class: model.isAdvancedOpen ? "hidden" : "",
                  },
                  m(PresetCards, {
                    selectedPreset: model.selectedPreset,
                    onSelect: (name: PresetName) => {
                      model.applyPreset(name);
                      scheduleHostNameValidation();
                    },
                  }),
                ),
                m("div", { class: "flex items-center justify-between mt-8 type-helper" }, [
                  m(
                    "select",
                    {
                      id: "account_id",
                      name: "account_id",
                      class:
                        "max-w-[220px] type-helper text-tertiary bg-transparent border-0 rounded-md px-1 py-1 -ml-1 " +
                        "outline-none cursor-pointer hover:text-primary focus:outline-none focus:ring-0" +
                        (model.isAccountErrorShown ? " !ring-1 ring-important" : ""),
                      value: model.accountId,
                      onchange: (event: Event) => {
                        model.accountId = (event.target as HTMLSelectElement).value;
                        if (!model.imbueCloudNeedsAccount()) model.isAccountErrorShown = false;
                        scheduleHostNameValidation();
                      },
                    },
                    [
                      defaults.accounts.map((account) =>
                        m(
                          "option",
                          { value: account.user_id, selected: model.accountId === account.user_id },
                          account.email,
                        ),
                      ),
                      m("option", { value: "", selected: model.accountId === "" }, "No account (private machine)"),
                    ],
                  ),
                  !model.isAdvancedOpen
                    ? m(
                        "span",
                        { id: "advanced-toggle-wrap" },
                        m(
                          Button,
                          {
                            variant: "ghost",
                            id: "toggle-advanced",
                            extra:
                              "!p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent " +
                              "hover:!text-primary hover:underline whitespace-nowrap",
                            onclick: () => {
                              model.isAdvancedOpen = true;
                            },
                          },
                          "Advanced Configuration",
                        ),
                      )
                    : m(
                        "span",
                        { id: "simple-toggle-wrap" },
                        m(
                          Button,
                          {
                            variant: "ghost",
                            id: "back-to-simple",
                            extra:
                              "!p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent " +
                              "hover:!text-primary hover:underline whitespace-nowrap",
                            onclick: () => {
                              model.isAdvancedOpen = false;
                            },
                          },
                          "Back to simple configuration",
                        ),
                      ),
                ]),
                model.isAccountErrorShown
                  ? m(
                      "p",
                      { id: "account-error", class: "mt-2 type-helper text-important text-center" },
                      "Pick an account to run on Imbue Cloud, or choose a different option for the providers.",
                    )
                  : m("p", { id: "account-error", class: "hidden" }),
                advancedView(defaults),
                m(
                  "div",
                  { class: "flex justify-center mt-16" },
                  m(
                    ButtonSubmit,
                    { id: "create-submit", extra: "w-80", disabled: model.isSubmitting },
                    model.isSubmitting ? "Creating..." : "Create",
                  ),
                ),
                m(CloudAccountsModal, {
                  state: byokModal,
                  regionOptionsByBackend: defaults.region_options_by_launch_mode,
                  onAccountAdded: (account) => {
                    defaults.cloud_accounts.push(account);
                    model.launchValue = `BYOK:${account.name}`;
                    model.selectedPreset = null;
                    scheduleHostNameValidation();
                  },
                }),
              ],
            ),
      );
    },
  };
};
