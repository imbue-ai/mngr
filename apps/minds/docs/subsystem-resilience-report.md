# Minds subsystem resilience report

A consolidated, decision-oriented report on the minds app's background processes
and error-recovery mechanisms. It brings together three prior pieces of audit
work and adds two lenses the earlier docs lacked:

- **Simplicity violations** -- concrete, file-and-line-cited places where the
  recovery/lifecycle code is more complex, more duplicated, or more dead than the
  problem warrants (traced to callers, not pattern-matched).
- **User-perspective scope** -- for each subsystem, the situation a user would
  have to be in for an *unrecovered* failure to be noticed at all, the noticed
  effect, and whether the recovery mechanism over-reacts or under-reacts relative
  to that.

It supersedes nothing; it sits on top of:

- `minds_background_process_audit.md` (repo root) -- the inventory of every
  persistent process/thread, organized as three concentric rings.
- `apps/minds/docs/error-recovery-audit.md` -- the per-mechanism recovery audit
  (what fails, how detected, what recovery does, what if recovery fails).
- The workspace-recovery and latchkey/auth/backup code traces summarized here.

This is a read-only analysis against branch `gabriel/recovery-audit`. No
production behavior was changed in producing it.

---

## 1. The governing principle

The two recovery mechanisms that caused their own problems in production are the
Rosetta stone for everything else:

| Incident | What actually went wrong | The rule it broke |
|---|---|---|
| Surgical restarts firing too often (pre-`UNRESOLVED`-filter) | detection counted routing-not-yet-caught-up as a wedged backend | **detection width** exceeded the real failure |
| Discovery watchdog moving the whole app to an error screen | a producer stall (narrow, mode-dependent) triggered the widest possible recovery | **recovery scope** exceeded the real blast radius |

Both are the same meta-bug seen from two sides. The unifying principle:

> **A recovery's scope and aggressiveness must never exceed the failure's actual,
> mode-dependent blast radius -- and "blast radius" depends on what the user is
> currently doing, not on which process happens to have failed.**

A producer stall has, for the median single-workspace user already loaded into a
workspace, a blast radius of approximately zero (the proxy still routes from
cached topology). Recovering it with an app-wide error screen, or with a
host-wide re-provision, is the over-reaction. The fix in both historical cases
was to *narrow* -- narrow detection, narrow scope -- not to add machinery.

### Three recovery tiers, and the rule that nothing jumps a tier

- **Tier 0 -- silent self-heal, owned locally.** Cheap idempotent reconnect/respawn
  loops, owned by the process that owns the failing thing. The proxy's 503
  auto-refresh, SSH-tunnel reconnect, per-agent `mngr event` respawn, the
  permission-stream reconnect. This is the correct default for almost everything.
- **Tier 1 -- scoped passive signal.** A non-blocking indicator at the scope of the
  affected thing that never blocks unaffected activity: stale liveness dots, the
  per-workspace recovery page, a (missing today) "permissions reconnecting" chip.
- **Tier 2 -- whole-app takeover.** Reserved, with a short explicit allowlist, for
  genuinely whole-app-fatal conditions: backend dead, proxy/consumer dead. This is
  where *more* vigilance is warranted, and it is exactly where the real
  silent-failure holes are (backend code-0/signal exit, renderer crash).

The recurring design move this report recommends is **demote and localize**, not
centralize: push liveness ownership down to the process that owns the child, demote
mode-dependent failures to passive signals, and reserve the app-wide hammer for
app-wide death.

---

## 2. Subsystem-by-subsystem

Each subsystem below has three parts: **Error logic** (condensed; the
error-recovery-audit has the long form), **Simplicity violations** (traced, with a
confidence marker), and **User scope** (who notices, what effect, over/under-react).

Confidence markers: **[verified]** = traced to callers/definitions; **[judgment]**
= a defensible-but-debatable complexity call; **[inferred]** = reasoned from
adjacent code, not directly confirmed.

---

### 2.1 Electron backend supervision (Ring 0)

**Error logic.** The Python backend is the only OS process Electron owns. Start
failures (env sync / spawn / port-readiness) and post-start crashes surface as
full-window takeovers with a Retry that re-runs the shutdown->start cycle. The SSE
consumer reconnects forever with 1500ms backoff.

**Simplicity violations.** None notable -- the shell is defensively engineered
(timeouts everywhere, shutdown always resolves, last-window-never-closes). The
issues here are *gaps*, not excess complexity, so they live in the scope column.

**User scope.** This is the one subsystem whose blast radius is genuinely
whole-app, so Tier-2 takeovers are proportionate. The holes are all
under-reactions:
- A **clean-but-unexpected exit (code 0)** or a **signal kill (code null, OOM/SIGKILL)**
  is deliberately ignored -- the app keeps showing dead content with no takeover.
- **No `render-process-gone`/`unresponsive`/`did-fail-load` handlers exist** -- a
  renderer crash is a blank/frozen view with no detection, recovery, or report.
  This is the single largest silent-failure surface in the product.
- Backend Retry has **no attempt cap or backoff** -- a persistently failing backend
  loops forever.

These are the places to add vigilance, consistent with the principle: maximal
blast radius justifies maximal detection.

---

### 2.2 Discovery & forwarding (the spine)

**Error logic.** A single `mngr observe` *producer* (owned by the detached
`mngr latchkey forward` supervisor) writes a discovery-events file; a single
`mngr forward` *consumer* (also the HTTP/WS proxy) tails it. Producer stall ->
`RECONNECTING` (passive, never escalates). Consumer death -> terminal `BLOCKED`
app takeover. Per-request proxy errors map to 502/503/504 with a 1s meta-refresh
loader.

**Simplicity violations.**

1. **The producer has no in-process respawn; the minds watchdog is its sole
   resurrection path. [verified]** `DiscoveryStreamConsumer.start`
   (`discovery_stream.py:158`) spawns `mngr observe` with
   `is_checked_by_group=False` (`:163`), and nothing inside the forward process
   watches `_process` for an unexpected exit (no liveness check, no exit callback;
   the consumer-side `add_on_unexpected_exit_callback` exists only in minds at
   `forward_cli.py:185`). So if the observe child dies while the forward parent
   stays alive, only the minds-side `DiscoveryHealthWatchdog` -- inferring a stall
   from downstream freshness (~35s) and calling `supervisor.bounce()`/`restart()` --
   ever brings it back. **Classification: layering smell.** Minds (Ring 1) is
   babysitting a Ring-1B grandchild because Ring 1B does not babysit its own child.
   A standalone (non-minds) `mngr latchkey forward` gets no producer recovery at all.

2. **`restart()` is a host-wide hammer used as a producer-stall remedy. [judgment]**
   `LatchkeyForwardSupervisor.restart()` (`forward_supervisor.py:524`) is
   `stop()` + `ensure_running()`, and a fresh forward re-provisions *every* managed
   VPS host (`discovery.py:436`; `_provisioned_hosts` cleared on restart). The
   watchdog itself documents this (`discovery_health.py:32-33`). For the common
   failure (a lone observe child dying under a healthy parent), the cheap
   `bounce()` (SIGHUP, gateway/tunnels/provisioning untouched) already suffices.
   The heaviness of `restart` is a direct consequence of violation #1: if the
   forward respawned its own observe child, the watchdog would rarely need bounce
   and `restart` could stay a true last resort.

3. **`mark_stuck` test-only path is *not* here** -- see 2.3; noting only that the
   discovery watchdog's `now_fn` clock injection (`discovery_health.py:184`) is the
   pattern the workspace tracker is *missing* (and the reason it grew a test hook).

4. **Minor supervisor nits. [verified/judgment]** A stale docstring claims
   `_terminate_pid` "mirrors `core._terminate_pid`" but no such function exists in
   `core.py` (`forward_supervisor.py:239` -- a false cross-reference that would
   mislead a future maintainer). `_terminate_pid`/`_terminate_process` are
   near-duplicate helpers that one parametrized function would cover. The
   `ensure_running` adopt-vs-spawn branch is largely dead in the minds path (minds
   calls `restart()` -- stop-then-spawn -- on every startup, `run.py:795`), so the
   adoption generality serves no current caller.

**Recommended direction.** Add a passive exit-callback on the latchkey observe
child (mirror `forward_cli.py:185`) so the forward bounces its *own* observe; that
moves producer liveness to Tier 0 where it belongs, demotes the minds watchdog's
`restart()` to a genuine last resort, and lets the watchdog's producer half shrink
dramatically. Before acting, confirm (a) where in the forward process main loop
(`cli.py:477-597`, currently just `shutdown_event.wait()`) such a monitor would
live, and (b) that the freshness-gated STUCK redirect (2.3) keeps working when
producer liveness is solid.

**User scope.** A **single-workspace user already loaded in does not notice a
producer stall at all** -- cached last-good topology + the established reverse
tunnel keep their traffic flowing; `RECONNECTING` surfaces only a passive
"time since last discovery" counter. It matters at the **home screen** (frozen
liveness dots / workspace list), when **switching to a not-yet-cached workspace**,
and when **creating** (new agent needs a fresh discovery event to get wired). The
only forced-notice failure is **consumer death -> `BLOCKED`**, which is genuinely
whole-app and correctly Tier-2. Net: the current split (producer = passive,
consumer = takeover) already embodies the governing principle; the remaining work
is removing the now-unjustified *complexity* behind the producer half, not changing
its UX.

---

### 2.3 Workspace recovery (per-workspace STUCK -> recovery page -> restart)

**Error logic.** The plugin emits a `system_interface_backend_failure` envelope but
classifies nothing. Minds enrolls only connection-level / infra-5xx failures as
*suspects* (ignores app 4xx/5xx and `UNRESOLVED`); a background 2s probe loop is
the sole authority on STUCK (an unbroken 5s run of probe failures). STUCK navigates
that workspace's view to a recovery page that runs an in-container diagnostics
probe, classifies one of five dispatch tiers, and offers a surgical or host
restart. Every restart-failure path converges on a visible `RESTART_FAILED` with a
reason. This is the model-citizen flow.

**Simplicity violations.**

1. **A test-only force-method drags dead production code into existence.
   [verified] (clean win).** `SystemInterfaceHealthTracker.mark_stuck()`
   (`system_interface_health.py:299`) has no production caller (grep: only tests +
   one docstring). In production STUCK is reached only via `record_probe_failure`,
   which always sets `failure_run_started_wall_at` alongside the run start
   (`:250-252`); so an in-production STUCK agent *always* has an onset timestamp.
   That single test-only possibility forces a whole chain to exist purely to keep
   the test path from being stranded:
   - the `if onset is None: return _is_discovery_fresh(...)` fallback in the
     freshness gate (`app.py:199-200`) -- unreachable in production;
   - `_is_discovery_fresh()` (`app.py:1496`) -- only caller is that dead branch;
   - `_DISCOVERY_FRESHNESS_THRESHOLD_SECONDS` (`app.py:1395`) -- used only by it.

   **Fix:** make the tracker's clock injectable (add `now_fn`, exactly as
   `DiscoveryHealthWatchdog` already has at `discovery_health.py:184`), drive STUCK
   through the real probe path in tests, then delete `mark_stuck`, the dead branch,
   the helper, and the constant. The gate collapses to one line:
   `return last_full_snapshot_at is not None and last_full_snapshot_at >= onset`.

2. **The STUCK->recovery redirect is emitted from four sites sharing one latch.
   [verified structure / judgment on fix].** One logical decision ("show the
   recovery page for agent X, once, when classification is trustworthy") is spread
   across: connect-time replay (`app.py:979-988`), the per-event flip check
   (`:1045-1055`), promote-suppressed-on-wake (`:1063-1072`), and the periodic
   15s re-assert (`:1082-1106`) -- all calling `_should_emit_system_interface_status`
   and all poking the `redirected_agent_ids` latch. Site 3 is genuinely needed
   (the freshness gate suppresses STUCK until a post-onset snapshot lands, and this
   bounds promote latency to one poll). But the periodic re-assert's stated reason
   ("a reloaded webview lost the one-shot event") **overlaps the connect-time
   replay** (a reloaded webview reconnects, replaying the full non-HEALTHY set).
   Candidate consolidation: a single per-wake *reconcile* (compute desired redirect
   set, diff against the latch) subsuming sites 2/3/4. Verify the SSE delivery
   guarantee before removing the periodic re-emit.

3. **The restart worker dual-writes two parallel status surfaces. [verified].**
   `run_restart_sequence` writes every transition twice -- `tracker.mark_restart_failed`
   + `registry.fail`, `tracker.record_probe_success` + `registry.complete`
   (`workspace_recovery.py:238-270`). Both are consumed (tracker -> chrome SSE;
   registry -> the v1 `/workspaces/operations/restart/<id>` polling API,
   `api_v1.py:739`), so this is a *migration-in-progress* coupling, not dead code.
   The tracker's `RESTARTING`/`RESTART_FAILED`/`last_restart_error` half duplicates
   the registry. End state: chrome derives restart status from the registry too,
   leaving the tracker with only its unique job (probe-confirmed STUCK detection).
   Larger move -- flag to finish the migration, not a quick fix.

4. **Minor: `_run_mngr_capturing`'s distinguishing behavior has no consumer.
   [verified].** Its non-raising / stdout-preserving variant is only called by
   `_run_mngr` (`workspace_recovery.py:99`), which discards stdout on nonzero. Could
   inline; low priority (mirrors a pair in `mngr_command.py`).

**Don't churn:** the suspect->probe->STUCK machine and the 7-probe diagnostics
classifier are genuinely clean (only 3 of 7 probes drive the dispatch tier; the
rest are diagnostic display, and the in-container script duplication is unavoidable
because the script can't import the module).

**User scope.** Correctly scoped: failure and recovery are both per-workspace, and
every failure path is visible (`RESTART_FAILED` + reason). The historical
over-firing was a *detection-width* bug (treating `UNRESOLVED` as wedged), already
fixed by narrowing -- the recovery action was always proportionate.

---

### 2.4 Latchkey: permissions & gateway client

**Error logic.** Four independent loops: gateway port-discovery (0.2s poll, 30s
cap, bail if supervisor dead); stale-port self-heal (connect-error ->
`invalidate_initialization` -> rebind on next call); the permission-requests
follow-stream (reconnect on 1s->30s backoff, per-record errors skipped, idempotent
on `request_id`); and resolution side-effects (durable response event + a
fire-and-forget `mngr message` nudge).

**Simplicity violations.**

1. **The correct delivery-verifying nudge exists but is unwired; production uses the
   weaker exit-code path; the verifier is now orphaned dead code. [verified].**
   `MngrMessageSender.deliver()` and `stdout_reports_message_delivered()`
   (`messaging.py:118`, `:26`) judge nudge success by the `message_sent` JSONL event
   -- the *correct* signal, because `mngr message` exits 0 even when no agent
   matched the target (documented in the file's own docstrings). But grep shows
   **zero production callers**: `deliver`'s only caller, `onboarding.py:710`, was
   deleted whole-file in `6eea87bde`. Every production handler nudge instead calls
   `send()` -> `try_send()`, which judges success **by exit code only**
   (`predefined.py:742`, `file_sharing.py:380`, `accounts.py:217`,
   `workspace.py:308`). **Classification: dead code + the exit-0-on-no-match
   weakness.** Either rewire production to `deliver`, or delete the dead
   verifier+tests; the current state keeps the cost of both and the benefit of
   neither.

2. **`MngrMessageSender` has three entry points; only `send` is used in prod.
   [verified].** `send` (thread), `try_send` (blocking, exit-code), `deliver`
   (blocking, JSONL). Production uses only `send`; `try_send` is reachable only via
   `send`'s thread target; `deliver` is dead (#1). With #1 resolved the surface
   collapses to one method.

3. **Gateway error subclasses are never distinguished from the base. [verified].**
   `LatchkeyGatewayInitializationError` / `LatchkeyGatewayClientNotInitializedError`
   (`gateway_client.py:86`, `:90`) -- no `except`/`raises` site anywhere branches on
   them; every catch uses the base `LatchkeyGatewayClientError`. Premature
   abstraction (message-only distinction). Mild.

4. **`LatchkeyAutoRegister`'s lock guards a reader that doesn't exist. [judgment].**
   `_processed_pairs` + `_lock` (`latchkey_auto_register.py:54-55`) are justified by
   a comment about tests/FastAPI inspecting state, but grep shows `_processed_pairs`
   is touched only inside the module, from the single envelope-consumer thread; the
   class docstring even says the dedup is "purely an optimization." Low stakes.

**User scope.** Entirely **demand-driven**: invisible until an agent actually
requests permission. The nudge weakness (#1): the grant lands (gateway write +
durable event succeed) but the *wakeup* is silently lost, so the agent stays parked
until it next self-polls -- the user sees "granted" but the agent doesn't react.
**Under-reacts (silent).** The heavier failure -- the follow-stream *thread* dying
(`is_checked=False`, no watchdog) -- means **all** permission requests for **all**
agents silently never reach the inbox until restart; invisible until a prompt fails
to appear. The right surface is Tier-1 and demand-driven: a "permissions
reconnecting/offline" chip shown *only* when there is a pending-but-undeliverable
request -- not an always-on watchdog.

---

### 2.5 Latchkey: supervisor & producer lifecycle

Covered under 2.2 (producer respawn gap, `restart` hammer, supervisor nits). Two
additional structural notes:

- **Duplicate-forward reaping is substantial machinery for a self-inflicted problem.
  [judgment].** Seven module-level helpers (`forward_supervisor.py:55-179`) +
  `_reap_duplicate_forwards` (`:415`) scan the process table, cmdline-match, scope by
  resolved latchkey directory, and kill duplicates + descendants. It is real
  defensive code against a real past incident (duplicate forwards -> stuck-new-mind
  503s, PR #2285), and the reaper is load-bearing. But the adopt-vs-reap generality
  in `ensure_running` exceeds what the sole real caller (minds, always
  stop-then-spawn) needs.
- **The remote-state-sync watchdog uses three threads (fs observer + stop-on-shutdown
  + fail-loudly sentinel). [judgment].** `discovery.py:419-491`. Each has a distinct
  blocking wait and the checked/unchecked split is meaningful, but three threads to
  supervise one watchdog observer is heavy; plausibly collapsible to one strand that
  `join()`s then checks `shutdown_event`. Borderline, not a clear violation.

---

### 2.6 Auth & sessions

**Error logic.** Four distinct auth representations -- desktop session cookie
(30-day, no refresh), per-startup in-memory `MINDS_API_KEY`, the plugin-owned
SuperTokens account session (minds mirrors only identity + associations), and the
`mngr_forward` subdomain cookie. These are *layered* (cookie gates UI, bearer gates
agents, account renders inside the UI), not redundant -- so not a violation, but a
real source of conceptual load. Fail-closed on corrupt code/key files; account
backend-unreachable -> visible 502; OAuth crash -> `state=error` (not an infinite
"Waiting..."); sign-out fails open toward user intent.

**Simplicity violations.**

1. **`auth_required` SSE event has no `chrome.js` handler. [verified] (recovery
   hole).** `/_chrome/events` emits `{"type":"auth_required"}` then closes the
   stream on expiry (`app.py:896`), but `chrome.js` dispatches only on
   `workspaces`/`auth_status`/`requests`/`system_interface_status`/`workspace_accent_preview`
   -- no `auth_required` branch (grep confirms it appears only in `electron/*.js`).
   In Electron the event only clears titlebar accents (cosmetic); the path that
   navigates to `/auth/login` (`main.js:2838`) is wired to a *different* producer
   (the SuperTokens share flow), not session-cookie expiry. **Classification:
   emit-without-consumer / layering.** Net: an expired session cookie leaves the
   sidebar silently empty with no "sign in again" prompt; the user recovers only by
   hitting a route that 302s to `/login`.

2. **`is_any_signed_in` + `has_signed_in_before` are dead in production.
   [verified].** Full-repo grep: `has_signed_in_before` has zero non-test callers;
   `is_any_signed_in` is called only by it and by tests. A closed cluster reachable
   only from `session_store_test.py`.

3. **`_AuthBackendShim` is admitted legacy scaffolding with hollow methods.
   [verified].** `supertokens_routes.py:77-161` -- its own docstring says it exists
   to avoid rewriting handlers. `is_email_verified` always returns True,
   `get_user_provider` always `"email"`, `base_url` returns `""` -- yet
   `_handle_reset_password_redirect` (`:708`) still builds a redirect from that empty
   base_url (a redirect to `/auth/reset-password` with no host). Collapse the shim
   or delete the now-inert verify/reset routes.

**Not violations (checked):** the double-checked signing-key lock is justified (a
real startup-burst race; `atomic_write` prevents torn reads). The identity cache's
copy-on-read + refresh-gated GC is intricate but matched to a real blast radius
(orphaned associations -> permanent "no associated account"); a reasonable future
simplification target, not a defect.

**User scope.** Expiry on the chrome surface (#1) **under-reacts**: silent empty
sidebar, no prompt, recover-only-by-accident. The account layer recovers well
(visible 502, OAuth `state=error`, share-flow login nav). Sign-out deliberately
over-honors intent (drops the local mirror even if connector revoke fails -> a
connector session can linger -- a mild hygiene wrinkle, not data loss).

---

### 2.7 Backup, restore & snapshot

**Error logic.** Provisioning runs detached after create, idempotent, retried in a
300s/10s budget with an inner 60s/3s retry for fresh-credential propagation;
failure -> one transient toast + a log line, workspace runs on without backups.
Status reads never raise into the route (degrade to `UNKNOWN`, wall-clock bounded).
Export/restore is synchronous so failures surface in the HTTP response. Canonical
envs are written before injection and never auto-deleted (a deliberate restore
affordance).

**Simplicity violations.**

1. **The rich `BackupStatusState` taxonomy is dead in production. [verified].**
   `BackupStatusState` (5-state enum), `compute_backup_status_for_workspace`, and
   the parallel `compute_backup_status_for_workspaces` batch computer
   (`backup_status.py:42-55, 99-248`) appear only in their own module + the test
   file -- zero production callers (the batch `/api/backup-status` route was removed,
   per `api_v1.py:332-334`). The live badge is derived in `api_v1.py:326-340` from
   `list_workspace_snapshots` + `is_workspace_backing_up`. **~100 lines of
   dead-taxonomy / test-only-code in production.** The frontend can only distinguish
   has-snapshots / none / backing-up; the 5-way distinction is computed nowhere live.

2. **Provisioning failure has no persistent UI signal. [verified].** The only
   surfacing is a `logger.warning` + one transient OS notification
   (`agent_creator.py:1860-1876`); grep finds no persisted failure flag. The absence
   of a canonical env is the only durable trace, and the route returns the same 404
   for "never configured" and "provisioning failed." Under-reaction. (The persistent
   red-badge fix lives only on the separate `gabriel/backup-failure` branch.)

3. **Parallel secure-write logic across three stores. [judgment].**
   `write_canonical_env` (`backup_env_store.py:60`), `save_backup_password_if_absent`
   (`backup_password_store.py:64`), and `_add_password_key_once`
   (`restic_cli.py:239`) each hand-roll a 0600 `os.open` + write (+ rename) dance,
   while `mngr.utils.file_utils.atomic_write` already does temp+fsync+replace at
   0600. Only a *partial* dedup: the password store's `O_EXCL` write-once and
   restic's ephemeral-secret semantics differ from a plain atomic overwrite, but
   `write_canonical_env` could fold in cleanly. Adjacent sharp edge: it uses a fixed
   `path.with_suffix(".tmp")` rather than a unique temp name (a collision risk under
   concurrent writers; low in practice, single-threaded per agent).

**Justified (checked):** the nested retry budgets target genuinely different
failure modes (edge-credential propagation vs host-reachability on inject); the
inner transient-auth detection is brittle (stderr substring match) but its misses
are caught by the coarser outer loop, so its value is latency, not correctness.

**User scope.** A provisioning failure is noticed only *if the user catches the one
toast*. Miss it and the workspace runs indefinitely with **no backups, silently** --
the badge is indistinguishable from a never-yet-backed-up workspace. **Under-reacts
on visibility, correctly conservative on data** (env written before inject, never
auto-deleted, restore works for stopped/destroyed workspaces). A misconfigured env
(repo present, creds wrong) also masks as a benign "no backups" empty state.

---

## 3. Consolidated simplicity-violations table

Ordered roughly by clarity-of-win. Effort is a rough estimate.

| # | Subsystem | Violation | Type | Confidence | Effort | Suggested action |
|---|---|---|---|---|---|---|
| 1 | Workspace recovery | `mark_stuck` test hook -> dead branch + `_is_discovery_fresh` + constant | test-only-in-prod | verified | S | Inject `now_fn`; delete the hook + dead chain. **Do first.** |
| 2 | Backup | `BackupStatusState` taxonomy + computers dead (~100 lines) | dead richness | verified | S | Delete enum + computers + their tests, or wire the badge to them. |
| 3 | Auth | `auth_required` SSE has no `chrome.js` handler | emit-without-consumer | verified | S | Add a chrome.js handler that prompts re-sign-in (Tier-1). |
| 4 | Latchkey perms | correct `deliver()` nudge orphaned; prod uses exit-code `try_send` | dead code + silent-failure | verified | S | Rewire prod to `deliver`, or delete the verifier; collapse `MngrMessageSender` to one method. |
| 5 | Auth | `is_any_signed_in` / `has_signed_in_before` dead | dead code | verified | S | Delete (or document as intentional API). |
| 6 | Discovery/forward | producer has no in-process respawn; minds watchdog is sole resurrector | layering smell | verified | M | Add observe-child exit-callback in the forward; demote watchdog `restart` to last resort. |
| 7 | Workspace recovery | restart worker dual-writes tracker + registry | duplicated mechanisms | verified | M | Finish the v1 migration: chrome reads restart status from the registry. |
| 8 | Workspace recovery | STUCK redirect emitted from 4 sites + latch | emit-site sprawl | verified/judgment | M | Consolidate to one per-wake reconcile (verify SSE delivery first). |
| 9 | Auth | `_AuthBackendShim` hollow legacy methods; empty `base_url` redirect | premature/legacy scaffolding | verified | M | Collapse the shim; delete inert verify/reset routes. |
| 10 | Discovery/forward | `restart()` re-provisions every host as a producer-stall remedy | over-broad recovery | judgment | (folds into #6) | Resolve via #6. |
| 11 | Backup | three hand-rolled secure-writes vs shared `atomic_write` | duplicated mechanisms | judgment | S | Fold `write_canonical_env` into `atomic_write`; fix the `.tmp` collision. |
| 12 | Latchkey perms | gateway error subclasses never distinguished | premature abstraction | verified | S | Collapse to the base error, or branch on them. |
| 13 | Discovery/forward | stale `core._terminate_pid` docstring; `_terminate_*` near-dup; dead adopt path | dead reference / nits | verified/judgment | S | Fix the docstring; consider merging the helpers. |
| 14 | Latchkey perms | `LatchkeyAutoRegister` lock guards a non-existent reader | over-guard | judgment | S | Simplify if touched; low stakes. |
| 15 | Workspace recovery | `_run_mngr_capturing` distinguishing behavior unused | premature abstraction | verified | S | Inline if touched; low priority. |

---

## 4. The silent-failure surfaces (user-impact order)

Pulled together from all subsystems, this is where an *unrecovered* failure hurts a
user with no signal -- the inverse of the over-reactions. Roughly ordered by impact:

1. **Renderer-process crash** (2.1) -- blank/frozen view, no detection at all.
2. **Permission gateway/stream permanently down** (2.4) -- prompts silently never
   appear, agent stays blocked; only logs.
3. **Approval nudge silently lost** (2.4 #1) -- grant applied, agent never woken.
4. **Backend exits code-0 or via signal/OOM** (2.1) -- app goes dead in place.
5. **Expired session cookie on the chrome surface** (2.6 #1) -- empty sidebar, no prompt.
6. **Backup provisioning failure** (2.7 #2) -- transient toast only; runs with no backups.
7. **Persistent producer stall while consumer is alive** (2.2) -- passive counter only
   (acceptable for single-workspace users; matters at home/switch/create).

Note the pattern: the over-reactions (the two historical incidents) and the
under-reactions (this list) are the *same* failure of proportionality. The fix for
both is the tier discipline in section 1 -- match the signal's scope and weight to
the failure's mode-dependent blast radius. Several of these (2, 3, 5) are also
*demand-driven*: invisible until the user does a specific thing, which argues for a
lazy Tier-1 indicator surfaced at the point of impact rather than an always-on
watchdog.

---

## 5. Recommended consolidation, in order

1. **Land the clean wins (table #1-#5).** All verified, all small, all pure
   reductions: dead code out, one missing handler in. These alone meaningfully
   shrink the "very specific code that could be brittle" surface.
2. **Push producer liveness into the forward (table #6/#10).** The single highest-
   leverage structural change: it removes the cross-ring babysitting, demotes the
   host-wide `restart` hammer, and lets the minds discovery watchdog's producer half
   shrink. Verify the two preconditions in 2.2 first.
3. **Finish in-flight migrations (table #7, #9).** Don't maintain two parallel
   status surfaces or a hollow shim indefinitely -- each is a standing dual-write
   tax. Pick the new surface and retire the old.
4. **Consolidate the redirect emit-sites (table #8)** once the SSE delivery
   guarantee is confirmed.
5. **Adopt the tier discipline as a review rule.** For any new background process or
   recovery mechanism, state its blast radius per user-mode and pick the lowest tier
   that covers it. Tier-2 (whole-app takeover) requires an explicit entry on a short
   allowlist. This is the durable fix that prevents the next over-reaction.

---

## 6. Open questions to resolve before acting

- **(#6)** Where in the `mngr latchkey forward` main loop (`cli.py:477-597`) should
  the observe-child liveness monitor live, and does adding it interact with the
  SIGHUP bounce watcher?
- **(#6 / 2.2)** Confirm a single-workspace user's cached route genuinely survives a
  long producer stall independent of freshness (the resolver's last-good-topology
  fallback) -- this is the load-bearing justification for demoting producer recovery.
- **(#8)** Can an *already-connected* SSE webview drop a single event without
  reconnecting? If not, the periodic re-assert is redundant with connect-time replay
  and the reconcile consolidation is safe.
- **(#4)** Decision: is the delivery-verifying nudge worth wiring into production
  (closing the silent-no-match gap), or should the dead verifier simply be deleted?
- **(#2)** Decision: delete the dead backup taxonomy, or is it the intended home for
  a future richer badge (in which case wire it and add a production caller)?
