## New providers panel on the landing page

- The landing page now includes a Providers section listing every configured provider (except `local`, which is always present and always healthy). Each entry shows the provider name, backend type, a status badge (OK / Error / Disabled), the last error message verbatim when applicable, and an Enable or Disable button.
- Two small freshness counters at the top of the panel show "time since last discovery event" and "time since last full discovery event" so a stalled discovery loop is immediately visible.
- Clicking Disable on a working or errored provider, or Enable on a disabled one, writes `is_enabled` to minds' active settings file and bounces `mngr observe` so the change takes effect on the next poll. The button shows "Waiting…" until the next full snapshot lands.

## No more silent auto-disable on auth errors

- Previously, when discovery surfaced `ImbueCloudAuthError`, minds would silently rewrite the user's settings to set `is_enabled = false` on the offending `imbue_cloud_<slug>` provider. That entire path is gone: `_ImbueCloudAuthErrorDisabler` and the provider-error callback plumbing on `EnvelopeStreamConsumer` are removed.
- The same outcome is now user-driven: an errored `imbue_cloud_<slug>` provider shows up in the providers panel with the verbatim error message; the user clicks Disable to silence it, or fixes the upstream auth and the provider recovers on the next snapshot.

## Agents no longer silently disappear when a provider fails

- When a provider (e.g. Modal or imbue_cloud) fails discovery, its agents previously vanished from the landing page agent list with no explanation. Now `AgentObserver` emits an `UNKNOWN` agent state for previously-observed agents on errored providers (sticky until they reappear or are explicitly destroyed). The landing page's agent list itself still shows only currently-discovered agents, but the providers panel surfaces the underlying provider error so the user can see *why* an agent might be missing.
- `mngr_notifications` users: see that project's changelog for the new `RUNNING -> UNKNOWN -> WAITING` transition handling.

## Internal: `set_provider_is_enabled`

- `disable_imbue_cloud_provider_for_account` was renamed to `set_provider_is_enabled(provider_name, is_enabled)` and generalized to work on any provider name. All callers in `apps/minds/` are migrated; no compatibility shim. The function writes to minds' active settings file and creates the `[providers.<name>]` block if it doesn't yet exist.
