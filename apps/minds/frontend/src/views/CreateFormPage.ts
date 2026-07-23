// Workspace creation form. Two views share one form: the simple view offers
// two compute presets -- "remote" (Imbue Cloud) and "local" (directly on this
// computer) -- as large selectable cards; picking one fills the advanced
// selects with that preset's compute / AI / backup defaults. The advanced
// view exposes those selects plus the repository / branch / region inputs
// directly. The advanced selects are always the source of truth on submit,
// so toggling between views never changes what gets POSTed.
import m from "mithril";

import { normalizeApiError } from "../api_errors";
import type { CreateFormBootExtras, CreateFormBootIsland } from "../chrome_state";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses, SELECT_CLASSES, TEXT_INPUT_CLASSES } from "../ui";
import { Icon } from "./Icon";

// Each preset fills the advanced selects with its compute / AI / backup
// defaults. These mirror the per-account defaults the server applies.
const PRESETS: Record<string, { launch_mode: string; ai_provider: string; backup_provider: string }> = {
  remote: { launch_mode: "IMBUE_CLOUD", ai_provider: "IMBUE_CLOUD", backup_provider: "IMBUE_CLOUD" },
  local: { launch_mode: "LIMA", ai_provider: "SUBSCRIPTION", backup_provider: "CONFIGURE_LATER" },
};

const DEFAULT_RESTIC_ENV =
  "# see docs linked above for available options\n# for example:\n" +
  "RESTIC_REPOSITORY=s3:s3.amazonaws.com/<bucket_name>\nAWS_ACCESS_KEY_ID=\nAWS_SECRET_ACCESS_KEY=\n";

const HOST_NAME_DEBOUNCE_MS = 300;

type HostNameState = "ok" | "invalid" | "taken";

// Friendly message for the first broken host-name rule, or "" if valid.
// Mirrors mngr's HostName rules (alphanumeric plus '-'/'_' in the middle, no
// leading/trailing '-'/'_', no dots); empty is valid (auto-named server-side).
export function hostNameFormatError(value: string): string {
  if (value === "") return "";
  const invalidChar = value.match(/[^a-zA-Z0-9_-]/);
  if (invalidChar !== null) {
    if (invalidChar[0] === ".") return "Dots aren't allowed in a name.";
    if (invalidChar[0] === " ") return "Spaces aren't allowed in a name.";
    return "Use only letters, numbers, dashes, and underscores.";
  }
  if (/^[-_]/.test(value)) return "Can't start with a dash or underscore.";
  if (/[-_]$/.test(value)) return "Can't end with a dash or underscore.";
  return "";
}

class CreateFormController {
  readonly extras: CreateFormBootExtras;

  // Form state (the advanced selects are the submit source of truth).
  gitUrl: string;
  branch: string;
  hostName: string;
  accountId: string;
  launchMode: string;
  aiProvider: string;
  backupProvider: string;
  dockerRuntime: string;
  anthropicApiKey: string;
  backupApiKeyEnv: string;
  regionByMode: Record<string, string> = {};

  selectedPreset: string;
  isAdvancedShown: boolean;
  isAccountErrorShown = false;
  createError: string | null = null;
  errorFieldId: string | null = null;
  isSubmitting = false;

  // Live name validation. 'ok' lets submit through; 'invalid'/'taken' block
  // it. Availability is optimistic: until the server answers, the name is
  // treated as ok (the server's create-time conflict check backstops the
  // race).
  hostNameState: HostNameState = "ok";
  hostNameError: string | null = null;
  // The availability key (name + provider scope) a 'taken' was computed for;
  // stale verdicts must not fail-close a name/scope they never checked.
  private hostNameTakenKey: string | null = null;
  private hostNameDebounce: ReturnType<typeof setTimeout> | null = null;
  private hostNameCheckSeq = 0;

  constructor(extras: CreateFormBootExtras) {
    this.extras = extras;
    this.gitUrl = extras.git_url;
    this.branch = extras.branch;
    this.hostName = extras.host_name;
    this.accountId = extras.default_account_id;
    this.launchMode = extras.selected_launch_mode;
    this.aiProvider = extras.selected_ai_provider;
    this.backupProvider = extras.selected_backup_provider;
    this.dockerRuntime = extras.selected_docker_runtime;
    this.anthropicApiKey = extras.anthropic_api_key;
    this.backupApiKeyEnv = extras.backup_api_key_env !== "" ? extras.backup_api_key_env : DEFAULT_RESTIC_ENV;
    this.selectedPreset = extras.selected_preset;
    this.isAdvancedShown = extras.start_advanced;
  }

  stop(): void {
    if (this.hostNameDebounce !== null) {
      clearTimeout(this.hostNameDebounce);
      this.hostNameDebounce = null;
    }
  }

  hasAnyAccount(): boolean {
    return this.extras.accounts.length > 0;
  }

  // The region select is only shown for providers that place a machine in a
  // chosen region; null hides (and un-submits) it.
  regionOptions(): string[] | null {
    const options = this.extras.region_options_by_launch_mode[this.launchMode];
    if (options === undefined || options.length === 0) return null;
    return options;
  }

  selectedRegion(): string {
    const options = this.regionOptions();
    if (options === null) return "";
    // A user's explicit pick survives unrelated form changes; otherwise the
    // per-provider preselection (or the first option) applies.
    return this.regionByMode[this.launchMode] ?? this.extras.region_selected_by_launch_mode[this.launchMode] ?? options[0];
  }

  isDockerRuntimeShown(): boolean {
    return this.launchMode === "DOCKER";
  }

  // Imbue Cloud compute / AI / backup requires an account.
  imbueCloudNeedsAccount(): boolean {
    return (
      this.accountId === "" &&
      (this.launchMode === "IMBUE_CLOUD" || this.aiProvider === "IMBUE_CLOUD" || this.backupProvider === "IMBUE_CLOUD")
    );
  }

  pickPreset(name: string): void {
    // Clicking a card only selects it -- it never navigates. Remote compute
    // needs an account: when the user has signed-in accounts but none is
    // picked, auto-pick the first so it is immediately usable. When there is
    // no account at all, just select it; pressing "Create" opens the sign-in
    // modal (see submit).
    const preset = PRESETS[name];
    if (preset === undefined) return;
    if (name === "remote" && this.accountId === "" && this.hasAnyAccount()) {
      this.accountId = this.extras.accounts[0].user_id;
    }
    this.selectedPreset = name;
    this.launchMode = preset.launch_mode;
    this.aiProvider = preset.ai_provider;
    this.backupProvider = preset.backup_provider;
    this.afterSelectionChange();
  }

  afterSelectionChange(): void {
    // Clear a previously-shown picker error once the selection is valid again.
    if (!this.imbueCloudNeedsAccount()) this.isAccountErrorShown = false;
    // Provider / account / region changes move the goalposts for "taken".
    this.scheduleHostNameValidation();
    m.redraw();
  }

  private hostNameAvailabilityUrl(value: string): string {
    const params = new URLSearchParams({
      name: value,
      launch_mode: this.launchMode,
      account_id: this.accountId,
      region: this.regionOptions() === null ? "" : this.selectedRegion(),
    });
    return `/api/v1/desktop/host-name-available?${params.toString()}`;
  }

  scheduleHostNameValidation(): void {
    if (this.hostNameDebounce !== null) clearTimeout(this.hostNameDebounce);
    this.hostNameDebounce = setTimeout(() => void this.validateHostName(), HOST_NAME_DEBOUNCE_MS);
  }

  async validateHostName(): Promise<void> {
    const value = this.hostName.trim();
    const formatError = hostNameFormatError(value);
    if (formatError !== "") {
      this.hostNameState = "invalid";
      this.hostNameTakenKey = null;
      this.hostNameError = formatError;
      m.redraw();
      return;
    }
    if (value === "") {
      this.hostNameState = "ok";
      this.hostNameTakenKey = null;
      this.hostNameError = null;
      m.redraw();
      return;
    }
    // Format is fine; ask the server about availability, optimistically
    // treating the name as ok until it answers.
    this.hostNameState = "ok";
    this.hostNameTakenKey = null;
    this.hostNameError = null;
    m.redraw();
    const url = this.hostNameAvailabilityUrl(value);
    const seq = ++this.hostNameCheckSeq;
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      const data = response.ok ? ((await response.json()) as { available?: boolean }) : null;
      // Ignore a stale response superseded by a newer keystroke/change.
      if (seq !== this.hostNameCheckSeq) return;
      if (data !== null && data.available === false) {
        this.hostNameState = "taken";
        this.hostNameTakenKey = url;
        this.hostNameError = "That name is already taken. Pick a different one.";
      } else {
        this.hostNameState = "ok";
        this.hostNameTakenKey = null;
        this.hostNameError = null;
      }
      m.redraw();
    } catch {
      // Network error: fail open, leave the name usable.
    }
  }

  private openSigninModal(): void {
    if (window.minds !== undefined || window.__mindsOpenModal !== undefined) {
      // Empty return_to keeps the server's default (back to the create
      // screen); signup tab leads for a signed-out user.
      getHost().openModal({ kind: "signin", returnTo: "", mode: "signup" });
    } else {
      // Standalone page without the shell: navigate to the sign-in page.
      window.location.href = "/auth/signin-modal";
    }
  }

  submit(): void {
    // Imbue Cloud needs an account. If none is selected, don't submit:
    // signed out -> open the sign-in modal; signed in -> surface the
    // account-picker error (only now, on a Create attempt).
    if (this.imbueCloudNeedsAccount()) {
      if (this.hasAnyAccount()) {
        this.isAccountErrorShown = true;
        m.redraw();
      } else {
        this.openSigninModal();
      }
      return;
    }
    // Re-run format validation synchronously so a known-bad / already-taken
    // name blocks submit. Availability that hasn't come back yet does not
    // block (the server's create-time conflict check backstops that race).
    const trimmedName = this.hostName.trim();
    const formatError = hostNameFormatError(trimmedName);
    if (formatError !== "") {
      this.hostNameState = "invalid";
      this.hostNameError = formatError;
    } else if (this.hostNameState === "invalid") {
      this.hostNameState = "ok";
      this.hostNameError = null;
    }
    // A 'taken' verdict only blocks the exact name/scope it was computed for.
    const nameStillTaken =
      this.hostNameState === "taken" && this.hostNameTakenKey === this.hostNameAvailabilityUrl(trimmedName);
    if (this.hostNameState === "invalid" || nameStillTaken) {
      this.isAdvancedShown = true;
      m.redraw();
      document.getElementById("host_name")?.focus();
      return;
    }
    this.isSubmitting = true;
    m.redraw();
    void this.submitCreate();
  }

  private async submitCreate(): Promise<void> {
    this.createError = null;
    this.errorFieldId = null;
    const regionOptions = this.regionOptions();
    const body: Record<string, unknown> = {
      git_url: this.gitUrl.trim(),
      host_name: this.hostName.trim(),
      branch: this.branch.trim(),
      color: this.extras.color,
      launch_mode: this.launchMode,
      ai_provider: this.aiProvider,
      account_id: this.accountId,
      anthropic_api_key: this.aiProvider === "API_KEY" ? this.anthropicApiKey.trim() : "",
      backup_provider: this.backupProvider,
      backup_api_key_env: this.backupProvider === "API_KEY" ? this.backupApiKeyEnv : "",
      region: regionOptions === null ? "" : this.selectedRegion(),
      // The container runtime only applies to the local Docker provider;
      // omit it otherwise and let the server pick its platform default.
      runtime: this.isDockerRuntimeShown() ? this.dockerRuntime : undefined,
    };
    try {
      const response = await fetch("/api/v1/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await response.json().catch(() => ({}))) as { operation_id?: string };
      if (response.status === 202 && data.operation_id !== undefined) {
        window.location.href = `/creating/${data.operation_id}`;
        return;
      }
      // Normalize both the structural 422 contract and the handler's
      // semantic {error, field, redirect_url} into one shape.
      const normalized = normalizeApiError(data);
      if (normalized.redirectUrl !== null) {
        window.location.href = normalized.redirectUrl;
        return;
      }
      this.showCreateError(normalized.message, normalized.field);
    } catch {
      this.showCreateError("Could not reach the server. Please try again.", null);
    }
  }

  private showCreateError(message: string, fieldId: string | null): void {
    this.createError = message !== "" ? message : "Could not create the workspace.";
    this.errorFieldId = fieldId;
    this.isSubmitting = false;
    // When the API names the offending field, reveal the advanced view it
    // lives in and bring it into focus so the message has context.
    if (fieldId !== null) this.isAdvancedShown = true;
    m.redraw();
    if (fieldId !== null) {
      const fieldElement = document.getElementById(fieldId);
      fieldElement?.focus();
      fieldElement?.scrollIntoView({ block: "center" });
    }
  }
}

// -- Views -------------------------------------------------------------------

const PRESET_CARD_CLASSES =
  "flex flex-col gap-2 p-4 text-left cursor-pointer rounded-lg bg-surface-primary outline-1 outline-dashed " +
  "outline-strong transition-all duration-150 ease-out hover:-translate-y-px hover:shadow-raised " +
  "active:translate-y-0 active:scale-[0.99] aria-checked:outline-2 aria-checked:outline-solid " +
  "aria-checked:outline-accent flex-1";
const GHOST_LINK_CLASSES =
  buttonClasses("ghost") +
  " !p-0 !bg-transparent !type-helper !text-tertiary hover:!bg-transparent hover:!text-primary hover:underline whitespace-nowrap";
const FIELD_ERROR_RING = "!ring-1 ring-important";

function presetFeature(label: string, isAccent: boolean): m.Children {
  return m("li", { class: "flex items-start gap-2 type-body text-primary" }, [
    m(
      "span",
      { class: `${isAccent ? "text-accent " : ""}shrink-0 mt-0.5` },
      m(Icon, { name: isAccent ? "badge-check-filled" : "badge-check" }),
    ),
    label,
  ]);
}

function simpleView(controller: CreateFormController): m.Children {
  return m(
    "div",
    { id: "simple-view", role: "radiogroup", "aria-label": "Where to run your workspace" },
    m("div", { class: "flex gap-6 items-stretch" }, [
      m(
        "button",
        {
          type: "button",
          role: "radio",
          "data-preset": "remote",
          "aria-checked": controller.selectedPreset === "remote" ? "true" : "false",
          class: PRESET_CARD_CLASSES,
          onclick: () => controller.pickPreset("remote"),
        },
        [
          m("div", { class: "flex items-center gap-2" }, [
            m("span", { class: "type-heading text-primary" }, "Imbue Cloud"),
            m(
              "span",
              { class: "inline-flex items-center px-2 py-0.5 rounded-md type-helper uppercase font-bold bg-accent/15 text-accent" },
              "Recommended",
            ),
          ]),
          m("ul", { class: "flex flex-col gap-1.5 mt-1" }, [
            presetFeature("30 second setup", true),
            presetFeature("Runs even if your computer is off", true),
            presetFeature("Accessible from mobile", true),
            presetFeature("Shareable with other people", true),
          ]),
        ],
      ),
      m(
        "button",
        {
          type: "button",
          role: "radio",
          "data-preset": "local",
          "aria-checked": controller.selectedPreset === "local" ? "true" : "false",
          class: PRESET_CARD_CLASSES,
          onclick: () => controller.pickPreset("local"),
        },
        [
          m("span", { class: "type-heading text-primary" }, "Directly on your computer"),
          m("ul", { class: "flex flex-col gap-1.5 mt-1" }, [
            presetFeature("5-10 minute setup", false),
            presetFeature("Runs only when your computer is on", false),
            presetFeature("Uses your computer's memory (may slow your device)", false),
          ]),
        ],
      ),
    ]),
  );
}

function labeledSelect(
  controller: CreateFormController,
  id: string,
  value: string,
  options: Array<{ value: string; label: string; requiresAccount?: boolean }>,
  onchange: (value: string) => void,
): m.Children {
  const hasAccount = controller.accountId !== "";
  return m("div", { class: "relative w-48" }, [
    m(
      "select",
      {
        id,
        name: id,
        class: SELECT_CLASSES + (controller.errorFieldId === id ? ` ${FIELD_ERROR_RING}` : ""),
        value,
        onchange: (event: Event) => onchange((event.target as HTMLSelectElement).value),
      },
      options.map((option) =>
        m(
          "option",
          { value: option.value, disabled: option.requiresAccount === true && !hasAccount },
          option.label,
        ),
      ),
    ),
    m(
      "span",
      { class: "pointer-events-none absolute inset-y-0 right-2 flex items-center text-secondary" },
      m(Icon, { name: "chevron-down" }),
    ),
  ]);
}

function textInput(controller: CreateFormController, id: string, attrs: Record<string, unknown>): m.Children {
  return m("input", {
    id,
    name: id,
    type: "text",
    class: TEXT_INPUT_CLASSES + (controller.errorFieldId === id ? ` ${FIELD_ERROR_RING}` : ""),
    ...attrs,
  });
}

function advancedView(controller: CreateFormController): m.Children {
  const { extras } = controller;
  const regionOptions = controller.regionOptions();
  return m(
    "div",
    { id: "advanced-view", class: "mt-8" },
    m("div", { class: "mx-auto max-w-[560px] flex flex-col gap-4" }, [
      m("div", [
        m("label", { for: "host_name", class: "type-label text-primary block mb-1.5" }, "Name"),
        m("p", { class: "mb-1 type-helper text-tertiary" }, "Leave empty to name it automatically"),
        textInput(controller, "host_name", {
          value: controller.hostName,
          placeholder: "my-workspace",
          class:
            TEXT_INPUT_CLASSES +
            (controller.hostNameError !== null ? " !border-important focus:!outline-important" : ""),
          oninput: (event: Event) => {
            controller.hostName = (event.target as HTMLInputElement).value;
            controller.scheduleHostNameValidation();
          },
        }),
        controller.hostNameError !== null
          ? m("p", { id: "host-name-error", class: "mt-1 type-helper text-important" }, controller.hostNameError)
          : null,
      ]),
      m("div", [
        m("div", { class: "flex items-start justify-between gap-3" }, [
          m("div", [
            m("label", { for: "launch_mode", class: "type-label text-primary" }, "Compute provider"),
            controller.launchMode === "AWS"
              ? m("p", { id: "aws-credentials-note", class: "mt-1 type-helper text-tertiary" }, [
                  "AWS credentials are read from this machine's environment (",
                  m("code", "AWS_ACCESS_KEY_ID"),
                  " / ",
                  m("code", "AWS_SECRET_ACCESS_KEY"),
                  ", or ",
                  m("code", "AWS_PROFILE"),
                  ", or ",
                  m("code", "~/.aws/credentials"),
                  "). Pick a region below.",
                ])
              : null,
            controller.launchMode === "MODAL"
              ? m("p", { id: "modal-direct-note", class: "mt-1 type-helper text-tertiary" }, [
                  "Modal creates sandboxes from this machine using your own Modal token. If you haven't ",
                  "authenticated yet, install the CLI and log in: ",
                  m("code", "uv tool install modal"),
                  ", then ",
                  m("code", "modal token new"),
                  " (or set ",
                  m("code", "MODAL_TOKEN_ID"),
                  " / ",
                  m("code", "MODAL_TOKEN_SECRET"),
                  "). Sandboxes are ephemeral (~1 day) -- testing only.",
                ])
              : null,
          ]),
          labeledSelect(
            controller,
            "launch_mode",
            controller.launchMode,
            extras.launch_modes.map((mode) => ({
              value: mode,
              label: mode === "MODAL" ? "Modal (1-day ephemeral)" : mode.toLowerCase(),
              requiresAccount: mode === "IMBUE_CLOUD",
            })),
            (value) => {
              controller.launchMode = value;
              controller.afterSelectionChange();
            },
          ),
        ]),
        controller.accountId === "" && controller.launchMode === "IMBUE_CLOUD"
          ? m(
              "p",
              { id: "launch-mode-account-error", class: "mt-1 type-helper text-warning text-right" },
              "imbue_cloud requires a selected account.",
            )
          : null,
      ]),
      m("div", [
        m("div", { class: "flex items-center justify-between gap-3" }, [
          m("label", { for: "ai_provider", class: "type-label text-primary" }, "AI provider"),
          labeledSelect(
            controller,
            "ai_provider",
            controller.aiProvider,
            extras.ai_providers.map((provider) => ({
              value: provider,
              label: provider.toLowerCase(),
              requiresAccount: provider === "IMBUE_CLOUD",
            })),
            (value) => {
              controller.aiProvider = value;
              controller.afterSelectionChange();
            },
          ),
        ]),
        controller.accountId === "" && controller.aiProvider === "IMBUE_CLOUD"
          ? m(
              "p",
              { id: "ai-provider-account-error", class: "mt-1 type-helper text-warning text-right" },
              "imbue_cloud requires a selected account.",
            )
          : null,
        controller.aiProvider === "API_KEY"
          ? m("div", { id: "api-key-row", class: "mt-2" }, [
              m("label", { for: "anthropic_api_key", class: "type-label text-primary block mb-1.5" }, "Anthropic API key"),
              textInput(controller, "anthropic_api_key", {
                type: "password",
                value: controller.anthropicApiKey,
                placeholder: "sk-ant-...",
                required: true,
                oninput: (event: Event) => {
                  controller.anthropicApiKey = (event.target as HTMLInputElement).value;
                },
              }),
            ])
          : null,
      ]),
      m("div", [
        m("div", { class: "flex items-center justify-between gap-3" }, [
          m("label", { for: "backup_provider", class: "type-label text-primary" }, "Backup provider"),
          labeledSelect(
            controller,
            "backup_provider",
            controller.backupProvider,
            extras.backup_providers.map((provider) => ({
              value: provider,
              label: provider === "API_KEY" ? "manual" : provider.toLowerCase(),
              requiresAccount: provider === "IMBUE_CLOUD",
            })),
            (value) => {
              controller.backupProvider = value;
              controller.afterSelectionChange();
            },
          ),
        ]),
        controller.accountId === "" && controller.backupProvider === "IMBUE_CLOUD"
          ? m(
              "p",
              { id: "backup-provider-account-error", class: "mt-1 type-helper text-warning text-right" },
              "imbue_cloud requires a selected account.",
            )
          : null,
        controller.backupProvider === "API_KEY"
          ? m("div", { id: "backup-api-key-row", class: "mt-2" }, [
              m("label", { for: "backup_api_key_env", class: "type-label text-primary block mb-1.5" }, "restic environment"),
              m("p", { class: "mb-1 type-helper text-tertiary" }, [
                "Written verbatim to ",
                m("code", "restic.env"),
                ". Don't set ",
                m("code", "RESTIC_PASSWORD"),
                " -- minds assigns each workspace its own. See the ",
                m(
                  "a",
                  {
                    href: "https://restic.readthedocs.io/en/stable/040_backup.html#environment-variables",
                    target: "_blank",
                    rel: "noopener",
                    class: "text-accent hover:underline",
                  },
                  "restic docs",
                ),
                ".",
              ]),
              m("textarea", {
                id: "backup_api_key_env",
                name: "backup_api_key_env",
                rows: 6,
                class: `w-full ${TEXT_INPUT_CLASSES.replace("w-full leading-tight ", "")} font-mono`,
                value: controller.backupApiKeyEnv,
                oninput: (event: Event) => {
                  controller.backupApiKeyEnv = (event.target as HTMLTextAreaElement).value;
                },
              }),
            ])
          : null,
      ]),
      regionOptions !== null
        ? m("div", { id: "region-row" }, [
            m("div", { class: "flex items-start justify-between gap-3" }, [
              m("div", [
                m("label", { for: "region", class: "type-label text-primary" }, "Region"),
                m(
                  "p",
                  { class: "mt-1 type-helper text-tertiary" },
                  "Where the machine is created. If a region is out of capacity, try another.",
                ),
              ]),
              labeledSelect(
                controller,
                "region",
                controller.selectedRegion(),
                regionOptions.map((region) => ({ value: region, label: region })),
                (value) => {
                  controller.regionByMode[controller.launchMode] = value;
                  controller.afterSelectionChange();
                },
              ),
            ]),
          ])
        : null,
      controller.isDockerRuntimeShown()
        ? m("div", { id: "runtime-row" }, [
            m("div", { class: "flex items-center justify-between gap-3" }, [
              m("label", { for: "runtime", class: "type-label text-primary" }, "Container runtime"),
              labeledSelect(
                controller,
                "runtime",
                controller.dockerRuntime,
                extras.docker_runtimes.map((runtime) => ({ value: runtime, label: runtime.toLowerCase() })),
                (value) => {
                  controller.dockerRuntime = value;
                },
              ),
            ]),
          ])
        : null,
      m("hr", { class: "border-t border-dashed border-default my-2" }),
      m("div", [
        m("label", { for: "git_url", class: "type-label text-primary block mb-1.5" }, "Repository"),
        m("p", { class: "mb-1 type-helper text-tertiary" }, "Git URL or local path"),
        textInput(controller, "git_url", {
          value: controller.gitUrl,
          placeholder: "https://github.com/user/repo.git",
          required: true,
          oninput: (event: Event) => {
            controller.gitUrl = (event.target as HTMLInputElement).value;
          },
        }),
      ]),
      m("div", [
        m("label", { for: "branch", class: "type-label text-primary block mb-1.5" }, "Branch"),
        m("p", { class: "mb-1 type-helper text-tertiary" }, "Leave empty for latest version"),
        textInput(controller, "branch", {
          value: controller.branch,
          placeholder: "latest tag",
          oninput: (event: Event) => {
            controller.branch = (event.target as HTMLInputElement).value;
          },
        }),
      ]),
    ]),
  );
}

interface CreateFormPageAttrs {
  controller: CreateFormController;
}

function CreateFormPage(): m.Component<CreateFormPageAttrs> {
  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      const { extras } = controller;
      return m(
        "form",
        {
          id: "create-form",
          onsubmit: (event: Event) => {
            // Always handle submission via fetch, never a native POST.
            event.preventDefault();
            controller.submit();
          },
        },
        [
          m("div", { class: "text-center mb-12" }, [
            m("p", { class: "type-label uppercase tracking-wide text-secondary" }, "Create a workspace"),
            m("h1", { class: "type-heading-lg text-primary mt-1" }, "Where should it run?"),
          ]),
          extras.error_message !== ""
            ? m(
                "div",
                { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-important-surface)] text-important" },
                extras.error_message,
              )
            : null,
          controller.createError !== null
            ? m(
                "p",
                { id: "create-error", role: "alert", class: "mb-6 type-helper text-important text-center" },
                controller.createError,
              )
            : null,
          controller.isAdvancedShown ? null : simpleView(controller),
          m("div", { class: "flex items-center justify-between mt-8 type-helper" }, [
            m(
              "select",
              {
                id: "account_id",
                name: "account_id",
                class:
                  "max-w-[220px] type-helper text-tertiary bg-transparent border-0 rounded-md px-1 py-1 -ml-1 " +
                  "outline-none cursor-pointer hover:text-primary focus:outline-none focus:ring-0" +
                  (controller.isAccountErrorShown ? ` ${FIELD_ERROR_RING}` : ""),
                value: controller.accountId,
                onchange: (event: Event) => {
                  controller.accountId = (event.target as HTMLSelectElement).value;
                  controller.afterSelectionChange();
                },
              },
              [
                ...extras.accounts.map((account) => m("option", { value: account.user_id }, account.email)),
                m("option", { value: "" }, "No account (private workspace)"),
              ],
            ),
            controller.isAdvancedShown
              ? m(
                  "span",
                  { id: "simple-toggle-wrap" },
                  m(
                    "button",
                    {
                      type: "button",
                      id: "back-to-simple",
                      class: GHOST_LINK_CLASSES,
                      onclick: () => {
                        controller.isAdvancedShown = false;
                        m.redraw();
                      },
                    },
                    "↑ Back to simple configuration",
                  ),
                )
              : m(
                  "span",
                  { id: "advanced-toggle-wrap" },
                  m(
                    "button",
                    {
                      type: "button",
                      id: "toggle-advanced",
                      class: GHOST_LINK_CLASSES,
                      onclick: () => {
                        controller.isAdvancedShown = true;
                        m.redraw();
                      },
                    },
                    "Advanced Configuration",
                  ),
                ),
          ]),
          controller.isAccountErrorShown
            ? m(
                "p",
                { id: "account-error", class: "mt-2 type-helper text-important text-center" },
                "Pick an account to run on Imbue Cloud, or choose a different option for the providers.",
              )
            : null,
          controller.isAdvancedShown ? advancedView(controller) : null,
          m("div", { class: "flex justify-center mt-16" }, [
            m(
              "button",
              {
                type: "submit",
                id: "create-submit",
                class: `${buttonClasses("primary")} w-80`,
                disabled: controller.isSubmitting,
              },
              controller.isSubmitting ? "Creating..." : "Create",
            ),
          ]),
        ],
      );
    },
  };
}

export function mountCreateForm(target: Element | null): void {
  const el = requireElement(target, "create form container");
  const island = readBootState() as CreateFormBootIsland;
  if (island.create === undefined) {
    throw new MindsUIError("create boot island is missing the create slice");
  }
  const controller = new CreateFormController(island.create);
  mountWithTeardown(el, {
    view: () => m(CreateFormPage, { controller }),
    onremove: () => controller.stop(),
  });
  // Validate any pre-filled name on load (e.g. a re-rendered submit error).
  void controller.validateHostName();
}
