// State machine for the create form (the SPA port of Create.jinja's inline
// script). Pure state + injected fetchers so vitest can drive every branch;
// the CreatePage view renders from it and forwards DOM events.

import type { CloudAccountOption, CreateFormDefaults } from "../../../models/create";
import { hostNameFormatError } from "../../../models/create";

export type PresetName = "remote" | "local";

// Seeded into the restic-env textarea so what the user sees is exactly what
// submit sends (the view renders model state verbatim; no display-only
// fallback).
export const DEFAULT_RESTIC_ENV =
  "# see docs linked above for available options\n" +
  "# for example:\n" +
  "RESTIC_REPOSITORY=s3:s3.amazonaws.com/<bucket_name>\n" +
  "AWS_ACCESS_KEY_ID=\n" +
  "AWS_SECRET_ACCESS_KEY=\n";

export const PRESET_FILLS: Record<PresetName, { launch_mode: string; backup_provider: string }> = {
  remote: { launch_mode: "IMBUE_CLOUD", backup_provider: "IMBUE_CLOUD" },
  local: { launch_mode: "LIMA", backup_provider: "CONFIGURE_LATER" },
};

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
  // "Enable web access" (default off): bring sharing up post-create so the
  // workspace is reachable from the hosted web client.
  enableWebAccess = false;
  selectedPreset: PresetName | null = "remote";
  isAdvancedOpen = false;

  // Region / machine-size picks are keyed by the raw select value so a user's
  // explicit pick survives unrelated changes but rebuilds on provider switch.
  private regionByLaunchValue = new Map<string, string>();
  private instanceTypeByLaunchValue = new Map<string, string>();
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

  applyPreset(name: PresetName): void {
    this.selectedPreset = name;
    this.launchValue = PRESET_FILLS[name].launch_mode;
    this.backupProvider = PRESET_FILLS[name].backup_provider;
    if (name === "remote" && this.accountId === "" && (this.defaults?.accounts.length ?? 0) > 0) {
      this.accountId = this.defaults?.accounts[0]?.user_id ?? "";
    }
    if (!this.imbueCloudNeedsAccount()) this.isAccountErrorShown = false;
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
    return body;
  }
}
