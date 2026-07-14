# Host-start readiness race (lima "Running but sshd not up yet")

## Purpose and audience

This is an analysis document, not an implementation plan. It captures a diagnosed
race between two concurrent `mngr` invocations against a lima host, verifies the
root cause against the code, surveys how every other entry point behaves against
the same mid-boot window, enumerates candidate resolutions with their trade-offs,
and makes a single recommendation. It is aimed at whoever implements the fix and
at reviewers who need to understand why that fix is scoped the way it is.

Related: `specs/lima-provider/concise.md`, `specs/provider-shape.md` (§1.5 the
`start_host` idempotency contract, §1.9 error taxonomy), `specs/implementing-a-provider.md`.
This document is the resolution owner for the corresponding entry in
`specs/uncertainties.md` (the `mngr start` comment/code disagreement).

## Summary

- **What happens.** A lima host stopped out-of-band is booted by one `mngr`
  process (an auto-starting `mngr exec`). ~10s into the ~20-30s boot, a second
  process runs `mngr start --quiet`. Lima reports the instance as `Running` from
  the instant the hostagent spawns, so the second process classifies the host
  online and skips all start machinery, then its first SSH operation fails with a
  raw TCP connection-refused, five seconds before the boot actually finishes. The
  VM was fine; the failure was spurious.
- **Root cause (verified).** The implicit contract is that a provider returning an
  online `Host` from `get_host` means "connectable right now." Lima breaks it: it
  classifies online purely from the `limactl list` status string `"Running"` with
  no readiness probe (`libs/mngr_lima/imbue/mngr_lima/instance.py:960-976`). The
  only readiness gate (`wait_for_sshd`) lives *inside* `start_host`, and
  `ensure_host_started` reaches `start_host` only when the provider classified the
  host offline (`libs/mngr/imbue/mngr/api/find.py:337-344`). Serial usage never
  exposes this because the only path to `Running` is mngr's own `start_host`, which
  does not return until `wait_for_sshd` passes; only a concurrent second process
  can observe the mid-boot window.
- **Recommendation (direction 3 below).** Add a scoped readiness gate at the
  `ensure_host_started` seam: when the caller intends to start the host
  (`is_start_desired=True`) and the provider returned an already-online host,
  confirm the host is connectable via a bounded, provider-supplied readiness wait
  before handing it back. Implement it as a provider method with a default no-op
  (preserving today's behavior for providers whose "online" already implies
  connectable) and a lima override that runs `wait_for_sshd` against the live SSH
  endpoint. This acts on precise knowledge (a real SSH transport handshake, not
  error-shape), adds no persistent state, leaves the hot `get_host` read path
  untouched, and avoids the residual tight race that routing through `start_host`
  would introduce. Two independent latent bugs the survey surfaced
  (`gc_work_dirs` exception breadth; `destroy` mid-boot escalation) are called out
  separately.

## Background: the flow and the implicit contract

### The pieces

- `mngr start` (`libs/mngr/imbue/mngr/cli/start.py`, `_start_agents`) groups the
  target agents by host and, per host, calls `provider.get_host(...)` then
  `ensure_host_started(host, is_start_desired=True, provider=provider)`
  (`start.py:233-238`).
- `ensure_host_started` (`libs/mngr/imbue/mngr/api/find.py:326-351`) has no judgment
  of its own. It dispatches on the static type of the returned host:
  - `case Host()` (an online host) -> return it untouched, `was_started=False`.
  - `case HostInterface()` (offline, e.g. `OfflineHost`) -> if `is_start_desired`,
    call `provider.start_host(offline_host)` and return the result; else raise
    `UserInputError`.

  So `start_host` (and the readiness wait it contains) is reached **iff the
  provider classified the host offline**.
- lima's `start_host` (`libs/mngr_lima/imbue/mngr_lima/instance.py:741-806`) is the
  correct readiness gate: `limactl start` (a no-op on a `Running` instance),
  `_get_ssh_config` reads the live forwarded port, then
  `wait_for_sshd(hostname, port, ssh_connect_timeout)` — a full SSH transport
  handshake (`libs/mngr/imbue/mngr/providers/ssh_utils.py:242-270`), default
  `ssh_connect_timeout=120s` (`libs/mngr_lima/imbue/mngr_lima/config.py:96`). Had
  `mngr start` routed through `start_host`, this incident resolves cleanly: the
  wait absorbs the tail of the in-flight boot.
- After `ensure_host_started`, `mngr start` immediately does SSH work: it takes the
  cooperative host lock (`start.py:248`) and checks `path_exists` on each agent's
  state dir (`start.py:254-255`). The cooperative lock is itself a `flock(2)` taken
  **inside the guest over SSH** (`libs/mngr/imbue/mngr/hosts/host.py:806-832`), so
  it cannot coordinate a boot — it is unreachable exactly when this race happens.
  The first SSH connect goes through `OuterHost._ensure_connected`
  (`libs/mngr/imbue/mngr/hosts/outer_host.py:385-398`), which maps paramiko's
  `ConnectError` to a base `HostConnectionError` with the message
  `"Failed to connect to host: ..."` — matching the incident log exactly.

### The implicit contract

Across the codebase, "`get_host` returned an online `Host`" is treated as
"connectable right now." Nothing in the interface states this:
`ProviderInstanceInterface.get_host` is documented only as "Retrieve a host by its
ID or name, raising HostNotFoundError if not found"
(`libs/mngr/imbue/mngr/interfaces/provider_instance.py:649-655`), and `start_host`
as "Start a stopped host, optionally restoring from a specific snapshot"
(`:539-546`) — the wording presumes the host is *stopped* and makes no idempotency
or online-safety guarantee. The contract is real but implicit, and lima is the
provider that violates it.

## The incident (2026-07-13 minds staging)

1. A lima host was stopped out-of-band (`limactl stop`).
2. A background "backup check" ran `mngr exec` against an agent on the host. Because
   the instance status was not `Running`, `get_host` classified it offline,
   `ensure_host_started` routed through `start_host`, and a `limactl` boot went in
   flight (boot ~20-30s; lima hostagent logged `READY` at 16:15:53 PDT).
3. ~10s into that boot, the minds recovery flow ran `mngr start <agent> --quiet`.
   `get_host` now saw status `Running` (the hostagent had spawned), classified the
   host online, and `ensure_host_started` returned it untouched. The first SSH
   operation hit connection-refused and the command exited 1 after ~3.9s with
   `Error: Failed to connect to host: ... Unable to connect to port 57282 on
   127.0.0.1` — about five seconds before the boot finished.

The two actors are asymmetric on purpose: actor 1 booted the host because it
observed the host genuinely offline (status not `Running`), so it took the
`start_host` path with its readiness wait. Actor 2 observed the same host mid-boot
(status `Running`), so it took the online short-circuit and skipped the wait.

## Verification of the traced analysis

Every claim in the original diagnosis was checked against the code and holds:

- `ensure_host_started` short-circuits online hosts and only calls `start_host` on
  the offline branch — confirmed (`find.py:337-344`).
- lima classifies online from `limactl list` status `== "Running"` with no SSH
  probe, then builds the host object directly — confirmed
  (`instance.py:960-976`). There is no `Booting` status and no readiness field in
  the listing; discovery maps statuses via `_LIMA_STATUS_TO_HOST_STATE`
  (`instance.py:1034`) but `_get_host_by_id` only checks for `"Running"`.
- lima's `start_host` contains the readiness gate and `limactl_start_existing`
  runs `limactl start <instance>` with `is_checked_after=False`, raising
  `LimaCommandError` on nonzero exit (`libs/mngr_lima/imbue/mngr_lima/limactl.py:254-273`)
  — confirmed. The claim that `limactl start` on a `Running` instance exits 0
  ("already running") without waiting, and that lima has no start lock (a tighter
  collision yields a nonzero "seems running" error), comes from the incident's
  reading of lima's Go source (`pkg/instance/start.go`); it is not verifiable from
  this repo and is treated as reported. It matters only for direction 1's residual
  race.
- The cooperative host lock is an in-guest `flock` over SSH and cannot coordinate a
  boot — confirmed (`host.py:806-832`, `lock_cooperatively` splits into
  `_hold_local_host_lock` / `_hold_remote_host_lock`).
- The `start.py:235-237` comment describes routing through `start_host`
  unconditionally, which the code does not do — confirmed, and logged in
  `specs/uncertainties.md`.

## Provider survey

Two axes matter: how each provider classifies a host as online (does "online" imply
"connectable"?), and whether its `start_host` is safe to call on an already-running
host (relevant to direction 1). File references are to each provider's `start_host`
and `get_host`.

### Online classification — does "online" imply "connectable"?

| Provider | Online signal | Connectable when classified online? |
|---|---|---|
| local | Always online | Yes (trivially) |
| docker | Container `status == "running"` (`providers/docker/instance.py:1102-1105`, `:1887`) | Effectively — a started container is exec-able almost immediately; negligible window |
| lima | `limactl list` status `== "Running"` (`mngr_lima/.../instance.py:962`) | **No** — 15-25s boot window where status is `Running` but guest sshd refuses |
| vps (Vultr/OVH) | Live: SSH the outer VPS, `realizer.is_placement_running` (`mngr_vps/.../instance.py:1516-1538`) | Mostly — a working outer transport was just demonstrated; inner container-sshd has a small window |
| vps offline (AWS/GCP/Azure) | Delegates to vps live check, offline fallback on `HostNotFoundError` (`instance_offline.py`) | Mostly — same as vps |
| imbue_cloud | Live: probe inner container over outer root SSH, `docker_inspect_running` (`mngr_imbue_cloud/.../instance.py:1075-1093`) | Mostly — but returns online optimistically when the per-host key is absent (`:1085-1087`) |
| modal | Live Modal sandbox found via API (`mngr_modal/.../instance.py:2257`) | Yes — a running sandbox is exec-able through the handle |
| ssh | Every configured host hard-coded `RUNNING`, no probe (`providers/ssh/instance.py:265`) | Not verified at all (out of scope; see below) |

lima is the outlier on both counts: the cheapest possible classification (a status
string read, no network round-trip) combined with the longest connectable-lag. The
VPS-family checks are genuine live SSH checks; even they do not prove the *inner*
container sshd is up, but their window is short (sshd is launched via `docker exec`
at start) and the reported incident is lima-specific.

### `start_host` idempotency against an already-running host

This matters only for direction 1 (route `mngr start` through `start_host`
unconditionally). Summary of the audit:

- **Self-guarded / safe to double-call:** docker (short-circuits before
  `container.start()` when already running, `providers/docker/instance.py:1518-1532`),
  modal (short-circuits when the sandbox is live, `mngr_modal/.../instance.py:1983-1992`),
  local (pure no-op, `providers/local/instance.py:239-241`).
- **Runs unconditionally, with side effects that re-fire on a running host:**
  - **lima** (`mngr_lima/.../instance.py:741-806`): always calls `limactl start`,
    rewrites the host record's SSH host/port/user/identity, clears `stop_reason`,
    records BOOT activity, and **re-execs the in-VM activity watcher**
    (`:801-803`) — risking a duplicate watcher.
  - **vps base -> Vultr/OVH** (`mngr_vps/.../instance.py:1346`, docker realizer): a
    no-op `docker start` followed by an **unconditional `start_container_sshd`**
    (launches a second in-container sshd), BOOT activity reset, and a relaunched
    activity watcher.
  - **vps offline -> AWS/GCP/Azure** (`mngr_vps/.../instance_offline.py:226`): the
    cloud `start_instance` API is documented idempotent, but the wrapper still
    re-rebinds known_hosts, rewrites the host record, **mirrors it to the external
    state store**, and runs the whole base chain (duplicate sshd + watcher).
  - **imbue_cloud** (`mngr_imbue_cloud/.../instance.py:1769`): no-op `docker start`
    then an unconditional `start_container_sshd` (duplicate sshd).
- **Raises:** **ssh** — `start_host` raises `NotImplementedError`
  (`providers/ssh/instance.py:197`), and ssh hosts are *always* classified online,
  so routing through `start_host` unconditionally would newly fail on every online
  ssh host.

There is no declared idempotency contract in the base class or interface
(`BaseProviderInstance.start_host` is a bare `raise NotImplementedError`,
`providers/base_provider.py`); §1.5 of `specs/provider-shape.md` states the
*intended* contract ("MUST be idempotent... if already running, return success with
no API call or at most a cheap status check"), but several providers do not honor
it today.

## Blast radius: other entry points against a mid-boot lima host

The error taxonomy is load-bearing here. `HostConnectionError` is the base;
`HostOfflineError` and `HostAuthenticationError` are its subclasses
(`libs/mngr/imbue/mngr/errors.py:92-101`). A mid-boot connection-refused surfaces
as the **base** `HostConnectionError`, so a handler that catches only
`HostOfflineError` misses it. No general retry currently bridges the window: the
`config.retry` machinery guards only the interactive `ssh` attach subprocess in
`connect_to_agent`, and `_retry_on_transient_ssh_error`
(`outer_host.py`) matches only mid-stream channel deaths on an already-established
connection, not an initial-connect refusal.

Behavior against a lima host that is status-`Running` but sshd-refusing:

| Entry point | Goes through `ensure_host_started`? | First SSH op | Outcome today |
|---|---|---|---|
| `mngr start` | Yes (`start.py:238`) | lock / `path_exists` | **Fatal** — the reported incident |
| `mngr exec` (multi, CLI) | Yes (`api/exec.py:201`) | `get_agents` | Per-agent failure recorded; continue-mode non-fatal, abort-mode stops |
| `mngr exec` single / `capture` | Yes (`find.py:668`) | `get_agents` (`find.py:669`) | **Fatal** (no catch) |
| `mngr connect` | Yes (`find.py:668`) | `get_agents` | **Fatal** before the retry-capable attach loop is reached |
| `mngr create --reuse` | Yes (`create.py:1370`) | `get_agents` | **Fatal** |
| `mngr create` (existing target host) | Yes (`create.py:1480`) | provisioning ops | **Fatal** |
| `mngr create --from agent` | via `resolve_host_location_address` (`create.py:1459`) | `discover_agents` (`find.py:237`) | **Fatal** |
| `mngr rename --start` | Yes (`rename.py:234`) | `rename_agent` | **Fatal** |
| `mngr message` | Yes (`api/message.py:117`) | `get_agents` | Continue: silently skips host; abort: fatal |
| `mngr rsync` / `git` | via `resolve_host_location` | `discover_agents` (if agent named) or the rsync/git ssh subprocess | **Fatal** |
| `mngr stop AGENT` | **No** — `get_host` directly (`stop.py:378`) | `stop_agents` (`stop.py:388`) | **Fatal** |
| `mngr stop --stop-host` | No (SSH-free) | none | **Safe** by design |
| `mngr destroy AGENT` | **No** — `get_host` directly (`destroy.py:429`) | `get_agents` (`destroy.py:434`) | **Reclassified offline** -> can escalate to whole-host `destroy_host` |
| `mngr gc` (machines) | No | `discover_agents` (`gc.py:396`) | **Safe** — `HostConnectionError` caught (`gc.py:405-408`), host skipped, never destroyed |
| `mngr gc` (work dirs) | No | `get_certified_data` (`gc.py:800`) | **Aborts gc** — catch is `HostOfflineError`/`HostAuthenticationError` only (`gc.py:237-240`), base `HostConnectionError` escapes |
| `mngr list` / discovery | No | `discover_agents` | **Safe** — offline fallback recovers agents from local records |

Three findings deserve separate attention because they are independent of the
`mngr start` fix:

1. **`gc` machines is safe by construction and does not destroy a mid-boot host.**
   `_gc_single_host` catches both `HostAuthenticationError` and the base
   `HostConnectionError` around `discover_agents` and every subsequent probe and
   returns (skips) rather than destroying (`gc.py:399-408`, `:415-419`, `:436-438`,
   `:450-452`). Destruction requires either an offline classification (which
   mid-boot does *not* produce) or a successful empty `discover_agents`. Good — no
   data-loss risk here.
2. **`gc` work-dirs is a real, separate bug.** `_gc_single_host_work_dir` catches
   only `HostOfflineError`/`HostAuthenticationError` (`gc.py:237-240`); the base
   `HostConnectionError` a mid-boot host raises escapes and, in a standalone
   `mngr gc` (default abort), aborts the whole sweep in the work-dirs phase — which
   runs before the machines phase. It does not destroy anything, but it is
   inconsistent with the machines phase's deliberate skip-on-`HostConnectionError`.
3. **`mngr destroy` escalation is a real, separate hazard.** `_resolve_host_for_partition`
   turns a mid-boot `HostConnectionError` into "treat host as offline"
   (`destroy.py:435-445`); if all the host's agents are targeted, the offline path
   calls `provider.destroy_host` — tearing down the whole VM — where a moment later
   (once sshd is up) it would do a per-agent destroy. The VM's data survives on the
   volume, but this is a surprising mid-boot-driven divergence.

## Design constraints (hard requirements from the incident owner)

- **No generic retry on connection errors.** A paramiko refused-connection is
  indistinguishable from "sshd crashed" or "wrong port"; retrying on that
  error-shape is unacceptable. The fix must act on precise knowledge.
- **No new persistent state or caching.** No "starting in progress" markers, no
  freshness caches. Prefer idempotent operations and precondition checks at commit
  time over recording intent. (A bounded, in-process readiness *wait* is not
  persistent state — it records nothing and leaves no artifact.)
- **Keep coupling low; `get_host` is a hot read path.** Adding an SSH handshake to
  every `get_host` call is likely unacceptable; any per-call cost must be evaluated
  honestly.

## Candidate resolutions

### Direction 1 — route `mngr start` through `start_host` unconditionally

Make `mngr start` (and, for consistency, the other `is_start_desired=True` callers)
call `provider.start_host` even when `get_host` returned an online host, matching
the aspirational `start.py` comment.

- **Pros.** Reuses the existing, correct readiness gate verbatim. The `start.py`
  comment becomes true. One code path for "make this host usable."
- **Cons.**
  - Requires every provider's `start_host` to be genuinely idempotent first. Per
    the audit, that is a large precondition: ssh raises `NotImplementedError` (must
    be special-cased or made a no-op); lima re-runs record rewrites and restarts the
    activity watcher; vps/imbue_cloud relaunch a second in-container sshd; vps
    offline rewrites the external state store. Each of these is a behavior change to
    audit and test in its own right.
  - Introduces a residual tight race that does not exist today: two `limactl start`
    invocations against the same instance can collide, and per the incident's
    reading of lima, the loser gets a nonzero "seems running" error (lima has no
    start lock). So this direction trades a readiness race for a start-collision
    race and then needs its own error-tolerance handling in lima's `start_host`.
  - Runs a lot of machinery (record writes, watcher relaunch) on the common path
    where the host is already fully up, purely to reach the readiness wait buried at
    the end.
- **Coverage.** Covers exactly the `ensure_host_started` callers. `stop`,
  `destroy`, and `gc` (which call `get_host` directly) are unaffected.

### Direction 2 — make lima's online classification honor the contract

Change lima's `_get_host_by_id` to verify SSH reachability (or a cheaper precise
readiness signal) before returning an online `Host`, so "online" means
"connectable" for lima as it effectively does for the live-checking providers.

- **Pros.** Fixes the contract at its source; every consumer of `get_host`
  (including the `get_host`-direct paths: stop, destroy, gc) benefits at once with
  no per-call-site change.
- **Cons.**
  - `get_host` is a hot read path — invoked in discovery, `mngr list`, gc, and
    every command. Adding an SSH transport handshake to it imposes latency and a
    connection attempt on paths that only want to *classify* the host, not use it
    (e.g. `mngr list` explicitly tolerates and recovers from unreachable online
    hosts via its offline fallback; making classification itself block on a
    handshake would regress list latency and could turn a mid-boot host into a
    spurious offline row).
  - A pure TCP-connect probe is cheaper but is exactly the error-shape signal the
    constraints reject; a full handshake is precise but expensive on the hot path.
  - A "verify only when the caller intends to mutate" variant needs the caller's
    intent, which `get_host` does not receive — pushing the decision back up to the
    call site, which *is* direction 3.
- **Coverage.** Broadest (all `get_host` consumers), at the cost of the hot-path
  regression the constraints warn against.

### Direction 3 — scoped readiness gate at the `ensure_host_started` seam (recommended)

Treat an already-online host returned to a start-intending caller as a *candidate*
and confirm it is connectable before handing it back, via a bounded,
provider-supplied readiness wait. Concretely:

- Add a provider method, e.g. `wait_until_connectable(online_host: Host) -> None`,
  on `ProviderInstanceInterface` with a **default no-op** implementation in the
  base class (preserving today's behavior for providers whose "online" already
  implies connectable: local, docker, modal, and the live-checking VPS family).
- lima overrides it to run `wait_for_sshd(hostname, port, timeout)` against the
  live SSH endpoint, re-deriving the endpoint exactly as `start_host` already does
  (`_get_ssh_config(instance_name)` from the host record) rather than reaching into
  connector internals, and reusing the same readiness primitive.
- In `ensure_host_started`, on the `case Host()` (online) branch, when
  `is_start_desired` is True, call `provider.wait_until_connectable(online_host)`
  before returning. When the offline branch ran `start_host` instead, skip the gate
  — `start_host` already waited.

- **Pros.**
  - Acts on precise knowledge: `wait_for_sshd` succeeds on a real transport
    handshake, not on the absence of a particular error string. It returns
    immediately on the first successful handshake, so a healthy host pays only ~one
    handshake of latency (sub-second, loopback for lima); remote providers with the
    no-op default pay nothing.
  - Adds no persistent state — it records nothing and leaves no marker or cache. It
    is a precondition check at commit time, exactly what the constraints ask for.
  - Leaves the hot `get_host` path untouched; the cost lands only on the
    start-intending callers that are about to do SSH work anyway.
  - Sidesteps direction 1's start-collision race entirely: it never calls
    `limactl start` for an already-online host.
  - Keeps the provider abstraction clean: each provider declares what "ready" means
    for it, rather than hardcoding lima specifics into shared `find.py`.
  - Naturally covers all `ensure_host_started` callers — the reported `mngr start`
    plus `exec`, `connect`, `create --reuse`, `create` target-host, `rename`,
    `message`, and the `resolve_host_location*` paths — in one change.
- **Cons / things to get right.**
  - A genuinely-wedged host that is status-`Running` but whose sshd will *never*
    come up now blocks for the readiness timeout before failing, instead of failing
    fast. The full 120s `ssh_connect_timeout` is too long for this gate; it should
    take a shorter bound sized to the boot tail (the observed window is ~25s, so a
    30-60s cap covers boot while failing a truly-dead host in reasonable time). This
    is a knob to choose deliberately (see open questions).
  - Does not cover the `get_host`-direct paths (`stop AGENT`, `destroy`,
    `gc` work-dirs). Those are addressed as separate items (below) rather than by
    this seam, since they do not express start intent.
- **Coverage.** All `ensure_host_started` callers. The reported incident is fully
  resolved.

### Direction 4 — lima-native readiness signal

Instead of an SSH handshake, gate on a lima-internal readiness signal (e.g. the
hostagent event log that `limactl start` itself tails for boot completion).

- **Pros.** Potentially the most faithful "is the guest ready" signal, and could be
  cheaper than a handshake.
- **Cons.** Couples mngr to undocumented lima internals (event-log format, file
  location, semantics across lima versions) — fragile and a maintenance liability,
  and `limactl list --json` exposes no readiness field today. The SSH handshake is
  the signal we actually care about (mngr's next action is SSH), is stable, and is
  already implemented. Not recommended, but noted for completeness.

## Recommendation

Adopt **direction 3**: a scoped, provider-supplied readiness gate at the
`ensure_host_started` seam, default no-op, lima override running a bounded
`wait_for_sshd`. It is the smallest change that acts on precise knowledge, adds no
state, leaves the hot path alone, and avoids introducing a new race — and it covers
every start-intending entry point at once.

Handle the independently-discovered issues as **separate, smaller changes**, each
with its own reasoning and test, rather than folding them into the readiness gate:

- **`gc` work-dirs:** widen the catch in `_gc_single_host_work_dir` (`gc.py:237-240`)
  from `HostOfflineError`/`HostAuthenticationError` to the base
  `HostConnectionError`, matching the machines phase's deliberate skip
  (`gc.py:405-408`). This is a one-line consistency fix; a mid-boot (or otherwise
  transiently unreachable) online host should be skipped, not abort the sweep.
- **`mngr destroy`:** decide, explicitly, whether a mid-boot `HostConnectionError`
  should escalate a per-agent destroy into a whole-host `destroy_host`
  (`destroy.py:435-445`). The safe change is to not treat a transient connection
  failure as "offline" for the purpose of whole-host teardown — either apply the
  same readiness confirmation before classifying, or fail the destroy with a
  retryable error rather than escalating. This is a behavior decision for the
  destroy owner, not a mechanical fix.
- **`mngr stop AGENT`:** lowest priority. It fails loudly with a clear connection
  error and the user (or the caller's own retry) tries again; `stop --stop-host` is
  already safe. Optionally route plain `stop` through the same readiness
  confirmation, but it is acceptable to leave as a known residual.

The **ssh provider** is explicitly out of scope for direction 3: it is unaffected
because direction 3 does not call `start_host`, and its default `wait_until_connectable`
no-op preserves current behavior. Its always-`RUNNING`, never-probed classification
is a separate latent issue.

## Acceptance criteria

For the recommended fix:

1. A `mngr start <agent>` invoked while the target lima host is mid-boot (status
   `Running`, guest sshd not yet accepting connections) **succeeds** once the boot
   completes within the readiness timeout, rather than exiting nonzero with
   "Failed to connect to host." The command's own subsequent SSH work (host lock,
   `path_exists`, `start_agents`) then proceeds normally.
2. A `mngr start` against a fully-up lima host is not measurably slower than today
   (the readiness wait returns on the first successful handshake).
3. A `mngr start` (or other start-intending command) against a lima host that is
   status-`Running` but genuinely unreachable (sshd will never come up) fails after
   the bounded readiness timeout with a clear error — it does not hang for the full
   120s `ssh_connect_timeout`, and it does not loop forever.
4. Non-lima providers see no behavior change: `wait_until_connectable` defaults to a
   no-op, so local/docker/modal/vps/imbue_cloud start flows are byte-for-byte
   unchanged. No new SSH handshake is added to `get_host` or to `mngr list` /
   discovery / gc.
5. No new persistent files, markers, or caches are introduced by the fix.
6. The `start.py:235-237` comment is corrected to describe what the code does (the
   readiness gate), resolving the `specs/uncertainties.md` entry.
7. (Separate change) `mngr gc` no longer aborts its work-dirs phase when a
   status-`Running` host is unreachable; it skips that host and continues,
   consistent with the machines phase.

## Test strategy and the testable seam

A mid-boot lima VM is hard to fabricate in a unit test — you cannot cheaply hold a
real VM in the "status Running, sshd down" state on demand. Design the fix so the
race does not require a real VM to test:

- **Seam.** `wait_until_connectable` as a provider method is the unit-test seam.
  `ensure_host_started`'s online branch calling it is testable with a fake provider
  whose `get_host` returns an online host and whose `wait_until_connectable` is a
  spy: assert it is called when `is_start_desired=True` on an online host, and *not*
  called on the offline (`start_host`) branch (which already waited) nor when
  `is_start_desired=False`. This exercises the routing logic with no lima at all.
  Use the existing shared fixtures (`temp_mngr_ctx`, `local_provider`, the mock
  provider in `providers/mock_provider_test.py`) rather than new scaffolding.
- **lima override, in isolation.** Test lima's `wait_until_connectable` against a
  controllable TCP endpoint: point the host's connector at a `127.0.0.1:<port>`
  that is initially closed, then opened by the test after a delay, and assert the
  method blocks until the port answers a handshake and then returns — and that it
  raises (rather than hanging) when the port never opens within the bound. This
  reuses the `wait_for_sshd` primitive already covered by `ssh_utils` tests; the
  new coverage is that lima wires the live endpoint into it. A local paramiko
  transport (as `wait_for_sshd` itself uses) or a lightweight socket server is
  enough; no lima binary required.
- **`ensure_host_started` unit tests** (`api/find_test.py` / equivalent): cover the
  three branches — online + start-desired (gate runs), offline + start-desired
  (`start_host` runs, gate skipped), online + not-start-desired (gate skipped).
- **Timeout behavior.** A unit test that the gate gives up after the bounded
  readiness timeout (a never-opening port) and surfaces a clear error, protecting
  acceptance criterion 3.
- **Acceptance/release tier (real lima).** An acceptance test is the honest place to
  prove the end-to-end race, but fabricating the exact mid-boot window is timing-
  dependent and flaky. Prefer a release-tier test that (a) creates a lima host,
  (b) stops it, (c) starts a `start_host`/`create`-driven boot in one thread while
  a second thread runs `mngr start` against the same host, and asserts the second
  command succeeds rather than failing with a connection error. Mark it
  appropriately (`@pytest.mark.release`) and expect it to need a generous timeout;
  if it proves irreducibly flaky, the unit-level seam tests above are the
  load-bearing coverage and the release test is a belt-and-suspenders smoke check.
- **Regression guards for the separate fixes.** For `gc` work-dirs, a unit test with
  a fake online host whose first work-dir probe raises the base
  `HostConnectionError`, asserting the sweep skips that host and continues (mirrors
  the existing machines-phase skip test in `gc_test.py`). For `destroy`, a unit test
  pinning the chosen behavior (no silent whole-host escalation on a transient
  connection error).

## Open questions

- **Readiness-gate timeout value.** What bound sizes the boot tail without hanging
  on a genuinely-dead host? The observed window is ~25s; a 30-60s cap is a
  reasonable starting point. Should it be a dedicated config knob or reuse an
  existing one? (It should *not* be the full 120s `ssh_connect_timeout`.)
- **`mngr stop AGENT` and `destroy`.** Do we want them routed through the same
  readiness confirmation, or is loud-failure-plus-retry acceptable for `stop` and an
  explicit non-escalation the right call for `destroy`? This document recommends
  treating them separately; the owners of those commands should confirm.
- **Broader contract.** Should `specs/provider-shape.md` / the `get_host` docstring
  be amended to state the "online implies connectable, or the provider supplies a
  readiness wait" contract explicitly, so future providers implement
  `wait_until_connectable` deliberately rather than inheriting a silent no-op?
