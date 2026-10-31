// State machine for the create form (the SPA port of Create.jinja's inline
// script). Pure state + injected fetchers so vitest can drive every branch;
// the CreatePage view renders from it and forwards DOM events.

import type {
  CloudAccountOption,
  CreateAccountOption,
  CreateFormDefaults,
  LocalBackendPrerequisite,
} from "../../../models/create";
import {
  connectionBackendLabelFor,
  hostNameFormatError,
  launchModeLabel,
} from "../../../models/create";
import type { MachineSizeDimension, MachineSizeDraft, MachineSizeNumbers } from "../../../models/machineSize";
import {
  MACHINE_SIZE_DIMENSIONS,
  machineSizeDraftFor,
  parseMachineSizeNumber,
} from "../../../models/machineSize";
import {
  LOCAL_PROFILE_USER_ID,
  connectionLaunchLabel,
} from "../../../models/providers";

/** The launch select value of a connection option: the prefix plus the connection instance name. */
export const CONNECTION_LAUNCH_PREFIX = "CONNECTION:";

// Modal's two placement choices that are not region names (the server lists
// them ahead of the regions and defaults to "any": no placement constraint,
// which is Modal's cheapest option).
const REGION_CHOICE_LABELS: Record<string, string> = {
  any: "Any (cheapest)",
  nearest: "Nearest to me",
};

/** The label the region picker shows for a choice: the placement choices are spelled out, a region is its code. */
export function regionChoiceLabel(choice: string): string {
  return REGION_CHOICE_LABELS[choice] ?? choice;
}

/** Appended to a this-device-only connection's label when it is offered under an account. */
export const LOCAL_CONNECTION_SUFFIX = " (this device only)";

interface LaunchOption {
  value: string;
  label: string;
  isDisabled: boolean;
}

export type PresetName = "remote" | "local" | "own-cloud";

/**
 * The local backend a compute mode depends on, or null for the cloud modes.
 * The container runtime's own requirement (runsc) is annotated separately,
 * under the runtime selector.
 */
function localPrerequisiteKeyFor(
  launchMode: string,
): LocalBackendPrerequisite["key"] | null {
  if (launchMode === "LIMA") return "LIMA";
  if (launchMode === "DOCKER") return "DOCKER";
  return null;
}

// Seeded into the restic-env textarea so what the user sees is exactly what
// submit sends (the view renders model state verbatim; no display-only
// fallback).
export const DEFAULT_RESTIC_ENV =
  "# see docs linked above for available options\n" +
  "# for example:\n" +
  "RESTIC_REPOSITORY=s3:s3.amazonaws.com/<bucket_name>\n" +
  "AWS_ACCESS_KEY_ID=\n" +
  "AWS_SECRET_ACCESS_KEY=\n";

// The backup provider the form falls to when no account is selected: Imbue
// backups need one, and the server refuses a create that asks without.
const BACKUP_PROVIDER_WITHOUT_ACCOUNT = "CONFIGURE_LATER";

/** Whether picking a preset opens the advanced view: your own cloud's compute provider and region are chosen there. */
export function presetOpensAdvanced(name: PresetName): boolean {
  return name === "own-cloud";
}

export interface LaunchSelection {
  mode: string;
  cloudAccount: string;
}

export interface CreateSubmitBody {
  git_url: string;
  host_name: string;
  branch: string;
  color: string;
  launch_mode: string;
  cloud_account: string;
  account_id: string;
  backup_provider: string;
  backup_api_key_env: string;
  region: string;
  instance_type: string;
  enable_web_access: boolean;
  is_onboarding_driven_by_agent: boolean;
  runtime?: string;
  cpu_count?: number;
  memory_gb?: number;
  disk_gb?: number;
}

export interface NormalizedApiError {
  message: string;
  field: string;
  redirectUrl: string;
}

/** Normalize the create API's error shapes ({error, field}, spectree 422
 * bodies, and the no-account {error, redirect_url} backstop) into one form.
 * `fallbackMessage` names the operation when the body carries no message. */
export function normalizeCreateApiError(
  data: unknown,
  fallbackMessage = "Could not create the workspace.",
): NormalizedApiError {
  const body = (data ?? {}) as Record<string, unknown>;
  const message =
    typeof body.error === "string"
      ? body.error
      : typeof body.detail === "string"
        ? body.detail
        : fallbackMessage;
  return {
    message,
    field: typeof body.field === "string" ? body.field : "",
    redirectUrl: typeof body.redirect_url === "string" ? body.redirect_url : "",
  };
}

export class CreateFormModel {
  defaults: CreateFormDefaults | null = null;
  loadError = "";
  /** Why a retry prefill was not applied (its connection is gone); "" when there was none or it was applied. */
  retryUnavailableReason = "";

  gitUrl = "";
  branch = "";
  hostName = "";
  color = "";
  // The raw compute select value: a LaunchMode value or "CONNECTION:<instance>".
  launchValue = "IMBUE_CLOUD";
  backupProvider = "IMBUE_CLOUD";
  backupApiKeyEnv = "";
  accountId = "";
  runtime = "";
  // The share panel's "Enable sharing and web access" switch, thrown for the
  // new workspace right after its create. On by default under an account
  // (syncAccountDefaults), off without one, since a share needs an owner.
  enableWebAccess = false;
  // The advanced view's "Show onboarding examples?": whether the creation page
  // shows its recording while the workspace is built. Off from the form; the
  // start flow's creates turn it on (its form opens with it on).
  isOnboardingDrivenByAgent = false;
  // Whether the user set the switch or the backup provider by hand: an
  // explicit pick survives account changes, a default follows the account.
  private isWebAccessTouched = false;
  private isBackupProviderTouched = false;
  selectedPreset: PresetName | null = "remote";
  isAdvancedOpen = false;

  // Region / machine-size picks are keyed by the raw select value so a user's
  // explicit pick survives unrelated changes but rebuilds on provider switch.
  private regionByLaunchValue = new Map<string, string>();
  private instanceTypeByLaunchValue = new Map<string, string>();
  // The numeric size fields (DOCKER / LIMA / MODAL), keyed the same way; a
  // mode starts from the defaults the server offers for it.
  private machineSizeByLaunchValue = new Map<string, MachineSizeDraft>();
  // One-shot pre-fill (a retry) for whichever mode the retry targeted.
  private machineSizePreselect: MachineSizeDraft | null = null;
  // One-shot pre-fill (a retry): honored by the first populate, then cleared.
  private instanceTypePreselect = "";

  hostNameState: "ok" | "invalid" | "taken" = "ok";
  hostNameError = "";
  hostNameTakenKey: string | null = null;

  isSubmitting = false;
  submitError = "";
  submitErrorField = "";

  applyDefaults(defaults: CreateFormDefaults): void {
    this.defaults = defaults;
    this.color = defaults.color;
    this.accountId = defaults.account?.user_id ?? "";
    this.backupProvider = defaults.selected_backup_provider;
    this.runtime = defaults.selected_docker_runtime;
    // Seed the repository/branch defaults (the shipped template, or the
    // operator's local worktree under just minds-start). A deep-link that
    // already set an explicit repository wins, and keeps its own branch
    // semantics: blank there means "the repo's latest version", so the
    // default branch is only paired with the default repository.
    if (this.gitUrl === "") {
      this.gitUrl = defaults.git_url;
      if (this.branch === "") {
        this.branch = defaults.branch;
      }
    }
    const prefill = defaults.prefill;
    // An attempt whose connection is gone cannot be retried as recorded, and
    // the form does not fall back to another compute for it: none of its
    // fields are applied, and the page shows the reason instead.
    this.retryUnavailableReason = prefill?.unavailable_reason ?? "";
    if (prefill !== null && this.retryUnavailableReason === "") {
      this.gitUrl = prefill.git_url;
      this.branch = prefill.branch;
      this.hostName = prefill.host_name;
      this.color = prefill.color;
      // A retry keeps the attempt's backup choice as if the user had made it:
      // the account defaults applied below must not replace it.
      this.backupProvider = prefill.backup_provider;
      this.isBackupProviderTouched = true;
      this.backupApiKeyEnv = prefill.backup_api_key_env;
      this.runtime = prefill.docker_runtime;
      this.isOnboardingDrivenByAgent = prefill.is_onboarding_driven_by_agent;
      this.launchValue =
        prefill.cloud_account !== ""
          ? `${CONNECTION_LAUNCH_PREFIX}${prefill.cloud_account}`
          : prefill.launch_mode;
      if (prefill.region !== "")
        this.regionByLaunchValue.set(this.launchValue, prefill.region);
      // The attempt's connection may have changed owner since (re-homed, or its account signed out).
      const prefillLaunchValue = this.launchValue;
      this.syncLaunchValueToSelectedAccount();
      this.instanceTypePreselect = prefill.instance_type;
      // The size belongs to the attempt's mode: once that mode fell back, the
      // next numbered mode the user picks starts from its own defaults.
      if (
        this.launchValue === prefillLaunchValue &&
        (prefill.cpu_count !== null || prefill.memory_gb !== null || prefill.disk_gb !== null)
      ) {
        this.machineSizePreselect = machineSizeDraftFor(prefill);
      }
      this.isAdvancedOpen = true;
      this.selectedPreset = null;
    }
    // Seed the restic env once the defaults land (a retry prefill's saved
    // env wins): the textarea renders and submit sends this same value.
    if (this.backupApiKeyEnv === "") {
      this.backupApiKeyEnv = DEFAULT_RESTIC_ENV;
    }
    this.syncAccountDefaults();
  }

  /** The user flipped the sharing switch. */
  setWebAccess(isOn: boolean): void {
    this.enableWebAccess = isOn;
    this.isWebAccessTouched = true;
  }

  /** The user picked a backup provider. */
  setBackupProvider(provider: string): void {
    this.backupProvider = provider;
    this.isBackupProviderTouched = true;
  }

  /**
   * Sharing and Imbue backups follow the window's account: both are on by
   * default once there is an account to own them, and both fall off when
   * there is none (the server refuses either without an account). A choice
   * the user made by hand holds across account changes.
   */
  syncAccountDefaults(): void {
    if (this.accountId === "") {
      this.enableWebAccess = false;
      if (this.backupProvider === "IMBUE_CLOUD") {
        this.backupProvider = BACKUP_PROVIDER_WITHOUT_ACCOUNT;
      }
      return;
    }
    if (!this.isWebAccessTouched) this.enableWebAccess = true;
    if (!this.isBackupProviderTouched) this.backupProvider = "IMBUE_CLOUD";
  }

  launchSelection(): LaunchSelection {
    if (!this.launchValue.startsWith(CONNECTION_LAUNCH_PREFIX))
      return { mode: this.launchValue, cloudAccount: "" };
    const name = this.launchValue.slice(CONNECTION_LAUNCH_PREFIX.length);
    const account = this.cloudAccount(name);
    return {
      mode: (account?.backend ?? "aws").toUpperCase(),
      cloudAccount: name,
    };
  }

  cloudAccount(name: string): CloudAccountOption | null {
    return (
      this.defaults?.cloud_accounts.find((account) => account.name === name) ??
      null
    );
  }

  /** The compute mode the local preset selects: whichever local backend this machine can run. */
  localLaunchMode(): string {
    return this.defaults?.local_launch_mode ?? "LIMA";
  }

  /** What the machine reports about one local backend, or null before the defaults land. */
  prerequisiteFor(
    key: LocalBackendPrerequisite["key"],
  ): LocalBackendPrerequisite | null {
    return (
      this.defaults?.local_prerequisites.find((entry) => entry.key === key) ??
      null
    );
  }

  /**
   * What the machine is missing for one local backend, or null when the key
   * is a cloud mode's (none), the defaults have not landed, or it can run it.
   */
  unmetPrerequisite(
    key: LocalBackendPrerequisite["key"] | null,
  ): LocalBackendPrerequisite | null {
    if (key === null) return null;
    const prerequisite = this.prerequisiteFor(key);
    return prerequisite !== null && !prerequisite.is_available
      ? prerequisite
      : null;
  }

  /** What the selected compute mode itself needs from this machine and is not getting. */
  unmetPrerequisiteForLaunchMode(): LocalBackendPrerequisite | null {
    return this.unmetPrerequisite(
      localPrerequisiteKeyFor(this.launchSelection().mode),
    );
  }

  /** What the selected container runtime (runsc) needs and is not getting, while the runtime selector is shown. */
  unmetPrerequisiteForRuntime(): LocalBackendPrerequisite | null {
    return this.isRuntimeShown() && this.runtime === "RUNSC"
      ? this.unmetPrerequisite("RUNSC")
      : null;
  }

  /** What the local preset's compute mode needs and is not getting. */
  unmetPrerequisiteForLocalPreset(): LocalBackendPrerequisite | null {
    return this.unmetPrerequisite(
      localPrerequisiteKeyFor(this.localLaunchMode()),
    );
  }

  /**
   * What the selection submit would send needs from this machine and is not
   * getting: the compute mode's own need, else the runtime's. The local preset
   * fills the compute select with the local backend, so it is covered too, and
   * a cloud mode needs nothing local. Submit is refused while this is set: the
   * create would only fail, slowly, where the notice already says what to do.
   */
  unmetPrerequisiteForSubmit(): LocalBackendPrerequisite | null {
    return (
      this.unmetPrerequisiteForLaunchMode() ??
      this.unmetPrerequisiteForRuntime()
    );
  }

  /**
   * Re-read what the machine can run, after the user installed something. Only
   * the prerequisite facts move; every edit in the form stays.
   */
  refreshPrerequisites(defaults: CreateFormDefaults): void {
    if (this.defaults === null) return;
    this.defaults = {
      ...this.defaults,
      local_prerequisites: defaults.local_prerequisites,
      local_launch_mode: defaults.local_launch_mode,
    };
    if (this.selectedPreset === "local")
      this.launchValue = defaults.local_launch_mode;
  }

  /** The owner whose providers are on offer: the window's account, or the local profile. */
  selectedOwnerUserId(): string {
    return this.accountId || LOCAL_PROFILE_USER_ID;
  }

  /** The connections `owner` holds, in alias order. */
  private connectionsOwnedBy(owner: string): CloudAccountOption[] {
    return (this.defaults?.cloud_accounts ?? [])
      .filter((account) => account.owner_user_id === owner)
      .sort((left, right) => left.alias.localeCompare(right.alias));
  }

  /**
   * Whether the compute select offers a connection under the selected owner. A
   * this-device-only connection is a device provider, like docker and lima: it
   * is offered under every account and under none. An account's own connection
   * is offered under that account alone.
   */
  isConnectionOfferedUnderSelectedOwner(account: CloudAccountOption): boolean {
    return (
      account.owner_user_id === LOCAL_PROFILE_USER_ID ||
      account.owner_user_id === this.selectedOwnerUserId()
    );
  }

  /**
   * The compute select's rows: the device providers, Imbue Cloud, the selected
   * account's own connections, then this device's connections (suffixed, under
   * an account, so they read apart from the account's own).
   */
  launchOptions(): LaunchOption[] {
    const options: LaunchOption[] = [];
    for (const mode of this.defaults?.launch_modes ?? []) {
      // Imbue Cloud is the window's account's own provider: without one it stays visible but unusable.
      // Every other mode the backend offers belongs to no account (the connection-only modes are never sent).
      const isDisabled = mode === "IMBUE_CLOUD" && this.accountId === "";
      options.push({ value: mode, label: launchModeLabel(mode), isDisabled });
    }
    const connectionOption = (
      account: CloudAccountOption,
      suffix: string,
    ): LaunchOption => ({
      value: `${CONNECTION_LAUNCH_PREFIX}${account.name}`,
      label:
        connectionLaunchLabel(
          account.alias,
          connectionBackendLabelFor(
            this.defaults?.connection_backends ?? [],
            account.backend,
          ),
        ) + suffix,
      isDisabled: false,
    });
    if (this.accountId !== "") {
      for (const account of this.connectionsOwnedBy(this.accountId))
        options.push(connectionOption(account, ""));
    }
    const localSuffix = this.accountId === "" ? "" : LOCAL_CONNECTION_SUFFIX;
    for (const account of this.connectionsOwnedBy(LOCAL_PROFILE_USER_ID))
      options.push(connectionOption(account, localSuffix));
    return options;
  }

  /**
   * After the account changes (or a retry prefill lands): a connection the new
   * owner cannot use, or a mode the select does not offer (a connection-only
   * mode whose connection is gone), falls back to a mode the owner can use. A
   * preset card no longer describes a compute that moved, so it is cleared, as
   * the advanced compute select clears it.
   */
  syncLaunchValueToSelectedAccount(): void {
    const launchValueBefore = this.launchValue;
    this.launchValue = this.launchValueForSelectedAccount();
    if (this.launchValue !== launchValueBefore) this.selectedPreset = null;
    this.syncAccountDefaults();
  }

  private launchValueForSelectedAccount(): string {
    const selection = this.launchSelection();
    if (selection.cloudAccount === "") {
      if (selection.mode === "IMBUE_CLOUD" && this.accountId === "")
        return this.localLaunchMode();
      if (
        this.defaults !== null &&
        !this.defaults.launch_modes.includes(selection.mode)
      )
        return this.ownerFallbackLaunchMode();
      return this.launchValue;
    }
    const account = this.cloudAccount(selection.cloudAccount);
    if (account === null || !this.isConnectionOfferedUnderSelectedOwner(account)) {
      return this.ownerFallbackLaunchMode();
    }
    return this.launchValue;
  }

  /** The mode a dropped selection falls back to: the account's Imbue Cloud, or this device without one. */
  private ownerFallbackLaunchMode(): string {
    return this.accountId === "" ? this.localLaunchMode() : "IMBUE_CLOUD";
  }

  /**
   * The compute select value a preset picks: the local preset's is whichever
   * local backend this machine can run, and the own-cloud preset's is the
   * first connection the compute select offers under the selected owner (the
   * account's own, then this device's), else Vultr.
   */
  presetLaunchValue(name: PresetName): string {
    switch (name) {
      case "remote":
        return "IMBUE_CLOUD";
      case "local":
        return this.localLaunchMode();
      case "own-cloud": {
        const connection = this.launchOptions().find((option) =>
          option.value.startsWith(CONNECTION_LAUNCH_PREFIX),
        );
        return connection === undefined ? "VULTR" : connection.value;
      }
    }
  }

  applyPreset(name: PresetName): void {
    this.selectedPreset = name;
    this.launchValue = this.presetLaunchValue(name);
    this.syncAccountDefaults();
  }

  /**
   * Take up the account the window now shows (a sign-in it asked for landed),
   * and the connections on offer under it (the account's synced ones). Every
   * edit in the form stays; a compute selection the new owner cannot use
   * falls back. Returns whether the compute selection survived, so a caller
   * about to submit knows the form still says what the user picked.
   */
  adoptAccount(defaults: CreateFormDefaults): boolean {
    if (this.defaults === null) return true;
    this.defaults = {
      ...this.defaults,
      account: defaults.account,
      cloud_accounts: defaults.cloud_accounts,
    };
    this.accountId = defaults.account?.user_id ?? "";
    const launchValueBefore = this.launchValue;
    this.syncLaunchValueToSelectedAccount();
    return this.launchValue === launchValueBefore;
  }

  /**
   * Offer a connection the add-connection modal just registered, and select it
   * when the window's account can use it. The account stays the window's: a
   * connection added under another owner is offered once the window is on that
   * owner, rather than moving the workspace's association as a side effect.
   */
  adoptConnection(account: CloudAccountOption): void {
    if (this.defaults === null) return;
    this.defaults = {
      ...this.defaults,
      cloud_accounts: [...this.defaults.cloud_accounts, account],
    };
    if (!this.isConnectionOfferedUnderSelectedOwner(account)) return;
    this.launchValue = `${CONNECTION_LAUNCH_PREFIX}${account.name}`;
    this.selectedPreset = null;
  }

  /** The window's account the form creates under, or null on the device or a signed-out account. */
  selectedAccount(): CreateAccountOption | null {
    const account = this.defaults?.account ?? null;
    return account !== null && account.user_id === this.accountId
      ? account
      : null;
  }

  /** The email of the account the form creates under, or "" when there is none. */
  selectedAccountEmail(): string {
    return this.selectedAccount()?.email ?? "";
  }

  /** Whether the picked account's connections need the master password here before they can be used. */
  isSelectedAccountLocked(): boolean {
    return this.selectedAccount()?.state === "LOCKED";
  }

  /** Whether the picked compute boots a machine from scratch (everything but a leased Imbue Cloud host). */
  isSlowStartCompute(): boolean {
    return this.launchSelection().mode !== "IMBUE_CLOUD";
  }

  imbueCloudNeedsAccount(): boolean {
    return (
      this.accountId === "" &&
      (this.launchValue === "IMBUE_CLOUD" ||
        this.backupProvider === "IMBUE_CLOUD")
    );
  }

  /** Region options for the current selection; empty means hide the picker (region-less, or pinned on the connection). */
  regionOptions(): string[] {
    const selection = this.launchSelection();
    if (
      selection.cloudAccount !== "" &&
      (this.cloudAccount(selection.cloudAccount)?.is_region_pinned ?? true)
    ) {
      return [];
    }
    return this.defaults?.region_options_by_launch_mode[selection.mode] ?? [];
  }

  selectedRegion(): string {
    const remembered = this.regionByLaunchValue.get(this.launchValue);
    if (remembered !== undefined) return remembered;
    const selection = this.launchSelection();
    return (
      this.defaults?.region_selected_by_launch_mode[selection.mode] ??
      this.regionOptions()[0] ??
      ""
    );
  }

  setRegion(region: string): void {
    this.regionByLaunchValue.set(this.launchValue, region);
  }

  /** The selected connection's pinned region note, or "" when the region is picked per create. */
  pinnedRegion(): string {
    const selection = this.launchSelection();
    if (selection.cloudAccount === "") return "";
    const account = this.cloudAccount(selection.cloudAccount);
    return account !== null && account.is_region_pinned ? account.region : "";
  }

  instanceTypeOptions(): [string, string][] {
    const backend = this.launchSelection().mode;
    return this.defaults?.instance_types_by_backend[backend] ?? [];
  }

  selectedInstanceType(): string {
    const options = this.instanceTypeOptions();
    if (options.length === 0) return "";
    const remembered = this.instanceTypeByLaunchValue.get(this.launchValue);
    if (remembered !== undefined) return remembered;
    if (
      this.instanceTypePreselect !== "" &&
      options.some(([value]) => value === this.instanceTypePreselect)
    ) {
      const preselect = this.instanceTypePreselect;
      this.instanceTypePreselect = "";
      this.instanceTypeByLaunchValue.set(this.launchValue, preselect);
      return preselect;
    }
    const backend = this.launchSelection().mode;
    return (
      this.defaults?.default_instance_type_by_backend[backend] ?? options[0][0]
    );
  }

  setInstanceType(instanceType: string): void {
    this.instanceTypeByLaunchValue.set(this.launchValue, instanceType);
  }

  /** The numeric size the selected mode takes (its defaults, as offered), or null for a mode sized another way. */
  machineSizeDefaults(): MachineSizeNumbers | null {
    return (
      this.defaults?.machine_size_defaults_by_launch_mode[
        this.launchSelection().mode
      ] ?? null
    );
  }

  /** The size fields as currently shown for the selected mode; null when the mode has none. */
  machineSizeDraft(): MachineSizeDraft | null {
    const defaults = this.machineSizeDefaults();
    if (defaults === null) return null;
    const remembered = this.machineSizeByLaunchValue.get(this.launchValue);
    if (remembered !== undefined) return remembered;
    if (this.machineSizePreselect !== null) {
      // A retry pre-fill names the dimensions its mode took; the mode keeps only those.
      const preselect: MachineSizeDraft = {
        cpu_count: defaults.cpu_count === null ? "" : this.machineSizePreselect.cpu_count,
        memory_gb: defaults.memory_gb === null ? "" : this.machineSizePreselect.memory_gb,
        disk_gb: defaults.disk_gb === null ? "" : this.machineSizePreselect.disk_gb,
      };
      this.machineSizePreselect = null;
      this.machineSizeByLaunchValue.set(this.launchValue, preselect);
      return preselect;
    }
    return machineSizeDraftFor(defaults);
  }

  setMachineSizeField(dimension: MachineSizeDimension, text: string): void {
    const current = this.machineSizeDraft();
    if (current === null) return;
    this.machineSizeByLaunchValue.set(this.launchValue, { ...current, [dimension]: text });
  }

  /**
   * Which size fields hold something other than a whole positive number, for
   * the dimensions the selected mode takes. Submit is refused while any does.
   */
  invalidMachineSizeDimensions(): MachineSizeDimension[] {
    const defaults = this.machineSizeDefaults();
    const draft = this.machineSizeDraft();
    if (defaults === null || draft === null) return [];
    return MACHINE_SIZE_DIMENSIONS.filter(
      (dimension) => defaults[dimension] !== null && parseMachineSizeNumber(draft[dimension]) === null,
    );
  }

  isRuntimeShown(): boolean {
    return this.launchValue === "DOCKER";
  }

  hostNameAvailabilityUrl(): string {
    const selection = this.launchSelection();
    const params = new URLSearchParams({
      name: this.hostName.trim(),
      launch_mode: selection.mode,
      account_id: this.accountId,
      cloud_account: selection.cloudAccount,
    });
    return `/api/v1/desktop/host-name-available?${params.toString()}`;
  }

  /** Synchronous format check; returns whether submit may proceed name-wise. */
  validateHostNameFormatForSubmit(): boolean {
    const trimmed = this.hostName.trim();
    const formatError = hostNameFormatError(trimmed);
    if (formatError !== "") {
      this.hostNameState = "invalid";
      this.hostNameError = formatError;
      return false;
    }
    if (this.hostNameState === "invalid") {
      this.hostNameState = "ok";
      this.hostNameError = "";
    }
    // A 'taken' verdict only blocks the exact name/scope it was computed for;
    // anything else is stale and falls through to the server's own check.
    const isStillTaken =
      this.hostNameState === "taken" &&
      this.hostNameTakenKey === this.hostNameAvailabilityUrl();
    return !isStillTaken;
  }

  applyAvailabilityVerdict(url: string, isAvailable: boolean): void {
    if (url !== this.hostNameAvailabilityUrl()) return;
    if (!isAvailable) {
      this.hostNameState = "taken";
      this.hostNameTakenKey = url;
      this.hostNameError = "That name is already taken. Pick a different one.";
    } else {
      this.hostNameState = "ok";
      this.hostNameTakenKey = null;
      this.hostNameError = "";
    }
  }

  validateHostNameLive(): void {
    const trimmed = this.hostName.trim();
    const formatError = hostNameFormatError(trimmed);
    if (formatError !== "") {
      this.hostNameState = "invalid";
      this.hostNameTakenKey = null;
      this.hostNameError = formatError;
      return;
    }
    this.hostNameState = "ok";
    this.hostNameTakenKey = null;
    this.hostNameError = "";
  }

  submitBody(): CreateSubmitBody {
    const selection = this.launchSelection();
    const body: CreateSubmitBody = {
      git_url: this.gitUrl.trim(),
      host_name: this.hostName.trim(),
      branch: this.branch.trim(),
      color: this.color,
      launch_mode: selection.mode,
      cloud_account: selection.cloudAccount,
      account_id: this.accountId,
      backup_provider: this.backupProvider,
      backup_api_key_env:
        this.backupProvider === "API_KEY" ? this.backupApiKeyEnv : "",
      region: this.regionOptions().length > 0 ? this.selectedRegion() : "",
      instance_type:
        this.instanceTypeOptions().length > 0
          ? this.selectedInstanceType()
          : "",
      enable_web_access: this.enableWebAccess,
      is_onboarding_driven_by_agent: this.isOnboardingDrivenByAgent,
    };
    if (this.isRuntimeShown()) body.runtime = this.runtime;
    const sizeDefaults = this.machineSizeDefaults();
    const sizeDraft = this.machineSizeDraft();
    if (sizeDefaults !== null && sizeDraft !== null) {
      for (const dimension of MACHINE_SIZE_DIMENSIONS) {
        if (sizeDefaults[dimension] === null) continue;
        const value = parseMachineSizeNumber(sizeDraft[dimension]);
        if (value !== null) body[dimension] = value;
      }
    }
    return body;
  }
}
