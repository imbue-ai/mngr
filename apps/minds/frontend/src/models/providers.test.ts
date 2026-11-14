import { describe, expect, it } from "vitest";
import type { UiAccountProvidersEntry, UiConnectionEntry } from "../generated/ui";
import { TextResponseBody } from "./errors";
import {
  ProvidersStore,
  accountSectionTitleFor,
  connectionLaunchLabel,
  connectionRemovalErrorFor,
  connectionRowProblemFor,
  connectionStatusChipFor,
  editControlFor,
  providerRowProblemFor,
  providersSummaryFor,
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

  it("counts every row, and every row that asks something of the user, across sections", () => {
    const store = new ProvidersStore();
    store.applyProvidersMessage({
      type: "providers",
      accounts: [
        account({
          imbue_cloud: { name: "imbue_cloud_user-1", backend: null, status: "ERROR", error_type: "X", error_message: "y" },
          connections: [
            connection(),
            connection({ id: "conn-2", status: "ERROR" }),
            connection({ id: "conn-3", status: "PROFILE_MISSING" }),
            connection({ id: "conn-4", status: "UNLISTED" }),
          ],
        }),
        account({ user_id: "local", email: "", imbue_cloud: null }),
      ],
      device_providers: [
        { name: "docker", backend: "docker", status: "OK" },
        { name: "lima", backend: null, status: "ERROR", error_type: "E", error_message: "m" },
      ],
      last_event_at: null,
      last_full_snapshot_at: null,
    });
    expect(store.rowCount()).toBe(7);
    // A connection still starting up is not the user's to fix; a missing login is.
    expect(store.attentionCount()).toBe(4);
    expect(providersSummaryFor(store.rowCount(), store.attentionCount())).toBe("7 providers, 4 need attention");
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
    expect(connectionStatusChipFor("PROFILE_MISSING")).toEqual({ label: "Set up on this computer", tone: "warn" });
  });

  it("tells a missing login's setup step apart from a failed check, keeping the fix visible and the raw error behind details", () => {
    const missing = connectionRowProblemFor({
      status: "PROFILE_MISSING",
      error_type: "ProviderCredentialSourceMissingError",
      error_message: "AWS profile 'work' is not configured on this machine",
      remediation: "run `aws configure --profile work`",
    });
    expect(missing?.message).toContain("not set up on this computer");
    expect(missing?.message).toContain("run `aws configure --profile work`");
    expect(missing?.message).not.toContain("ProviderCredentialSourceMissingError");
    expect(missing?.details).toBe(
      "ProviderCredentialSourceMissingError: AWS profile 'work' is not configured on this machine",
    );

    // The provider's own reason is what tells the user what to do, so it leads
    // the row; mngr's generic wrapper and the exception type stay in details.
    const wrapped =
      "Provider 'aws_1' is not available: The SSO session for profile 'work' has expired. " +
      "Any agents managed by this provider could not be reached.";
    const failed = connectionRowProblemFor({
      status: "ERROR",
      error_type: "ProviderNotAuthorizedError",
      error_message: wrapped,
      error_reason: "The SSO session for profile 'work' has expired",
      remediation: "run `aws sso login --profile work`",
    });
    expect(failed?.message).toBe(
      "The SSO session for profile 'work' has expired. Workspaces on it may be missing from the list. " +
        "To fix it, *run `aws sso login --profile work`*.",
    );
    expect(failed?.details).toBe(`ProviderNotAuthorizedError: ${wrapped}`);

    const unfixable = connectionRowProblemFor({ status: "ERROR", error_type: "E", error_message: "boom", remediation: null });
    expect(unfixable?.message).toBe("boom. Workspaces on it may be missing from the list.");
    expect(unfixable?.details).toBe("E: boom");
    expect(connectionRowProblemFor({ status: "OK", error_type: null, error_message: null, remediation: null })).toBeNull();
  });

  it("leads a provider row with its reason and fix, keeping the exception type behind details", () => {
    const docker = providerRowProblemFor(
      {
        status: "ERROR",
        error_type: "ProviderUnavailableError",
        error_message: "Cannot connect to Docker daemon: connection refused",
        error_reason: "Cannot connect to Docker daemon: connection refused",
        remediation: "start Docker",
      },
      "provider",
    );
    expect(docker?.message).toBe(
      "Cannot connect to Docker daemon: connection refused. Workspaces on it may be missing from the list. " +
        "To fix it, *start Docker*.",
    );
    expect(docker?.details).toBe("ProviderUnavailableError: Cannot connect to Docker daemon: connection refused");

    // With nothing of the provider's to show, the row names itself the way its section does.
    const silent = { status: "ERROR" as const, error_type: null, error_message: null };
    expect(providerRowProblemFor(silent, "account")?.message).toMatch(/^Couldn't check this account\./);
    expect(providerRowProblemFor({ status: "OK", error_type: null, error_message: null }, "provider")).toBeNull();
  });

  it("summarises the panel by rows needing attention rather than errors", () => {
    expect(providersSummaryFor(3, 0)).toBe("3 providers");
    expect(providersSummaryFor(1, 1)).toBe("1 provider, 1 needs attention");
    expect(providersSummaryFor(5, 2)).toBe("5 providers, 2 need attention");
  });

  it("keeps a removal's refusal visible, since the user can act on it, and hides anything else behind details", () => {
    const inUse = connectionRemovalErrorFor({
      status: 409,
      body: { error: "This connection still has 2 active workspaces. Remove them first, then remove the connection." },
    });
    expect(inUse.message).toBe(
      "This connection still has 2 active workspaces. Remove them first, then remove the connection.",
    );
    expect(inUse.details).toContain("HTTP 409");

    const unknown = connectionRemovalErrorFor({ status: 404, body: { error: "Unknown cloud account 'modal_abc'." } });
    expect(unknown.message).toBe("Couldn't remove this connection. Try again. If this persists, report a bug.");
    expect(unknown.details).toBe("HTTP 404\nUnknown cloud account 'modal_abc'.");

    const unanswered = connectionRemovalErrorFor({ status: 0, body: null });
    expect(unanswered.message).not.toContain("internet");
    expect(connectionRemovalErrorFor({ status: 409, body: null }).message).toContain("Couldn't remove this connection");
  });

  it("keeps a 409 that carries no sentence of the route's off the visible line", () => {
    const proxyText = connectionRemovalErrorFor({ status: 409, body: new TextResponseBody("upstream conflict") });
    expect(proxyText.message).toBe("Couldn't remove this connection. Try again. If this persists, report a bug.");
    expect(proxyText.details).toBe("HTTP 409\nupstream conflict");

    const detailOnly = connectionRemovalErrorFor({ status: 409, body: { detail: "lease held by host-abc" } });
    expect(detailOnly.message).toBe("Couldn't remove this connection. Try again. If this persists, report a bug.");
    expect(detailOnly.details).toContain("lease held by host-abc");
  });

  it("disables Remove while workspaces use the connection or a newer app owns it", () => {
    expect(removeControlFor({ workspace_count: 0, status: "OK" })).toEqual({
      isEnabled: true,
      tooltip: "Remove this connection",
    });
    expect(removeControlFor({ workspace_count: 2, status: "OK" })).toEqual({
      isEnabled: false,
      tooltip: "In use by 2 workspaces. Remove them first.",
    });
    expect(removeControlFor({ workspace_count: 1, status: "ERROR" }).tooltip).toBe(
      "In use by 1 workspace. Remove it first.",
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
