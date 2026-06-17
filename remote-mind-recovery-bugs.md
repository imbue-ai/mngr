# Remote (imbue_cloud) mind recovery failure: bug breakdown

Two users on different machines lost access to healthy leased `imbue_cloud` minds.
A transient connectivity blip funneled each into the workspace-recovery page; the
recovery action then stopped the container and could not bring it back, leaving
the mind dead (data survived on the `/mngr-vol` volume). Both incidents are the
same failure.

There are **two real bugs** here — **A** (minds funnels remote minds into recovery
on a blip) and **C** (imbue_cloud can't restart a stopped container). The other
two items people noticed — the missing restart guard (**B**) and the host-key
mismatch (**D**) — are not independent bugs: **B is a policy decision / stop-gap**
and **D is an implementation detail of fixing C**. Line references are confirmed
against the current tree.

---

## How it chains together

```
transient blip
   │
   ▼
[Bug A]  remote mind trips STUCK on ordinary network jitter → recovery page
   │
   ▼
(decision B)  recovery page offers/dispatches a host restart for the remote mind
              → a `mngr stop --stop-host` stops the (healthy) container
   │
   ▼
[Bug C]  imbue_cloud `start_host` can't re-bootstrap the stopped container
         → `mngr start` step fails → mind is dead and UI-unrecoverable
   │
   └── (detail D)  the container host key no longer matches what mngr recorded,
       so SSH would fail even if sshd were up — one more reason C's start fails
```

A and C are the load-bearing faults: A is why the user is in front of the button
at all, and C is why anything that stops the container becomes permanent.

---

## Bug A (real) — minds: recovery entry has no local vs. remote distinction

**What it is.** The background health probe treats remote minds exactly like
local ones. It does a short HTTP probe through the plugin and flips a workspace
to STUCK after a few seconds of continuous failures, with no allowance for the
fact that a remote host's reachability is inherently jittery.

- `apps/minds/imbue/minds/desktop_client/app.py` — background probe loop
  (`_run_system_interface_health_probe_loop`), `_HEALTH_PROBE_INTERVAL_SECONDS`
  ~2s.
- `apps/minds/imbue/minds/desktop_client/system_interface_health.py:57` —
  `_DEFAULT_STUCK_THRESHOLD_SECONDS = 5.0`: ~5s of unbroken probe failures →
  STUCK → the chrome navigates the user to the recovery page.

**How it contributed.** A loopback-reliable local docker/lima mind essentially
never trips this on a blip; a network-reached remote mind does. So a workspace
that was completely healthy got presented to the user as broken, on the recovery
page, purely because of a few seconds of network noise. Without A, the user is
never funneled toward the destructive button — and this is true *regardless* of
whether restart-on-remote is ever fixed: a brief blip should self-resolve
silently, not throw up a "your workspace is broken" page.

**Fix direction.** Stricter STUCK criteria for remote providers (longer sustained
failure window / more consecutive failures before escalating), and ideally treat
a transient-unreachable remote mind as "wait and re-probe" rather than escalating
to a recovery prompt at all.

---

## Bug C (real, root cause) — mngr_imbue_cloud: `start_host` does not re-bootstrap the container

**What it is.** `ImbueCloudProvider.start_host`
(`libs/mngr_imbue_cloud/.../providers/instance.py:1515`) is a bare `docker start`
plus `_build_host_object`. It does **not** restart the in-container sshd, re-seed
`/root/.ssh/authorized_keys`, re-run container SSH setup, or reconcile the host
key. The in-container sshd is launched via `docker exec` (not the container's
entrypoint, whose CMD is just a sleep), so it does **not survive** a stop/start.

Contrast the **local docker provider**, whose `start_host`
(`libs/mngr/imbue/mngr/providers/docker/instance.py:1298`) re-runs
`_setup_container_ssh_and_create_host` on a native restart (call at
`instance.py:1355`). Local minds therefore self-heal across a stop/start; leased
remote minds cannot.

**How it contributed.** Once the container was stopped, the `mngr start` step
needs to SSH into the container to start the agent. The container came back with
no sshd and no `authorized_keys`, so the SSH fails and the start step raises —
the "Start step of host restart failed" the users saw. This is what converts a
recoverable ~30s blip into a permanently dead mind. C is load-bearing no matter
*who* stopped the container: even an independently-stopped container cannot be
resurrected through this path.

**Important correction.** The recent fix that added `start_container_sshd` "on
`start_host`" (commits `b626fcd20`, `32fe7b624`) patched
`VpsDockerProvider.start_host` (base vps-docker). `ImbueCloudProvider` is a
separate provider with its **own** `start_host`, which still does a bare
`docker start`. **C is not fixed for imbue_cloud in the current tree** — do not
assume the vps-docker fix covered it.

**Fix direction.** `start_host` must re-establish the container SSH bootstrap on
restart (sshd, `authorized_keys`, host-key reconciliation), the way the local
docker provider already does.

---

## Decision B — should host-restart be offered for leased imbue_cloud minds at all?

This is the missing-guard observation, but it is **not an independent bug** — it's
a policy choice, and which way it cuts depends on whether we want remote restart
to be a supported operation.

- The predicate already exists — `_is_leased_imbue_cloud_workspace`
  (`app.py:3475`) — and is wired into the settings/associate/disassociate
  handlers (`app.py:3501`, `3551`, `3584`) — but **not** into any recovery/restart
  path: `_handle_host_health_probe_api` (`app.py:3314`), `_dispatch_restart`
  (`app.py:2963`), `_handle_restart_host_api` (`app.py:3037`),
  `_run_restart_sequence` (`app.py:2890`). The stop it dispatches is real:
  `supports_shutdown_hosts` is `True` (`instance.py:285`) and `stop_host`
  (`instance.py:1488`) does a genuine `docker stop`.

**The decision:**

- **If we want restart to work for remote minds** (likely intent): B is *not* a
  bug. Fix C and restart-on-remote just works. A guard here would only be a
  **temporary stop-gap** to prevent the destructive restart until C ships, and
  should be **removed once C lands** — not kept permanently.
- **If we decide host-restart should never be offered for leased imbue_cloud
  minds**: then adding the guard is the actual fix, and B becomes a real
  requirement.

Either way, this is mutually exclusive with C *as the permanent answer*: make
restart work (C), or forbid it (guard). Not both forever. A separate, narrower
question survives even after C is fixed: whether recovery should escalate a
merely transient-unreachable remote mind to a host restart at all, versus just
waiting it out — but that's the A/escalation refinement, not the blanket guard.

---

## Detail D — host-key mismatch after restart (part of fixing C)

**What it is.** Host-side forensics found that after the restart the container's
`/etc/ssh/ssh_host_*` keys had reverted to image-build-dated keys, while mngr had
recorded a different host key for that container at lease time. A
recorded-vs-served mismatch fails `StrictHostKeyChecking`, so SSH to the
container would fail **even if sshd were running** — a second reason C's start
step can't reach the container.

**Why it's a detail, not a separate bug.** The fix for C (re-bootstrap on
`start_host`) has to re-establish/reconcile the host key anyway, so D is handled
as part of C rather than independently.

**Open implementation question.** The exact persistence mechanism — container
rootfs reverting to the image on stop/start (only `/mngr-vol` persisting) vs. the
lease-time SSH setup writing to a non-persistent location — was not resolved. It
needs a controlled stop/start on a throwaway lease rather than a paid one. Worth
also tracing the bake→lease host-key capture path, but it doesn't change C's fix.

---

## Fix priority

- **C** — the root cause and must-fix. Without it, no host restart can recover a
  stopped remote mind, and any stop (from any source) is permanent.
- **A** — independent and also a must-fix: keep transient remote blips from
  funneling users into the recovery flow in the first place.
- **B** — a decision: if restart-on-remote is supported (fix C), a guard is at
  most a temporary stop-gap to remove once C lands; only a permanent fix if we
  decide to forbid remote host-restart.
- **D** — folded into C (host-key reconciliation on restart); the rootfs/key
  persistence mechanism is an open investigation, separable from the C fix.
