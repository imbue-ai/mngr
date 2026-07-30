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
