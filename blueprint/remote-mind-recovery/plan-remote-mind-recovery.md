# Extend minds recovery logic to remote (imbue_cloud) minds

## Overview

- Recovery was designed and tested only against local providers (docker/lima); the imbue_cloud provider wasn't finalized at the time, so remote behavior was never properly handled. Remote minds can be funneled to the recovery page on a transient blip and offered a destructive host restart that can break an otherwise-healthy mind.
- Core change: gate recovery on **provider reachability**, and never take a destructive action that can't help. If the provider (imbue_cloud connector / docker daemon) is unreachable, show a dedicated page instead of offering a restart; otherwise fall through to the existing host-state classification.
- Provider reachability is read from the `mngr list` the probe already runs (it round-trips the connector for imbue_cloud and the daemon for docker); imbue_cloud starts raising the specific `ProviderUnavailableError` so the signal is typed. No new probe and no new top-level CLI command.
- The destructive container stop is backstopped two ways: the provider-unavailable tier takes precedence over the manual-restart tier (so you're never offered a destructive restart while offline), and a live interface re-probe runs immediately before the stop (so a blip that has cleared can't trigger it). Note an unreachable host can't be destructively stopped anyway — the stop physically can't run over its SSH — so the dangerous case is only ever "host reachable + mind actually fine," which the re-probe catches.
- Behavior applies uniformly across providers (a down local docker daemon is treated the same: don't offer a restart that can't help).

## Expected behavior

- Brief connectivity blips no longer throw remote minds onto the recovery page: remote minds tolerate a longer sustained-failure window before going STUCK, and first show the existing "Loading workspace" state, escalating to the recovery page only if the failure persists. Local minds keep their current entry timing.
- Provider unreachable (your connection is down, or Imbue Cloud is down) → a "Can't connect to Imbue Cloud" page with Retry and **no** restart option.
- Provider reachable → recovery behaves much as today but prefers non-destructive actions: a stopped container is started in place; a wedged interface is restarted in place. The auto interface restart only fires after a sustained/confirmed failure, not a single probe.
- The destructive "Restart workspace" (container stop + start) remains available for remote minds, but immediately before it stops the container it re-checks the interface, and if the mind has recovered on its own it silently aborts and returns the user to the workspace.
- Provider-surfaced auth/account problems (expired login, no account configured) show a plain "can't reach your workspace — <reason>" message with no restart (a restart can't fix them) and are not treated as a connectivity outage.
- The "Can't connect to Imbue Cloud" page keeps polling in the background (backing off over time, not spamming) and automatically returns the user to the workspace once it's reachable again; a manual Retry is also available.
- The same behavior holds for local providers: a down docker daemon yields the provider-unavailable page and suppresses the restart affordance.

## Changes

- Feed provider-reachability into recovery classification, derived from the `mngr list` probe (now carrying a typed `ProviderUnavailableError` in its `errors[]`); the existing host-state / in-container exec probes continue to drive the existing tiers. No separate active reachability probe is added.
- imbue_cloud raises the specific `ProviderUnavailableError` when the connector is unreachable (today it raises nothing provider-specific from the discovery path).
- minds parses the `mngr list` JSON even when the command exits non-zero (today the stdout is discarded on a non-zero exit), so the typed `errors[]` is available to classification; scope this to the recovery probe rather than changing the shared mngr-runner globally.
- Add one classification tier, provider-unavailable, with precedence **above** the existing host/interface tiers (notably the manual-restart `HOST_UNRESPONSIVE` tier). Because it takes precedence, the recovery page shows the provider-unavailable page — and nothing auto-dispatches — when the provider is unreachable, without wrapping each dispatch path individually.
- The provider-unavailable tier keys on `errors[].exception_type == "ProviderUnavailableError"`, which works **only if imbue_cloud raises that error narrowly** — for genuine connector-unreachability, never for auth/account-config failures (those must keep raising their own distinct types so they fall into the generic bucket below).
- Distinguish connector-unreachable (→ provider-unavailable: retry, no restart) from auth/account-config failures (→ generic "can't reach — <reason>": no restart, no dedicated recovery flow). Do not lump all of `errors[]` into provider-unavailable.
- Add a pre-stop re-confirmation to the destructive `--stop-host` path: re-probe the interface immediately before the stop and abort — silently returning the user to the workspace — if it now responds. The surgical restart and plain `mngr start` dispatch are unchanged (no re-confirmation).
- Make remote minds more reluctant to enter recovery: a longer sustained-failure window before STUCK, plus a lighter first state that reuses the existing "Loading workspace" loading page, escalating to the recovery page only on persistence. This requires teaching `SystemInterfaceHealthTracker` about remoteness — it is keyed by agent id with a single global threshold today, while provider/remote info is only available at the host-health probe layer — so a remote-aware window means plumbing remoteness into the tracker.
- Gate the auto interface (`interface_unresponsive`) restart on a sustained/confirmed failure rather than a single host-health probe.
- Add the provider-unavailable page, with a manual Retry and background polling that backs off over time and auto-returns to the workspace on recovery.
- Apply the new classification and page uniformly to local providers (docker daemon down → provider-unavailable page, restart suppressed).
- Explicitly out of scope: a dedicated "host is down" page for the rare provider-reachable-but-host-unreachable case (a server-side VPS fault mngr can't fix anyway) — such a host keeps classifying as offline and falls through to existing behavior; the destructive stop can't run against it, so nothing is lost on safety. Also out of scope: the `CRASHED`-reconfirmation cleanup (no longer needed without the host-down page).
- Add a changelog entry under `apps/minds` (and `libs/mngr_imbue_cloud` / `libs/mngr` for the `ProviderUnavailableError` and list-error changes) per the repo's per-project changelog rule.
