# Reboot-resilience rollout: slice autostart on box boot + volume-gated workspace autostart

One-time operator runbook for deploying the two reboot-resilience fixes from
the August 2026 all-slices-down incident (production box `9ef5ab2e` /
147.135.97.121 was hard-reset; all 12 leased slice VMs stayed down ~19 hours
because nothing restarts them and users cannot self-recover):

1. **Box-level slice autostart** (mngr-internal#268): box prep now installs
   `mngr-slices-autostart.service`, which starts every stopped `mngr-slice-*`
   lima VM at box boot (bounded parallelism, visible failures). Prep also pins
   `Unattended-Upgrade::Automatic-Reboot "false"` so a staged kernel can never
   reboot a box unannounced.
2. **In-VM volume-gated workspace autostart** (default-workspace-template#381,
   merged): `minds-autostart` is now triggered by a path unit watching a
   readiness marker that only ever exists on the mounted data volume, replacing
   the oneshot that raced the `/mngr-btrfs` mount and died of a start-rate
   limit (mngr-internal#266).

The two compose: the box unit brings the VMs up, the in-VM units bring each
workspace (container + services agent) up. A real reboot of the staging box
proved the box unit (all 4 slice VMs started with no hands) and a pre-merge
variant of the in-VM units (3 of 4 workspaces recovered hands-off; the 4th
exposed the premature-trigger failure that the merged #381 then fixed with
the mount-gated readiness marker).

Audience: an operator with production Vault access and an activated production
env (see [environments.md](./environments.md)). Deferred follow-ups (the
reconciler watchdog, reboot forensics, persistent journald) are tracked in
mngr-internal#333, not here.

## What is automatic vs. what you must do

Automatic once the code is released:

- Newly prepped boxes get the box unit and the apt pin (both live in
  `build_box_prep_script`).
- Newly baked slices get the volume-gated in-VM units (the installer lives in
  default-workspace-template's `.mngr/settings.toml` provider blocks and runs
  at host create).

Requires operator action:

- Re-running `just prep-server` on each existing production box (prep only
  runs when you run it).
- Backfilling the in-VM units on every existing slice VM (the installer only
  runs at host create, so the existing fleet keeps its old racy oneshot until
  backfilled). Run the sweep via `just backfill-autostart` -- see Step 2.

Already done (during incident response and verification):

- Staging box `21ae4720` (15.204.52.75): box unit + apt pin installed via a
  real `prep-server` run; reboot-tested.
- Production box `9ef5ab2e`: apt pin only (hand-applied). It does NOT have the
  box unit; include it in the Step 1 sweep.
- The 4 staging VMs carry a hand-applied pre-merge variant of the in-VM units;
  include them in the Step 2 backfill so the fleet is uniform.

## Prerequisites

- mngr-internal#268 merged, and you are running from a checkout at or after it
  (the sweep uses your local `build_box_prep_script`).
- Production Vault token loaded and the env activated:
  `eval "$(uv run minds env activate production)"` (see
  [vault-setup.md](./vault-setup.md)). No OVH supplier creds needed.

## Step 1: box sweep (box unit + apt pin)

For every `ready` box in `uv run minds server list`:

```bash
just prep-server <server-id>
```

Prep is idempotent and safe on live boxes: the unit is enable-only (it never
starts during prep), starting already-running VMs is not part of its behavior
(it only starts *stopped* slices at boot), and running workspaces are
untouched.

**Note:** re-prep also applies any pending lima upgrade (the version-marker
guard in prep), so this sweep doubles as the lima 2.2.0 sweep from
[slice-hardening-rollout.md](./slice-hardening-rollout.md) if that has not
been completed -- one pass covers both.

Verify per box (as `debian@<box-address>` with the pool key):

```bash
systemctl is-enabled mngr-slices-autostart.service   # -> enabled
sudo apt-config dump | grep Automatic-Reboot          # -> "false"
```

## Step 2: in-VM backfill (existing slices)

Every pre-#381 slice VM must have the merged installer content re-applied.
The merged installer block (in default-workspace-template `main`,
`.mngr/settings.toml`, the `post_host_create_outer_command__extend` of the
pool/slice provider blocks) is explicitly designed as the backfill path: it
removes the old direct enablement, revives units a past boot left dead of a
start-rate limit, writes the `.minds-volume-ready` marker behind a hard
`mountpoint` check, and enables + starts the path unit (which fires
immediately and safely on a running workspace).

Run the sweep (`just backfill-autostart`, wrapping
`mngr imbue_cloud admin server backfill-autostart`; start with `--dry-run`
to see the per-VM plan). Do not hand-loop over the fleet; the sweep
handles:

- **Per-VM services-agent path.** The installer text launches the
  *current* tree's `minds_start_services_agent.sh`; older slices bake it at
  `/mngr/code/scripts/...` while newer ones use
  `/home/user/workspace/system/scripts/...`. Extract the path from each VM's
  existing `/usr/local/sbin/minds-outer-autostart.sh` and substitute it,
  rather than assuming the current layout.
- **Reach.** VMs are only reachable via each box's lima user with the pool
  key (`limactl shell <instance> sudo ...`); the script should sweep
  box-by-box from `minds server list` / `minds pool list` data.
- **Idempotence and liveness.** Applying to a healthy running workspace must
  be a no-op for the user (`docker start` / `mngr start` are no-ops); the
  installer's own `mountpoint` guard makes it refuse to run on a VM whose
  data volume is not mounted -- treat that as a per-VM failure to
  investigate, not something to bypass.

No database migrations are needed for any of this: neither fix touches the
`pool_hosts` schema or the connector.

Verify per VM:

```bash
systemctl is-active minds-autostart.path        # -> active
test -f /mngr-btrfs/.minds-volume-ready && echo ok
```

## Step 3: fleet verification

- Spot-check a few boxes and VMs per the commands above.
- Optional but recommended: pick one lightly-used production box in a
  maintenance window, reboot it, and confirm every slice port comes back with
  no manual action (the staging reboot test took ~3.5 minutes to full
  recovery for 4 slices). Watch `journalctl -u mngr-slices-autostart` on the
  box and `journalctl -u minds-autostart` in a VM if anything lags.
- `just audit-boxes` afterwards to confirm occupancy matches expectations.

## If a workspace is still down after a box reboot

Re-running the merged in-VM installer on that VM is the supported recovery
(it revives dead units and re-fires the start). The box side is
`systemctl start mngr-slices-autostart.service` (idempotent; only starts
stopped slices). Slice data disks survive reboots intact, so recovery is
never data-destructive.

## Related

- mngr-internal#268 (box unit + apt pin), default-workspace-template#381
  (in-VM units, merged), mngr-internal#266 (the original race, closed),
  mngr-internal#333 (deferred follow-ups incl. the backfill script and the
  reconciler watchdog).
- [host-pool-setup.md](./host-pool-setup.md) for prep/bake background;
  [slice-hardening-rollout.md](./slice-hardening-rollout.md) for the prior
  sweep this one composes with.
