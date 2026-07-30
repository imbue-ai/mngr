# Slice hardening rollout: container memory cap + lima 2.2.0

One-time operator runbook for deploying the two slice-hardening changes from
the July 2026 memory-wedge incident (a leased slice VM whose guest OS became
unrecoverable under memory exhaustion while lima still reported it `Running`):

1. **Container memory cap**: every newly created slice workspace container is
   hard-capped at the slice's RAM minus a 1 GiB VM-side reserve
   (`--memory=7168m --memory-swap=7168m` on today's 8 GB slices), so a
   workspace at capacity can no longer starve the VM's own sshd, dockerd, and
   lima-guestagent.
2. **Lima 2.2.0 on the bare-metal boxes**: lima <= 2.1.x guestagents leak one
   goroutine and one socket FD per forwarded connection (roughly 40 MB/day on
   an active workspace, ending in FD exhaustion), fixed upstream in 2.2.0.

Both changes are code-complete on the mngr side. This runbook covers getting
them onto the production fleet. Audience: an operator with production Vault
access and an activated production env (see
[environments.md](./environments.md)).

**Note:** the minds desktop app bundles its *own* lima (pinned at 2.0.3 in
`apps/minds/scripts/build.js` for a macOS-only usernet regression,
lima-vm/lima#4558). That pin is intentionally untouched by this rollout; only
the boxes move to 2.2.0.

## What is automatic vs. what you must do

Automatic once the code is released:

- The memory cap applies to every container created from new code, on both
  paths: pool bakes (operator-side mngr) and slow-path rebuilds (the desktop
  app's vendored mngr, after the next minds release).
- Newly booted slice VMs get the fixed 2.2.0 guestagent once their box has
  lima 2.2.0 installed.

Requires operator action:

- Re-running `just prep-server` on each existing box (the lima upgrade only
  happens at prep; the guard is version-aware, so re-prep is safe and
  idempotent).
- Already-running slice VMs keep their leaky guestagent until rebuilt, and
  already-created containers stay uncapped. Both are resolved by the normal
  pool upgrade cycle; interim mitigations below for slices that will live a
  while longer.

## Step 1: release the code

Merge the PR, then cut a minds release (see [release.md](./release.md)); the
release syncs the vendored mngr into default-workspace-template and tags both
repos. This is what delivers the cap to desktop clients' slow-path rebuilds --
bakes pick it up as soon as the operator's checkout has the merged code.

## Step 2: canary one box

Pick one production box with free slots (`just list-servers`,
`just list-pool-hosts`), then:

```bash
eval "$(uv run minds env activate production)"
just prep-server <canary-box-id>
```

Verify the lima upgrade landed:

```bash
ssh limahost@<box-address> 'cat /usr/local/share/lima/.mngr-installed-lima-version && limactl --version'
# expect: 2.2.0 on both lines
```

Bake one slice at the release tag (region label per the box, see
[production-release-deployment.md](./production-release-deployment.md)):

```bash
just bake-slice-prod <REGION> <minds-vX.Y.Z> 1 --server-id <canary-box-id>
```

Then check, on the box as `limahost`:

- **Cap present**: `limactl shell <new-instance> sudo docker inspect
  <container> --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'`
  must print `7516192768 7516192768` (7168 MiB twice).
- **Guestagent leak fixed**: note the guestagent's FD count inside the new
  VM, fire ~1000 short TCP connections at the slice's VM-root port, and
  confirm the count stays flat (on 2.1.2 it grew ~1:1 per connection):

  ```bash
  limactl shell <new-instance> sudo sh -c 'ls /proc/$(pgrep -x lima-guestagent)/fd | wc -l'
  for i in $(seq 1 1000); do (timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/<vm-ssh-port> && head -c 20 <&3 >/dev/null" &) done; wait
  limactl shell <new-instance> sudo sh -c 'ls /proc/$(pgrep -x lima-guestagent)/fd | wc -l'
  ```

- **Old instances still manageable**: `limactl list` shows the box's
  2.1.2-created instances, and `just destroy-pool-hosts <old-available-row-id>`
  tears one down cleanly with the 2.2.0 CLI.
- **End to end**: create a workspace against the canary slice from the minds
  app (or `mngr create ...@.imbue_cloud_<account>`), connect, then stress
  memory inside it and confirm the workspace degrades (earlyoom shedding /
  cgroup OOM kills) while the VM-root port keeps serving an SSH banner.

## Step 3: fleet rollout

For every remaining production box:

```bash
just prep-server <box-id>       # installs lima 2.2.0; idempotent
```

Then bake the new generation and retire old rows following the standard flows:
[production-release-deployment.md](./production-release-deployment.md) for
per-release capacity, and "Upgrading the pool" in
[host-pool-setup.md](./host-pool-setup.md) for destroying old `available`
rows. Old-version *leased* slices keep running until their leases end; the
connector destroys each slice VM at release.

## Step 4: interim mitigations for long-lived leased slices

Slices leased before this rollout still run the leaky guestagent in an
uncapped container. If a leased slice will survive for weeks more:

- **Reset the guestagent's leak clock** (safe; the unit is
  `Restart=on-failure` and the hostagent reconnects, but in-flight tunneled
  connections drop for a few seconds, so prefer a quiet hour):

  ```bash
  limactl shell <instance> sudo systemctl restart lima-guestagent
  ```

- **Cap the running container in place** (takes effect live; first confirm
  current usage is below the cap, or the update triggers immediate reclaim):

  ```bash
  limactl shell <instance> sudo docker update --memory=7g --memory-swap=7g <container>
  ```

Prioritize by guestagent FD count (over ~200k of the 524k limit is urgent --
at the limit, every new forwarded connection into the slice fails):

```bash
limactl shell <instance> sudo sh -c 'ls /proc/$(pgrep -x lima-guestagent)/fd | wc -l'
```

## Step 5: verify after a week

On each box (as `limahost`), banner-probe its slices and sample guestagent
growth (`limactl list` only sees the instances on the box it runs on):

```bash
limactl list --json | while read -r line; do
  name=$(echo "$line" | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4)
  port=$(echo "$line" | grep -o '"sshLocalPort":[0-9]*' | cut -d: -f2)
  [ -n "$port" ] || continue
  banner=$(timeout 5 bash -c "exec 3<>/dev/tcp/127.0.0.1/$port && head -c 20 <&3" 2>/dev/null)
  [ -n "$banner" ] && state=OK || state=DEAD
  rss=$(timeout 15 limactl shell "$name" sh -c 'ps -o rss= -C lima-guestagent' 2>/dev/null | tr -d ' ')
  echo "$state $name guestagent_rss_kb=$rss"
done
```

Every slice should be `OK`, and 2.2.0 guestagents should hold steady in the
tens of MB (2.1.2 ones grew without bound). A `DEAD` slice means its guest is
unresponsive -- recover it below.

## Tier rollout order: dev -> staging -> production

Steps 2-5 above are written against production but apply verbatim to any tier;
run them on dev first, then staging, and only then production, so the process
itself is rehearsed twice before it touches users. Per-tier notes learned from
the dev rollout:

### Mixed-version fleets are the expected state

Leased slices cannot be destroyed, so during (and after) the rollout every
tier has boxes carrying a mix of 2.1.2-created and 2.2.0-created VMs, and pool
rows baked at several `repo_branch_or_tag` values. That is fine by design:

- The lima upgrade is binary-only. Replacing `limactl` + guest artifacts on a
  box does not disturb running VMs (their hostagents keep the old binary's
  inode), and the 2.2.0 CLI lists, shells into, and destroys 2.1.2-created
  instances.
- Rows baked at an older tag stay leasable: a lease that requests a newer
  `repo_branch_or_tag` falls back to the slow path (container rebuild) on any
  free host. The rebuild applies the memory cap from the row's stamped
  `memory_gb` attribute; a legacy row without `memory_gb` rebuilds uncapped
  (the provider logs a warning).
- Old *available* rows should still be retired promptly after the new
  generation bakes (see "Upgrading the pool" in
  [host-pool-setup.md](./host-pool-setup.md)): every fast-path lease they
  absorb is a slice running the old guestagent.

Order matters per box: **prep before bake**, so every new slice VM boots with
the 2.2.0 guestagent.

One more mixed-version trap, found on the production rollout: a box whose
staged guest image pre-bakes docker-ce 29.5.1 (the pre-2026-07-30 pin) fails
every bake under lima 2.2.0 at `docker run` with `failed to create TTRPC
connection: unsupported protocol: Yunix`. The pin is now 29.6.2 (verified
against the same containerd.io); boxes staged with the old pin must have
their image re-staged before baking: delete
`~limahost/.cache/mngr-slice-base/debian-base.qcow2` on the box and re-run
`just prep-server <id>` (re-download + virt-customize takes a few minutes).
Boxes whose images predate the docker preinstall (2026-06-16) are unaffected
-- their VMs install current docker at first boot.

### Dev tier notes

- Dev bare-metal boxes are **shared across dev envs** (one box can carry
  slices for several `dev-<user>` envs, and `just list-servers` slot
  accounting only counts the activated env's own DB rows -- trust the bake's
  cross-env occupancy guard / `--dry-run`, not the fleet table, for free
  slots). Re-prepping a shared box upgrades lima for everyone's slices on it;
  running VMs are untouched, but give the other devs a heads-up.
- To put a dev-env lease on the fast path from a *released* desktop binary,
  bake with `--from-tag <minds-vX.Y.Z>` (i.e. `just bake-slice-prod`, which is
  env-agnostic despite the name) so the row's `repo_branch_or_tag` equals the
  binary's `FALLBACK_BRANCH`. `bake-slice-dev` stamps a branch name, which
  only matches source runs with `MINDS_WORKSPACE_BRANCH` set.
- The destroy-compat check (2.2.0 CLI destroying a 2.1.2-created instance)
  usually cannot run on a shared dev box: the old instances belong to other
  envs or are leased. Defer that single check to staging, where old
  `available` rows exist to retire.

### Staging tier notes

- Vault: `vault login -method=oidc role=minds_staging` (the default
  `employee` role is denied on `secrets/minds/staging/*`; tokens expire after
  168h, so expect to re-login). Deploys additionally need the
  `minds-staging` Modal profile (see
  [staging-bringup.md](./staging-bringup.md)).
- Redeploy the tier services first (`minds env activate --deploy staging` +
  `minds env deploy --yes-i-mean-staging`, from `main`), then run steps 2-5:
  canary-prep one box, bake one slice at the release tag, run all four canary
  checks -- including destroying one old-generation `available` row with the
  2.2.0 CLI (`just destroy-pool-hosts <old-row-id>`), which is the
  destroy-compat check dev could not perform.
- A tier whose laptop-side mngr profile was last seeded by a pre-cutover
  minds build used to fail every bake at the outer `mngr create` with
  `Cannot merge AgentTypeConfig with ClaudeAgentConfig`: the stale seeded
  `[agent_types.main] parent_type = "claude"` conflicts with the template's
  command-parented `main`. `minds pool create` now runs the seed migration
  itself before invoking the admin bake, so this fixes itself on the first
  bake from a current checkout; only an *older* checkout still needs the
  manual remedy (launch the current minds.app once against the tier).
- Only after the staging canary passes all checks, prep the remaining staging
  boxes, bake the new generation to capacity, and retire the old `available`
  rows.
- Staging is the rehearsal for production: run the same commands in the same
  order you will use for production (production adds the new-boxes-per-region
  capacity step from
  [production-release-deployment.md](./production-release-deployment.md);
  fresh boxes get lima 2.2.0 at `server setup` automatically, so only
  *pre-existing* production boxes need the re-prep).

## Appendix: recovering a wedged slice

If a guest is unresponsive (its ports accept TCP but serve no SSH banner)
while lima reports it `Running`, as `limahost` on the box:

```bash
limactl stop -f <instance>      # guest is unresponsive; graceful stop cannot work
limactl start <instance>        # data disk is separate and survives
```

Transient `address already in use` warnings during start are the old
hostagent's listeners releasing; the new hostagent rebinds within seconds.

Containers created before this rollout have no restart policy, so after the
VM boots, start the container and re-exec sshd (newer containers restart
themselves):

```bash
limactl shell <instance> sudo docker start <container>
limactl shell <instance> sudo docker exec -d <container> sh -c \
  'mkdir -p /run/sshd && ( ! grep -lxs sshd /proc/[0-9]*/comm >/dev/null 2>&1 && /usr/sbin/sshd -D -o MaxSessions=100 -o MaxStartups=100:30:200 )'
```

The user's agents and background services relaunch when they next open the
workspace (or run `mngr start`).
