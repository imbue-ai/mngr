# Discovery process and logging cleanup for the Minds app

## Overview

- Minds' logs are flooded with repeated `Discovery error from <provider>: ...` warnings (thousands per session for gcp/aws-* alone): every `mngr list` emits a `DISCOVERY_ERROR` event per unauthorized provider, and each of the three stream consumers (minds `forward_cli`, `mngr_forward` `stream_manager`, `mngr_latchkey` `discovery_stream`) logs a warning for every such event line, every poll cycle.
- Fix the spam on the consumer side: a shared once-per-process suppression for provider-level discovery errors, re-armed only when that provider's outcome changes (different error, or a clean snapshot).
- Make provider treatment consistent: all installed providers are enabled by default in the controlled config file, including the four minds-written `[providers.aws-<region>]` blocks, which are currently gated on plausible AWS credentials. Unauthorized providers error visibly in the providers panel instead of being silently absent.
- Make the observe discovery stream authoritative about skipped providers: providers that fail construction (unauthorized/unavailable) or are known-empty get one startup snapshot so the panel sees their state from the stream, not only from `mngr list` side effects.
- The producer's own per-poll "slow" / "timed out" / "poll failed" warnings stay as-is: a wedged provider with a live poller is an ongoing problem worth repeated warnings.

## Expected behavior

- A minds session with unauthorized providers (no AWS/GCP/Azure/OVH/Vultr credentials) logs exactly one warning per provider per consumer process, suffixed with a note that repeats are suppressed until the outcome changes. Subsequent poll cycles add no log lines for the same error.
- If a provider's error changes (different type or message), the new error is logged once, then suppressed again.
- When a previously-errored-and-logged provider has its first clean discovery, a one-line recovery message is logged at info level, re-arming suppression for that provider.
- Suppression applies only to provider-level errors (the provider's discovery as a whole failed). Host- or agent-attributed discovery errors keep logging on every occurrence, even when they carry a `provider_name` — their recovery cannot be reliably detected.
- Suppression is per consumer process: each of the three consumers logs at most once per provider+error, and once more after a process restart. Historical `DISCOVERY_ERROR` lines re-read when a consumer attaches to the events file are covered by the same dedup — no special replay handling.
- `mngr observe --discovery-only` still skips unauthorized/unavailable/empty providers at startup (no pollers for them), but now emits one startup `DISCOVERY_PROVIDER` snapshot per skipped provider: with the `error` field set for unavailable/unauthorized providers, or with no error and zero agents/hosts for empty ones. Each snapshot includes the provider's `DiscoveredProvider` config (resolvable from mngr config without constructing the provider).
- The providers panel therefore shows every installed provider from the stream alone, including errored ones — for a user with no AWS credentials that means four `aws-<region>` rows with error badges, which is acceptable (no panel-side collapsing).
- The skip snapshot is emitted once per observe process startup only. Its panel "last discovery" timestamp ages until the process restarts — accurate, since the provider genuinely isn't being polled.
- The full `mngr observe` (without `--discovery-only`) inherits all of this automatically: it consumes the `--discovery-only` stream as a subprocess.
- Unexpected construction errors (anything other than unavailable/unauthorized/empty) keep propagating and crash observe startup loudly, as today.
- Mid-run authorization is not picked up by the running observe process: recovery happens via the next `mngr list`, an observe bounce, or an app restart. Minds bouncing observe when credentials are set from within the app is future work.
- minds' bootstrap always writes all four `[providers.aws-<region>]` blocks into the controlled settings.toml, regardless of AWS credentials. On rewrite, an existing `is_enabled` value is preserved (so a panel-toggled disable survives); every other field in these blocks is minds-controlled and re-pinned.
- When the bootstrap's settings write actually modifies the file (e.g. the AWS blocks first appear after an upgrade), minds bounces the observe child so the new provider set is discovered without waiting for a signin or watchdog restart.
- New providers added in the future follow the same pattern: always write their controlled-config blocks, let them error visibly when unauthorized.
- `mngr list` behavior is unchanged: it still emits `DISCOVERY_ERROR` events and per-provider snapshots as today; its own CLI output is untouched.
- Out of scope: the docker `Skipped container <id>: no host record` per-poll warning (separate task); collapsing AWS rows in the panel; root-causing the host-level `local: 'ssh' (KeyError)` and imbue_cloud `FileNotFoundError` errors (separate tasks); cutting a minds release (normal cadence).

## Changes

- Add a shared discovery-error log-suppression helper in `libs/mngr` (alongside the discovery event types) that tracks, per process, the last logged provider-level error per provider (keyed by provider + error type + message), decides log-vs-suppress, appends the "suppressing repeats" note to the first occurrence, detects the errored-to-clean transition from provider snapshots, and emits the info-level recovery line only for providers whose error was actually logged.
- Wire the helper into the three consumers that log `Discovery error from ...`: minds `desktop_client/forward_cli.py`, `mngr_forward/stream_manager.py`, and `mngr_latchkey/discovery_stream.py`. Provider-level errors go through the helper; host/agent-attributed errors keep logging directly.
- Feed clean provider snapshots through the helper in each consumer so suppression re-arms on recovery.
- Extend the `--discovery-only` stream startup (`run_per_provider_discovery_stream` in `libs/mngr/imbue/mngr/api/provider_discovery_stream.py`) to enumerate providers whose construction was skipped (unavailable/unauthorized/empty) and emit one startup snapshot each: error snapshot for unavailable/unauthorized, clean empty snapshot for empty, both carrying the provider's config resolved from mngr settings.
- Remove the `_aws_credentials_plausibly_configured` gate in minds `bootstrap.py` so `_desired_aws_provider_names` always returns all `CONFIGURED_AWS_REGIONS`; adjust `_write_aws_provider_blocks` to preserve an existing block's `is_enabled` value while re-pinning all other fields.
- Have `_ensure_mngr_settings` report whether it modified the settings file, and have minds bounce the latchkey observe child when it did (same pattern as the signin path).
- Update changelog entries for every touched project: `libs/mngr`, `libs/mngr_forward`, `libs/mngr_latchkey`, `apps/minds`.
- Verification: unit tests for the suppression helper (log-once, re-arm on change and on clean snapshot, recovery line, host-level errors unaffected), the startup skip-snapshots, and the bootstrap AWS-block behavior (always written, `is_enabled` preserved, modified-flag returned); plus one manual minds dev-stack run confirming quiet logs across poll cycles with error badges still visible in the providers panel.
