import { describe, expect, it } from "vitest";
import type { UiAccountProvidersEntry, UiConnectionEntry } from "../generated/ui";
import {
  ProvidersStore,
  accountSectionTitleFor,
  connectionDetailFor,
  connectionLaunchLabel,
  connectionStatusChipFor,
  editControlFor,
  providerStatusChipFor,
  removeControlFor,
} from "./providers";

function connection(overrides: Partial<UiConnectionEntry> = {}): UiConnectionEntry {
  return {
    id: "conn-1",
    instance: "modal_" + "0".repeat(32),
    backend: "modal",
    backend_label: "Modal",
    alias: "team modal",
    identifier: "ak…1234",
    placement: {},
    status: "OK",
    workspace_count: 0,
    ...overrides,
  };
}

function account(overrides: Partial<UiAccountProvidersEntry> = {}): UiAccountProvidersEntry {
  return {
    user_id: "user-1",
    email: "alice@example.com",
    state: "SIGNED_IN",
    is_connection_owner_eligible: true,
    imbue_cloud: { name: "imbue_cloud_user-1", backend: "imbue_cloud", status: "OK" },
    connections: [],
    ...overrides,
  };
}

describe("ProvidersStore", () => {
  it("offers eligible accounts as connection owners, then this device", () => {
    const store = new ProvidersStore();
    store.applyProvidersMessage({
      type: "providers",
      accounts: [
        account(),
        account({ user_id: "user-2", email: "locked@example.com", state: "LOCKED", is_connection_owner_eligible: false }),
        account({ user_id: "local", email: "", imbue_cloud: null }),
      ],
      device_providers: [],
      last_event_at: null,
      last_full_snapshot_at: null,
    });
    expect(store.connectionOwnerOptions()).toEqual([
      { userId: "user-1", label: "alice@example.com" },
      { userId: "local", label: "This device only" },
    ]);
  });

  it("counts every row and every error across sections", () => {
    const store = new ProvidersStore();
    store.applyProvidersMessage({
      type: "providers",
      accounts: [
        account({
          imbue_cloud: { name: "imbue_cloud_user-1", backend: null, status: "ERROR", error_type: "X", error_message: "y" },
          connections: [connection(), connection({ id: "conn-2", status: "ERROR" })],
        }),
        account({ user_id: "local", email: "", imbue_cloud: null }),
      ],
      device_providers: [{ name: "docker", backend: "docker", status: "OK" }],
      last_event_at: null,
      last_full_snapshot_at: null,
    });
    expect(store.rowCount()).toBe(4);
    expect(store.errorCount()).toBe(2);
  });
});

describe("row decisions", () => {
  it("names a connection by its alias and the backend label the server sent", () => {
    expect(connectionLaunchLabel("team cloud", "AWS")).toBe("team cloud (AWS)");
  });

  it("labels section titles", () => {
    expect(accountSectionTitleFor({ user_id: "local", email: "", state: "SIGNED_IN" })).toBe("This device only");
    expect(accountSectionTitleFor({ user_id: "u", email: "a@b.c", state: "LOCKED" })).toBe("a@b.c (locked)");
    expect(accountSectionTitleFor({ user_id: "12345678-abcd", email: "", state: "SIGNED_OUT" })).toBe(
      "12345678… (signed out)",
    );
  });

  it("maps every provider status to a chip", () => {
    expect(providerStatusChipFor("OK")).toEqual({ label: "OK", tone: "ok" });
    expect(providerStatusChipFor("ERROR")).toEqual({ label: "Error", tone: "error" });
    expect(providerStatusChipFor("UNLISTED")).toEqual({ label: "Starting…", tone: "muted" });
  });

  it("maps every connection status to a chip", () => {
    expect(connectionStatusChipFor("OK")).toEqual({ label: "OK", tone: "ok" });
    expect(connectionStatusChipFor("LOCKED").tone).toBe("warn");
    expect(connectionStatusChipFor("ERROR").tone).toBe("error");
    expect(connectionStatusChipFor("UNLISTED").tone).toBe("muted");
    expect(connectionStatusChipFor("READ_ONLY").label).toBe("Needs a newer app");
    expect(connectionStatusChipFor("NO_CREDENTIALS").label).toBe("No credentials");
    expect(connectionStatusChipFor("PROFILE_MISSING")).toEqual({ label: "Set up on this device", tone: "warn" });
  });

  it("tells a missing login's setup step apart from a provider error, and names the fix for both", () => {
    expect(
      connectionDetailFor({
        status: "PROFILE_MISSING",
        error_type: "ProviderCredentialSourceMissingError",
        error_message: "AWS profile 'work' is not configured on this machine",
        remediation: "run `aws configure --profile work`",
      }),
    ).toBe(
      "This connection uses a login that is not set up on this device. To use it here, run `aws configure " +
        "--profile work`, then check again.",
    );
    expect(
      connectionDetailFor({
        status: "ERROR",
        error_type: "ProviderNotAuthorizedError",
        error_message: "expired",
        remediation: "run `aws sso login --profile work`",
      }),
    ).toBe("ProviderNotAuthorizedError: expired (fix: run `aws sso login --profile work`)");
    expect(connectionDetailFor({ status: "ERROR", error_type: "E", error_message: "boom", remediation: null })).toBe(
      "E: boom",
    );
    expect(connectionDetailFor({ status: "OK", error_type: null, error_message: null, remediation: null })).toBe("");
  });

  it("disables Remove while machines use the connection or a newer app owns it", () => {
    expect(removeControlFor({ workspace_count: 0, status: "OK" })).toEqual({
      isEnabled: true,
      tooltip: "Remove this connection",
    });
    expect(removeControlFor({ workspace_count: 2, status: "OK" })).toEqual({
      isEnabled: false,
      tooltip: "In use by 2 machines — destroy them first.",
    });
    expect(removeControlFor({ workspace_count: 1, status: "ERROR" }).tooltip).toBe(
      "In use by 1 machine — destroy them first.",
    );
    expect(removeControlFor({ workspace_count: 0, status: "READ_ONLY" }).isEnabled).toBe(false);
  });

  it("offers Edit only while the connection's credentials are usable here", () => {
    for (const status of ["OK", "ERROR", "UNLISTED"] as const) {
      expect(editControlFor({ status })).toEqual({
        isEnabled: true,
        tooltip: "Replace this connection's credentials",
      });
    }
    expect(editControlFor({ status: "LOCKED" })).toEqual({
      isEnabled: false,
      tooltip: "Enter your master password to edit this connection",
    });
    expect(editControlFor({ status: "NO_CREDENTIALS" }).isEnabled).toBe(false);
    expect(editControlFor({ status: "PROFILE_MISSING" })).toEqual({
      isEnabled: true,
      tooltip: "Use another login or paste keys instead",
    });
    expect(editControlFor({ status: "READ_ONLY" }).tooltip).toBe(
      "This connection was added by a newer version of the app.",
    );
  });
});
