# Plan: `mngr forward` last-known service-map cache (fast first-load)

Design source: `docs/design/forward-fast-first-load.md`. This spec implements the
"seed from last-known" direction (the doc's option (b) family), located entirely
in the `mngr_forward` plugin.

## Overview

- **Problem:** on launch, `mngr forward` starts with an empty routing table.
  `resolve()` returns `None` (the 503 "Loading workspace" loader) until the slow
  per-agent `mngr event … services --follow` stream delivers the agent's service
  URL. Measured live: a cold single stream takes ~10s; under spawn contention
  across many `is_primary` agents the tail workspace waits ~50-55s. Local Docker
  minds load in ~20s.
- **Fix:** persist the resolver's per-agent service map to disk while the plugin
  runs, and **seed** the resolver from that cache at startup. The route becomes
  resolvable as soon as fresh discovery supplies membership + SSH info (~t+3s),
  instead of after the cold event stream connects. The live stream still runs and
  overwrites the seed as soon as it delivers.
- **Why in `mngr_forward` (not `mngr event` / not discovery):** the resolver
  already holds the derived `{service → url}` map and already serializes it on
  every change; caching a small derived snapshot through that existing seam is far
  smaller than adding a durable raw-event mirror to the generic `mngr event`
  command or extending the discovery snapshot schema + every provider. It is also
  provider-agnostic by construction.
- **Why stale seeds are safe here:** `resolve()` only uses a service entry for an
  agent that *this launch's* fresh discovery lists as known and reachable
  (`resolver.py:157-159`), so a cache entry for a destroyed/replaced agent is
  never consulted. Service ports are fixed (measured: `system_interface` pinned to
  `localhost:8000` across container restarts), so a stale cached URL is almost
  always still correct; the SSH tunnel target is always fresh from discovery.
- **Scope decisions (from Q&A):** no provisional/grace window (accept the narrow
  fixed-port risk); no cache freshness bound (always seed, rely on
  discovery-membership gating + live-stream overwrite); an empty/absent cache must
  behave exactly as today.

## Expected behavior

- On relaunch with a warm cache, a restored window onto a **remote** mind whose
  container is up loads in ~t+3s (bounded by discovery-snapshot replay) instead of
  ~50-55s; local minds improve similarly.
- The seeded route is corrected automatically: when the live `mngr event … services`
  stream connects (~10s+), it replaces the seeded map with current data via the
  existing `update_services` path — a full replace, so a stale entry cannot linger.
- A seeded route is only ever served for an agent that this launch's discovery
  confirms is known and has SSH info; otherwise `resolve()` returns `None` and the
  loader shows exactly as today.
- **First-ever launch / just-created agent / cold container (no cache entry):**
  identical to today — the resolver stays empty for that agent until the live
  stream delivers; no regression.
- **Stale seed edge case (rare):** if a cached URL is wrong (same agent id,
  running, but a service moved ports), the proxy dials a dead port and emits the
  normal `CONNECT_ERROR` backend-failure envelope until the live stream corrects
  (~10s). With no grace window this can, in that narrow window, feed minds' STUCK
  detector (5s threshold) — accepted as near-impossible for fixed-port services.
- No change to the envelope stream minds consumes, to auth, to the per-agent live
  streams, to discovery, or to any provider. No new CLI flags required for the
  default minds flow.

## Changes

- **Persist the resolver service map.** While the plugin runs, write the current
  per-agent `{service → url}` map to a cache file under the existing plugin state
  dir (`$MNGR_HOST_DIR/plugin/forward/`, alongside the auth signing key). The write
  hooks the resolver's existing "services changed" mutation point (the same point
  that already emits `resolver_snapshot`), so the cache always reflects the live
  map. Writes are atomic (temp file + rename) and tolerant of I/O errors (best
  effort; a failed write must never break forwarding).
- **Seed at startup.** At plugin startup (observe / `--observe-via-file` modes),
  before/alongside starting the streams, load the cache file and populate the
  resolver's per-agent service map from it. Seeding only fills the service map;
  `resolve()` still waits on discovery-supplied membership + SSH info, so no route
  is served for an agent discovery hasn't confirmed.
- **Cache lifecycle / invalidation.** Removing an agent from the resolver
  (destruction / bulk discovery reconcile) drops its cache entry, mirroring the
  existing in-memory `_services_by_agent` removal. No freshness bound and no
  age-based eviction (per Q&A). The cache is scoped per `MNGR_HOST_DIR`, so
  staging/prod/local minds keep separate caches automatically.
- **No behavior when empty/absent.** A missing, empty, or unreadable cache file is
  a no-op seed — the resolver starts empty for those agents, exactly as today.
- **Out of scope (explicitly not built):** the priority-agent hint (option c), the
  discovery-snapshot service map (option a), any persistent per-agent streams or
  supervisor changes, and any grace/provisional-route logic. These remain future
  options documented in `docs/design/forward-fast-first-load.md`.
- **Residual open item to verify during implementation:** minds' recovery-redirect
  freshness math compares outage onset against discovery snapshot timestamps
  (`system_interface_health.py`); confirm a seeded route does not perturb that
  comparison.
- **Tests:** cover seed-from-cache populates the resolver; live stream overwrites a
  seeded entry; a seeded entry is not served until discovery marks the agent known
  with SSH info; destruction drops the cache entry; empty/absent/corrupt cache is a
  safe no-op. A changelog entry under `libs/mngr_forward/changelog/` is required.
