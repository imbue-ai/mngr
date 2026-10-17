// State machine for the create form (the SPA port of Create.jinja's inline
// script). Pure state + injected fetchers so vitest can drive every branch;
// the CreatePage view renders from it and forwards DOM events.

import type { CloudAccountOption, CreateFormDefaults, LocalBackendPrerequisite } from "../../../models/create";
import { hostNameFormatError } from "../../../models/create";
import type { MachineSizeDimension, MachineSizeDraft, MachineSizeNumbers } from "../../../models/machineSize";
import {
  MACHINE_SIZE_DIMENSIONS,
  machineSizeDraftFor,
  parseMachineSizeNumber,
} from "../../../models/machineSize";

export type PresetName = "remote" | "local" | "own-cloud";

/**
 * The local backend a compute mode depends on, or null for the cloud modes.
 * The container runtime's own requirement (runsc) is annotated separately,
 * under the runtime selector.
 */
function localPrerequisiteKeyFor(launchMode: string): LocalBackendPrerequisite["key"] | null {
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

// Each preset's backup provider; its compute mode is CreateFormModel.presetLaunchValue's.
export const PRESET_BACKUP_PROVIDERS: Record<PresetName, string> = {
  remote: "IMBUE_CLOUD",
  local: "CONFIGURE_LATER",
  "own-cloud": "CONFIGURE_LATER",
};

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
 * bodies, and the no-account {error, redirect_url} backstop) into one form. */
export function normalizeCreateApiError(data: unknown): NormalizedApiError {
  const body = (data ?? {}) as Record<string, unknown>;
  const message =
    typeof body.error === "string"
      ? body.error
      : typeof body.detail === "string"
        ? body.detail
        : "Could not create the workspace.";
  return {
    message,
    field: typeof body.field === "string" ? body.field : "",
    redirectUrl: typeof body.redirect_url === "string" ? body.redirect_url : "",
  };
}

export class CreateFormModel {
  defaults: CreateFormDefaults | null = null;
  loadError = "";

  gitUrl = "";
  branch = "";
  hostName = "";
  color = "";
  // The raw compute select value: a LaunchMode value or "BYOK:<name>".
  launchValue = "IMBUE_CLOUD";
  backupProvider = "IMBUE_CLOUD";
  backupApiKeyEnv = "";
  accountId = "";
  runtime = "";
  // The share panel's "Enable sharing and web access" switch, thrown for the
  // new workspace right after its create.
  enableWebAccess = false;
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
  isAccountErrorShown = false;

  applyDefaults(defaults: CreateFormDefaults): void {
    this.defaults = defaults;
    this.color = defaults.color;
    this.accountId = defaults.default_account_id;
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
    if (prefill !== null) {
      this.gitUrl = prefill.git_url;
      this.branch = prefill.branch;
      this.hostName = prefill.host_name;
      this.color = prefill.color;
      this.backupProvider = prefill.backup_provider;
      this.backupApiKeyEnv = prefill.backup_api_key_env;
      this.accountId = prefill.account_id || defaults.default_account_id;
      this.runtime = prefill.docker_runtime;
      this.launchValue = prefill.cloud_account !== "" ? `BYOK:${prefill.cloud_account}` : prefill.launch_mode;
      if (prefill.region !== "") this.regionByLaunchValue.set(this.launchValue, prefill.region);
      this.instanceTypePreselect = prefill.instance_type;
      if (prefill.cpu_count !== null || prefill.memory_gb !== null || prefill.disk_gb !== null) {
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
  }

  launchSelection(): LaunchSelection {
    if (!this.launchValue.startsWith("BYOK:")) return { mode: this.launchValue, cloudAccount: "" };
    const name = this.launchValue.slice("BYOK:".length);
    const account = this.cloudAccount(name);
    return { mode: (account?.backend ?? "aws").toUpperCase(), cloudAccount: name };
  }

  cloudAccount(name: string): CloudAccountOption | null {
    return this.defaults?.cloud_accounts.find((account) => account.name === name) ?? null;
  }

  /** The compute mode the local preset selects: whichever local backend this machine can run. */
  localLaunchMode(): string {
    return this.defaults?.local_launch_mode ?? "LIMA";
  }

  /** What the machine reports about one local backend, or null before the defaults land. */
  prerequisiteFor(key: LocalBackendPrerequisite["key"]): LocalBackendPrerequisite | null {
    return this.defaults?.local_prerequisites.find((entry) => entry.key === key) ?? null;
  }

  /**
   * What the machine is missing for one local backend, or null when the key
   * is a cloud mode's (none), the defaults have not landed, or it can run it.
   */
  unmetPrerequisite(key: LocalBackendPrerequisite["key"] | null): LocalBackendPrerequisite | null {
    if (key === null) return null;
    const prerequisite = this.prerequisiteFor(key);
    return prerequisite !== null && !prerequisite.is_available ? prerequisite : null;
  }

  /** What the selected compute mode itself needs from this machine and is not getting. */
  unmetPrerequisiteForLaunchMode(): LocalBackendPrerequisite | null {
    return this.unmetPrerequisite(localPrerequisiteKeyFor(this.launchSelection().mode));
  }

  /** What the selected container runtime (runsc) needs and is not getting, while the runtime selector is shown. */
  unmetPrerequisiteForRuntime(): LocalBackendPrerequisite | null {
    return this.isRuntimeShown() && this.runtime === "RUNSC" ? this.unmetPrerequisite("RUNSC") : null;
  }

  /** What the local preset's compute mode needs and is not getting. */
  unmetPrerequisiteForLocalPreset(): LocalBackendPrerequisite | null {
    return this.unmetPrerequisite(localPrerequisiteKeyFor(this.localLaunchMode()));
  }

  /**
   * What the selection submit would send needs from this machine and is not
   * getting: the compute mode's own need, else the runtime's. The local preset
   * fills the compute select with the local backend, so it is covered too, and
   * a cloud mode needs nothing local. Submit is refused while this is set: the
   * create would only fail, slowly, where the notice already says what to do.
   */
  unmetPrerequisiteForSubmit(): LocalBackendPrerequisite | null {
    return this.unmetPrerequisiteForLaunchMode() ?? this.unmetPrerequisiteForRuntime();
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
    if (this.selectedPreset === "local") this.launchValue = defaults.local_launch_mode;
  }

  /**
   * The compute select value a preset picks: the local preset's is whichever
   * local backend this machine can run, and the own-cloud preset's is the
   * first cloud account the user added (while bring-your-own-cloud is on),
   * else Vultr.
   */
  presetLaunchValue(name: PresetName): string {
    switch (name) {
      case "remote":
        return "IMBUE_CLOUD";
      case "local":
        return this.localLaunchMode();
      case "own-cloud": {
        const account = this.defaults?.byok_clouds_enabled ? this.defaults.cloud_accounts[0] : undefined;
        return account === undefined ? "VULTR" : `BYOK:${account.name}`;
      }
    }
  }

  applyPreset(name: PresetName): void {
    this.selectedPreset = name;
    this.launchValue = this.presetLaunchValue(name);
    this.backupProvider = PRESET_BACKUP_PROVIDERS[name];
    if (name === "remote" && this.accountId === "" && (this.defaults?.accounts.length ?? 0) > 0) {
      this.accountId = this.defaults?.accounts[0]?.user_id ?? "";
    }
    if (!this.imbueCloudNeedsAccount()) this.isAccountErrorShown = false;
  }

  /**
   * Take up the accounts a sign-in added while the form was open. Every edit
   * in the form stays; only an account pick that is empty or no longer signed
   * in moves, to the new default.
   */
  adoptAccounts(defaults: CreateFormDefaults): void {
    if (this.defaults === null) return;
    this.defaults = { ...this.defaults, accounts: defaults.accounts, default_account_id: defaults.default_account_id };
    if (!defaults.accounts.some((account) => account.user_id === this.accountId)) {
      this.accountId = defaults.default_account_id;
    }
  }

  /** The email of the picked account, or "" when none (or an unknown one) is picked. */
  selectedAccountEmail(): string {
    return this.defaults?.accounts.find((account) => account.user_id === this.accountId)?.email ?? "";
  }

  imbueCloudNeedsAccount(): boolean {
    return this.accountId === "" && (this.launchValue === "IMBUE_CLOUD" || this.backupProvider === "IMBUE_CLOUD");
  }

  /** Region options for the current selection; empty means hide the picker. */
  regionOptions(): string[] {
    const selection = this.launchSelection();
    if (selection.cloudAccount !== "") return [];
    return this.defaults?.region_options_by_launch_mode[selection.mode] ?? [];
  }

  selectedRegion(): string {
    const remembered = this.regionByLaunchValue.get(this.launchValue);
    if (remembered !== undefined) return remembered;
    const selection = this.launchSelection();
    return this.defaults?.region_selected_by_launch_mode[selection.mode] ?? this.regionOptions()[0] ?? "";
  }

  setRegion(region: string): void {
    this.regionByLaunchValue.set(this.launchValue, region);
  }

  /** The BYOK account's pinned region note, or "" for ordinary modes. */
  byokPinnedRegion(): string {
    const selection = this.launchSelection();
    if (selection.cloudAccount === "") return "";
    return this.cloudAccount(selection.cloudAccount)?.region ?? "";
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
    if (this.instanceTypePreselect !== "" && options.some(([value]) => value === this.instanceTypePreselect)) {
      const preselect = this.instanceTypePreselect;
      this.instanceTypePreselect = "";
      this.instanceTypeByLaunchValue.set(this.launchValue, preselect);
      return preselect;
    }
    const backend = this.launchSelection().mode;
    return this.defaults?.default_instance_type_by_backend[backend] ?? options[0][0];
  }

  setInstanceType(instanceType: string): void {
    this.instanceTypeByLaunchValue.set(this.launchValue, instanceType);
  }

  /** The numeric size the selected mode takes (its defaults, as offered), or null for a mode sized another way. */
  machineSizeDefaults(): MachineSizeNumbers | null {
    return this.defaults?.machine_size_defaults_by_launch_mode[this.launchSelection().mode] ?? null;
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
      region: this.regionOptions().length > 0 ? this.selectedRegion() : "",
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
    const isStillTaken = this.hostNameState === "taken" && this.hostNameTakenKey === this.hostNameAvailabilityUrl();
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
      backup_api_key_env: this.backupProvider === "API_KEY" ? this.backupApiKeyEnv : "",
      region: this.regionOptions().length > 0 ? this.selectedRegion() : "",
      instance_type: this.instanceTypeOptions().length > 0 ? this.selectedInstanceType() : "",
      enable_web_access: this.enableWebAccess,
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
