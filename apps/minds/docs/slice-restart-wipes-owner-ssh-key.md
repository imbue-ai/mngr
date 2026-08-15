# A slice VM restart wipes the owner's outer SSH key

Investigation of Sentry event `6551cbfbeec04cf6a4c1c3e4e84a980a` (minds 0.3.11,
production, 2026-08-12). The bug report read: *"seems like minds can't connect to
my machine right now (or maybe minds can't connect to cloud) - can't send anymore
messages through minds without getting a 'null' message"*, with agents on the
machine unable to make latchkey requests.

Line numbers below are against `origin/main` at the time of writing, and the
investigation is left as it was written -- see [Status](#status) for what has
since been fixed.

## Status

Three of the five items under [What to fix](#what-to-fix), the root cause among
them, are now closed:

- **Fixed.** `build_root_authorized_keys_block` appends if absent instead of
  truncating, so a VM carved from here on keeps every key it was given. A VM
  carved *before* that change keeps the truncating script in its stored
  `lima.yaml` until the `mngr imbue_cloud admin repair-keys` sweep patches it
  (see below).
- **Fixed.** The outer `authorized_keys` now has a standing writer, but a
  client-side one rather than the server-side reconcile cron this investigation
  originally proposed (a connector that can re-key hosts would re-establish the
  service as pinning authority, which the user-controlled-keys plan removes).
  Adoption (`libs/mngr_imbue_cloud/.../providers/adoption.py`) installs an
  in-VM systemd reconciler unit that re-asserts a root-owned desired-state
  `authorized_keys` on every boot, after cloud-init's replay -- so a restart
  can no longer orphan an adopted host's owner. For hosts already wiped (whose
  owner cannot SSH in to adopt), the operator-run
  `mngr imbue_cloud admin repair-keys` sweep patches each slice's stored
  `lima.yaml` provision block and restores the VM root's `authorized_keys`
  from the container's own copy.
- **Fixed.** `LatchkeyDiscoveryHandler` warns on `UNAUTHENTICATED` instead of
  skipping the host in silence. It still skips.
- **Open.** `recovery_probe` still lets the inner system-interface probe
  auto-dismiss a terminal host state.
- **Open.** The RSA -> Ed25519 key migration still runs against `RUNNING` hosts
  only, so it still skips the machines that most need it.

## Symptom

A leased imbue_cloud slice reports `HostState.UNAUTHENTICATED` forever. The
recovery page shows **"Can't connect to Imbue Cloud / This machine's access to the
machine host was rejected. You may need to recreate the machine or contact
support."** — then auto-dismisses a second later and drops the user back into a
workspace that is still degraded.

The user keeps full day-to-day access the whole time, which is what makes this
hard to recognise.

## Two doors, only one broken

A slice publishes two sshd ports on the box, both normally authorized with the
same per-host key:

| door | port | backing file | used by |
|---|---|---|---|
| container sshd | `pool_hosts.container_ssh_port` | `authorized_keys` **inside the container** | workspace UI, chat, terminal — everything interactive |
| VM root sshd ("outer") | `pool_hosts.ssh_port` (= container port - 1) | `/root/.ssh/authorized_keys` **on the slice VM** | `mngr exec`, host start/stop/restart, `mngr event --follow`, latchkey VPS gateway provisioning |

Only the outer door breaks. So "I've had access since <date>" does **not**
contradict an outer-key wipe — it is the expected presentation.

## Root cause

The two doors are authorized by **different writers**, and only one of them is
maintained by code in this repo.

- **Container**: mngr re-adds `per_host_public_key` on every container
  create/rebuild (`libs/mngr_imbue_cloud/.../providers/instance.py:1619`). Self-healing.
- **VM root**: the lima carve writes only the provider bake key, via a
  **truncating** `cat >` in `_build_root_authorized_keys_block`
  (`libs/mngr_lima/imbue/mngr_lima/lima_yaml.py:302`), then appends the pool
  management key (`libs/mngr_imbue_cloud/.../slices/lima_slice.py:94`). The
  leasing user's key is injected exactly once, by the connector at lease time —
  `POST /hosts/lease` (`libs/mngr_imbue_cloud/.../connector/client.py:465`) is the
  only call that ever carries `ssh_public_key`, and there is no re-authorize or
  repair endpoint.

So the outer authorization has one writer, one write, and no reconciler. Any event
that rewrites the VM's `/root/.ssh/authorized_keys` after lease time loses the
owner's key permanently.

### It fires on every start, not just some reboots

Lima's cloud-init instance-id is the unix timestamp of cidata generation. A fresh
instance-id makes cloud-init treat the VM as a brand-new instance and re-run every
per-instance module, including lima's provision scripts. Decoded from the affected
box:

```
mind-1  iid-1782491006 -> 2026-06-26T16:23:26Z   (carve)
mind-1  iid-1785795149 -> 2026-08-03T22:12:29Z   (the restart)
sibling iid-1785609758 -> 2026-08-01T18:42:38Z   (carve; only one, never restarted)
```

`limactl start` regenerates cidata, so the truncating write is deterministic on
every VM start.

## Evidence from the affected machine

Machine `host-f202830a653a4db79f1050bf7f5bb32f` ("mind-1"), agent
`agent-2efbf8a26d494e5cb210b30d61127f01`, box `147.135.97.121`
(`bare_metal_server_id 9ef5ab2e`), outer 22010 / container 22011, leased
2026-06-30.

Timeline of the wipe, all from the VM's own filesystem:

```
2026-08-03 22:12:29   lima rewrites cidata (new instance-id)
2026-08-03 22:12:33   VM boots                      <- uptime -s
2026-08-03 22:12:35   cloud-init mints iid-1785795149
2026-08-03 22:13:09   /root/.ssh/authorized_keys rewritten  <- 36s after boot
```

`mtime` **and** `ctime` are both `2026-08-03 22:13:09`. `ctime` updates on any
inode change, so the file provably held only two keys for the following ten days.

Key sets, by fingerprint:

| key | broken (mind-1) | healthy sibling (workspace-1) |
|---|---|---|
| `SHA256:uQE7SkLM…` RSA — provider bake key | yes | yes |
| `SHA256:8BiLndbl…` ED25519 `rtard@ubuntu-22-04` — pool mgmt key | yes | yes |
| owner's per-host key (RSA 4096) | **absent** | `SHA256:FUvA2M1N…` present |

The sibling is not immune — it simply had not restarted since it was leased. It
has a single `iid-*`, and its `authorized_keys` mtime (`2026-08-03 21:45:39.795`)
sits **1.7 seconds** after its `leased_at` (`2026-08-03 21:45:38.086`). That
timestamp pair is the lease-time injection caught in the act.

Corroboration from the client log (`minds.log`), across the whole retained window:

- `host_state` for mind-1: `UNKNOWN` x425 (Aug 3, VM down), `UNAUTHENTICATED` x10
  (Aug 6), x12 (Aug 12). **Never `RUNNING`.** For imbue_cloud, `RUNNING` is only
  reachable by running the outer listing script over outer SSH, so there is no
  point after Aug 3 at which outer auth succeeded.
- Per-agent events-stream failures: **2318** for the agent on the broken host
  versus **3** for the equally-primary agent on the healthy sibling.
- The owner's per-host key was still in the container's `authorized_keys`
  (mtime `2026-07-31 16:18:09`, untouched by the reboot), which is why interactive
  access never broke.

Nothing surfaced until 2026-08-06 20:55, the first time anything actually needed
the outer path (a system-interface probe triggering a host restart). That is the
first attempt, not the onset.

## Downstream failures

Each of these is a direct consequence, and each is independently worth fixing.

1. **Latchkey is silently skipped for the entire host.**
   `LatchkeyDiscoveryHandler.__call__`
   (`libs/mngr_latchkey/imbue/mngr_latchkey/discovery.py:245`) bails on
   `host_state is not HostState.RUNNING`, lumping `UNAUTHENTICATED` in with
   stopped/paused/crashed. It tears down the tunnel and returns with no warning
   (only a debug line if a tunnel existed), so the in-container gateway port is
   never bound and every agent-side latchkey call gets connection-refused. Across
   five supervisor restarts the broken host appears **zero** times in the latchkey
   log — no provisioning attempt, no unresolved-route warning. The guard's comment
   justifies itself by "`docker exec` would fail against a stopped container", but
   here the container is running and its sshd accepts our key, so the state is not
   the one the guard was written for. Note the fix is to make the skip visible, not
   to remove it: tunnelling the desktop gateway in as a substitute is actively
   harmful for a VPS-backed workspace, for the reasons already set out in
   `_warn_unresolved_gateway_route` (it half-works and so hides the problem, it
   exposes the desktop gateway to the workspace, and it squats the port the
   VPS-to-container tunnel needs).

2. **The per-agent events stream dies permanently.** `mngr forward` runs
   `mngr event <agent> --follow`, which resolves via `try_resolve_readable_host`
   -> `get_host` -> `_is_container_running`
   (`libs/mngr_imbue_cloud/.../providers/instance.py:1219`) -> outer SSH -> auth
   failure -> `MngrError` swallowed to `None` -> imbue_cloud exposes no host_dir
   volume -> target `None` -> exit 1. It respawns on the capped 60s backoff
   forever (`libs/mngr_forward/.../stream_manager.py:518`), so the app receives no
   live agent events for that workspace.

3. **Recovery is a no-op.** Both the stop and start steps of the host restart go
   over outer SSH and fail, so every recovery attempt fails by construction.

4. **The recovery page flaps instead of sticking.** `recovery_probe`
   (`apps/minds/imbue/minds/desktop_client/recovery_probe.py:772`) correctly maps
   `UNAUTHENTICATED` to `backend_unreachable` with a terminal reason, but the inner
   system-interface probe then succeeds and the page auto-dismisses. Observed one
   second apart: probe reason logged at `21:25:01.709`, `restart_failed -> HEALTHY
   (probe succeeded)` at `21:25:02.335`, Electron `revealing parked workspace` at
   `21:25:02.720`. A terminal host state should not be overridden by a
   container-level probe.

## Relationship to the reboot-resilience work

This is the same August 2026 incident described in
[reboot-resilience-rollout.md](./reboot-resilience-rollout.md) — box `9ef5ab2e`,
147.135.97.121. mind-1's VM was restarted as part of that recovery, and that
restart is what wiped the key.

That workstream fixes the **availability** half (workspaces come back instead of
staying down ~19 hours). It does not address the **authorization** half: they come
back unreachable to their owner over the outer door.

It also raises the stakes. `mngr-slices-autostart.service`
(`libs/mngr_imbue_cloud/.../slices/bare_metal_prep.py`) runs `limactl start` per
stopped slice, which is precisely the operation that regenerates cidata. The wipe
therefore becomes automatic and fleet-wide on every box reboot, where previously
it needed an operator to restart a VM by hand.

The behaviour is already known: commit `9324b4fa27` (RSA -> Ed25519 client key
migration) notes that "the lima provider's shared root key is deliberately out of
scope (its VMs overwrite authorized_keys from the baked lima.yaml on every boot)".
It is acknowledged as a constraint, not closed.

That migration also cannot repair an affected machine: it runs against `RUNNING`
hosts only, and it authorizes the new key on the outer host via
`mngr exec --outer`, which needs the working outer SSH that is missing. The
machines that most need it are exactly the ones it skips.

## Repairing an affected machine (no recreate needed)

The supported repair is now `mngr imbue_cloud admin repair-keys` (fleet-wide, or
scoped to one box/VM for break-glass): it patches the slice's stored `lima.yaml`
provision block so future restarts stop truncating, and restores the VM root's
`authorized_keys` from the container's own copy. The manual procedure below is
the same repair by hand, kept for reference.

The owner's key is still in the container's `authorized_keys`, so the exact line
can be lifted from there and re-appended to the VM root. No key material is needed
from the user.

1. Get the tier pool key. Note it is a nested **path**, not a field, and needs
   `VAULT_NAMESPACE=admin`:
   `vault kv get -format=json secrets/minds/<tier>/pool-ssh/POOL_SSH_PRIVATE_KEY`
2. Get the owner's fingerprint from their machine:
   `ssh-keygen -lf ~/.minds/mngr/profiles/<profile>/providers/imbue_cloud/<provider>/hosts/<host_id>/ssh_key.pub`
3. Get the outer port and box address from `minds pool list` (`ssh_port`, not
   `container_ssh_port`).
4. SSH the VM as root with the pool key, find the line in
   `docker exec <cid> cat /root/.ssh/authorized_keys` whose fingerprint matches,
   and guarded-append it: `grep -qxF "$KEY" ... || printf '%s\n' "$KEY" >> ...`,
   then `chmod 600` / `chown root:root`.
5. Verify end to end on the VM: `journalctl -u ssh --since "-5 min"` should show
   `Accepted publickey for root SHA256:<their fingerprint>` from their client.

Have the user restart the app afterwards to clear the events-stream backoff ladder
and the latchkey `_provisioned_hosts` marker rather than waiting them out.

**The manual append alone does not survive the next VM restart** on a VM carved
before the lima fix -- its stored `lima.yaml` still truncates on every start.
The `repair-keys` sweep closes that too (it patches the stored `lima.yaml`), and
an owner whose outer access works can simply reconnect: the client's
ensure-adopted pass installs the in-VM reconciler, after which restarts can no
longer drop the key.

## What to fix

- Make `_build_root_authorized_keys_block` append-if-absent rather than truncate —
  the pool-key script two steps later (`lima_slice.py:94`) already does exactly
  this with a `grep -qxF ||` guard, so the pattern is established.
- Add a lease-key reconcile so a restart cannot orphan an owner: the outer
  `authorized_keys` needs a writer that re-asserts the leased key, the way the
  container path already re-asserts it on rebuild.
- Treat `UNAUTHENTICATED` distinctly from stopped/paused/crashed in
  `LatchkeyDiscoveryHandler`, and warn rather than returning silently.
- Do not let a container-level probe auto-dismiss a terminal host state in
  `recovery_probe`.
- Reconsider the `RUNNING`-only gate on the RSA -> Ed25519 migration.

## Resolution of this instance

`host-f202830a653a4db79f1050bf7f5bb32f` was repaired on 2026-08-13 by appending
the owner's key (`SHA256:M6g7NqR0…`) to the VM root's `authorized_keys`
(825 -> 1550 bytes, 2 -> 3 keys, perms unchanged at `600 root:root`). Verified by
four `Accepted publickey for root SHA256:M6g7NqR0…` entries in the VM's sshd
journal from the owner's client within five minutes. The hand-append alone
would not have survived this VM's next restart (it was carved before the lima
fix); the `repair-keys` sweep patches its stored `lima.yaml`, and the owner's
next connect adopts the host (installing the in-VM reconciler), which closes
the failure mode for good.
