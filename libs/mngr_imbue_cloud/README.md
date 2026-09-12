# mngr_imbue_cloud

Provider backend plugin and CLI for Imbue Cloud, the imbue-team-hosted leasing service for pre-provisioned pool hosts. All functionality is reachable through `mngr` commands: auth, account plans/quotas, host leasing, LiteLLM virtual keys, R2 buckets, and workspace shares.

## Configuration

Each signed-in account is its own provider instance entry in `~/.mngr/config.toml`:

```toml
[providers.imbue_cloud_alice]
backend = "imbue_cloud"
account = "alice@imbue.com"
# connector_url is optional; when unset, the env var below is used.
```

There is no baked-in default connector URL: it comes from the per-instance `connector_url` field, or, when that is unset, the `MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL` environment variable. If neither is set, the provider raises.

On tiers with a dedicated browser accounts origin (e.g. production's accounts.imbue.com), `auth login` opens the hosted login page there instead of on the connector host: pass `--accounts-url` or set the `MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL` environment variable (the minds desktop client sets it automatically from its `client.toml`). When neither is set, the login page opens on the connector host itself, which is correct on dev/CI tiers.

## Sign in

```bash
# Browser-based (the primary path): opens the hosted accounts page --
# email/password, sign-up, or Continue with Google -- and hands the session
# back to this machine via a localhost loopback + PKCE code exchange.
mngr imbue_cloud auth login

# Headless (tests, SSH sessions): email + password straight to the connector.
mngr imbue_cloud auth signin --account alice@imbue.com
```

`auth login` requires a connector that serves the hosted accounts pages. Against an older connector (e.g. a stale dev/CI env) it fails immediately with an actionable error -- redeploy the env (Imbue-internal: `minds-admin env deploy`), or fall back to `auth signin`.

Account **creation** from the CLI (`mngr imbue_cloud auth signup`) works only on dev/CI tiers: production and staging refuse it (status `SIGNUP_DISABLED`) so every new account goes through the browser flow (`auth login`), which carries the bot-mitigation gate. Signing in headlessly to an existing account works on every tier.

Email verification is non-blocking: a fresh signup counts as signed in immediately, and no verification email is sent at signup. A few actions require a verified email (creating a remote workspace, opening a workspace that was shared with you, and switching to the ally plan); hitting one of those triggers a contextual verification email -- check the inbox (and spam folder), click the link, and retry. `mngr imbue_cloud auth is-verified` reports the current verification state, and `mngr imbue_cloud auth resend-verification` sends the link on demand (rate-limited server-side).

`mngr imbue_cloud auth signout` revokes only this machine's session; pass `--all-devices` to revoke every session for the account (other machines and the browser).

## Account plans and quotas

Every account has a plan whose quotas cap resource use: remote workspaces, buckets, total bucket storage, monthly LLM spend, and synced workspaces. New accounts pick "free" (one remote workspace) or "explorer" (two remote workspaces, in exchange for sharing product data from those workspaces with Imbue) at signup; an account with no recorded choice defaults to "free". "Ally" grants higher limits and requires a paid-listed email. The connector enforces quotas at grant time and returns a structured 403 (`quota_exceeded`, with the entitlement name, limit, and current usage) when a cap is hit. Workspace sharing (`mngr imbue_cloud shares`, self-hosted relays with workspace-terminated TLS) is capped separately at 50 shared workspaces per account rather than through a plan entitlement.

```bash
# Show the plan, entitlement values, and live usage.
mngr imbue_cloud account show

# Switch plans (re-selecting the current plan is a no-op; switching to
# "ally" errors with the reason unless the email is paid-listed).
mngr imbue_cloud account set-plan ally
```

Operator-side account management (plan resets, quota bumps, on-demand storage sweeps) lives in Imbue's internal operator CLI, not in this plugin.

## Create an agent on a leased host

Use the standard `mngr create` pipeline -- the provider leases a pool host and bootstraps it, and the rest of create adopts the pool's pre-baked agent under your chosen name:

```bash
mngr create my-agent@my-host.imbue_cloud_alice --new-host \
    -b repo_url=https://github.com/imbue-ai/default-workspace-template \
    -b repo_branch_or_tag=v1.2.3
```

The recognized build args (`repo_url`, `repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`) select which pool host to lease. One hard requirement rides alongside them: `-b region=<label>` (only lease in that lease region, e.g. `US-EAST-VA`); when no matching host is available the create fails with a clear no-capacity error instead of relaxing it. Any other `-b` entry (e.g. `--file=Dockerfile`, `.`) is forwarded as a build arg to the slow-path container rebuild.

## Fast path vs. slow path (`fast_mode`)

`mngr create` against imbue_cloud can land on a pool host two ways, selected by `-b fast_mode=<require|prevent>`:

- **`fast_mode=require`** (fast path) -- lease a pool host that exactly matches and adopt its pre-baked agent. Almost no client-side setup is needed. If no exact match is available, this raises `FastPathUnavailableError` rather than falling back.
- **`fast_mode=prevent`** (slow path, the **default**) -- lease any adequately-sized available host, rebuild its container from your `Dockerfile`, and do full client-side setup, as if it were a fresh host.

The slow path needs a usable build context: run `mngr create` from (or `--project` at) a default-workspace-template checkout whose `imbue_cloud` create template supplies the Dockerfile build args. The logs state which path was taken (`FAST PATH` vs `SLOW PATH`).

If a step fails after a successful lease, the lease is released back to the pool before the error propagates. When the pool is empty, even the slow-path lease returns `ImbueCloudLeaseUnavailableError`.

minds drives this automatically: it tries `fast_mode=require` first and, on `FastPathUnavailableError`, retries with `fast_mode=prevent`.

## Destroy / delete / stop

- `mngr destroy <agent>` is **terminal**: it wipes the workspace and its data, then releases the lease back to the pool. The user's data is gone before the lease is released.
- `mngr delete <agent>` (or `mngr imbue_cloud hosts release <host-db-id>`) runs the same flow; it's the path mngr's GC takes after the destroyed-host grace period. Safe to re-run on an already-released lease.
- `mngr stop <agent> --stop-host` is the "resume later" path (plain `mngr stop <agent>` only stops the agent process inside the container and leaves the machine running): it gracefully stops the container, halts the slice VM, and uploads the VM's disks (encrypted) to the tier's storage bucket -- the workspace shows as stopping while the upload runs and reports stopped once it verifies; the halted local VM (and its bare-metal slot) is kept through the local-retention window for a fast restart in place, then reaped. `mngr start <agent>` brings the same workspace back: near-instantly on its origin box within the window, or restored onto any same-region box with a free slot after it (the client re-resolves the new coordinates automatically). Against a connector without the workspace-lifecycle endpoints, stop falls back to the old container-only behavior.

## Machine sizing

A remote workspace runs in a **machine** (the sized slice VM; its mngr host id is stable
across stop/start). A machine's size has two independent factors:

- **Units** -- the single compute knob: 1 unit = 1GiB of machine RAM, with vCPUs and
  fair-share bandwidth scaling proportionally. Allowed sizes are any multiple of 8 units
  from 8 to 128; every new workspace starts at the default 8 units.
- **Disk** -- grow-only: the data disk is sized once at creation (3.5GiB per unit, so
  28GB at the default size) and can be grown independently afterwards. It never shrinks.

Resizing is **record-then-restart**: the resize stamps the desired size on the connector
and nothing changes until the machine's next restart (stop it and start it again, or use
the desktop client's restart), which applies the size in place when the machine's box has
room, or restores it onto a box that does -- transparently, the workspace's content and
address contract are unchanged.

```bash
# Show every machine's current/target sizes and whether a restart is pending.
mngr imbue_cloud machines show

# Or just one (by mngr host id, connector row id, or friendly name).
mngr imbue_cloud machines show my-workspace

# Record a resize (applied at the next restart). Units may go up or down;
# disk only grows.
mngr imbue_cloud machines resize my-workspace --units 16
mngr imbue_cloud machines resize my-workspace --disk-gb 56
```

Units and disk are metered by two plan quotas: `max_active_machine_units` caps the units
summed across your running machines, and `max_total_machine_disk_gb` caps data-disk GB
across running + stopped machines. Both return the standard structured 403
(`quota_exceeded`) when a resize, create, or start would exceed them. A size the fleet
cannot place right now fails the start gracefully: the machine lands back on stopped with
a clear "try a smaller size or try again later" error and nothing is lost.

## Adoption and key rotation (slices)

A freshly-leased slice's SSH trust material is bake-time: its sshd host keys
were generated by the operator tooling (and recorded by the connector), and the
VM root's `authorized_keys` was written by the carve's cloud-init (on a
gen-2 slice that file authorizes nothing: the VM root and the container trust
the tier's SSH certificate authority instead, installed by the same cloud-init
and by the container setup, and management access presents a short-lived
CA-signed certificate). On
lease -- and on the first connect for a host leased earlier -- the client
**adopts** the slice: it rotates both endpoints' sshd host keys to fresh
user-generated keys (pinned user-origin in the host-key store, which bootstrap
material can never displace), and installs an in-VM systemd reconciler that
re-asserts a root-owned desired-state `authorized_keys` and host key on every
boot, after cloud-init's replay. That replay is a gen-1 (lima) behavior: a gen-2
slice's cloud-init runs exactly once, at first boot -- its instance-id is
stable and its network comes from the box's DHCP server, so a stop/start or a
restore onto another box never reruns it, and the adopted host key and
`authorized_keys` simply persist. The pins are bound to an address and port,
and a workspace comes back at fresh ports (possibly on another box) on every
restore -- one driven by this client's own `mngr start`, by an operator, by a
watchdog, by a rollback, or by another of your devices. So the client remembers
the endpoints it last wrote the host's pins at (`bound_endpoints.json` in the
per-host state dir) and, before every connection, compares them with the
endpoints the connector currently reports: when they differ, the VM pin and
the container pin are moved to the new endpoints, origins intact, with no
network round trip. A device that has no such record yet (a second device that
only synced the workspace record) seeds it from the synced pins, by port order
when the record predates a relocation (the VM port is always the lower of the
pair). The connector's bake-time keys are dropped once both endpoints are
verified. Adoption is idempotent and marker-driven:
later connects are a pure-local check, with one full re-verification per
process (plus after start/restart/rebuild), which heals drift. A served key
that matches neither the pins nor an in-flight rotation is refused, not
re-trusted -- an operator re-key requires an explicit re-adoption by the user.

Adoption happens once per host, not once per device: the client-side marker is
per-device, so before adopting, the client probes for an installed reconciler
(the fingerprint of a sibling device's adoption) and, when present, verifies
and heals instead of re-rotating the host keys out from under that device --
the synced workspace record is the channel through which the other devices
receive the adopted trust material. Both the adopt and the full-verification
paths finish by bringing the per-host client key current: an in-flight
client-key rotation is resumed, and a legacy RSA client key (from a host
leased before the Ed25519 keygen switch) is rotated to Ed25519 through the
reconciler desired state, with the retired RSA key de-authorized on both
endpoints.

```bash
# Rotate everything for one host: its per-host client key and both endpoints'
# sshd host keys (adopting the host first when needed). Run from a machine
# that leased the host.
mngr imbue_cloud hosts rotate <host-id|host-db-id|name>
```

Operators repair slices hit by the historical cidata `authorized_keys` wipe
(see `apps/minds/docs/deploy/history/rollouts/slice-restart-wipes-owner-ssh-key.md`) with a fleet
sweep in Imbue's internal operator CLI, which patches each slice's stored
`lima.yaml` and restores wiped VM roots from the workspace container's own
`authorized_keys` (copies only -- it never injects new material).

## Buckets

Create an R2 bucket (for storing files remotely). Each bucket is isolated (think one per host) and has exactly **one** S3 key.

```bash
# Create a bucket; emits {bucket, key} where key includes the one-time secret.
mngr imbue_cloud bucket create my-backups --account alice@imbue.com

# List / inspect / destroy (destroy refuses a non-empty bucket).
mngr imbue_cloud bucket list
mngr imbue_cloud bucket info my-backups
mngr imbue_cloud bucket destroy my-backups

# Force-destroy: when the destroy is refused as non-empty, delete ALL of the
# bucket's contents (batched S3 deletes, client-side) and retry the destroy.
# The destroy is attempted first, so any other refusal (e.g. the
# active-workspace interlock below) aborts before anything is deleted.
# Prompts for confirmation unless -y. When the account is over its storage
# quota (keys downgraded to read-only), a cleanup grant temporarily restores
# write access for the deletion.
mngr imbue_cloud bucket destroy my-backups --force -y

# Get working credentials again: rolls the key's secret in place (same
# Access Key ID, fresh secret; the old secret stops working immediately).
mngr imbue_cloud bucket roll-key my-backups

# Inspect key metadata (never includes secrets).
mngr imbue_cloud bucket keys list                # all keys across buckets
mngr imbue_cloud bucket keys list my-backups     # just this bucket's key
```

The emitted credentials (`access_key_id`, `secret_access_key`, `s3_endpoint`, `bucket_name`) are standard S3-compatible credentials -- point any S3 client at the endpoint. The secret is shown only once (at creation or roll) and is never stored by the service.

Two rules protect workspace backups (buckets whose short name is their workspace's host id, `host-<hex>`):

- `bucket create` reserves the `host-` short-name prefix: creating such a name is refused unless a workspace record with that host id exists for your account, so a generic bucket can never collide with a backup bucket.

- `bucket destroy` (with or without `--force`) refuses to destroy a workspace-backup bucket whose workspace record is still ACTIVE -- destroy the workspace first. Destroyed workspaces' backups are retained for 30 days and then reaped automatically by the connector (see the minds backup-retention docs).

**Note:** total storage across all your buckets is capped by your plan's quota. While over the cap, an hourly server-side sweep turns your bucket keys read-only (the same credentials keep working for reads); they are restored automatically once you are back under quota, and an account over its storage quota cannot create new buckets.

A read-only key cannot delete data (restic's `forget`/`prune` need full write access), so getting back under quota goes through a **cleanup grant**:

```bash
# Temporarily restore your downgraded keys to readwrite so cleanup can run.
mngr imbue_cloud account cleanup-grant

# ... run restic forget/prune (or delete objects) against your buckets ...

# Re-measure and settle: restores your keys immediately if you are now under
# quota, or re-downgrades if not.
mngr imbue_cloud account recheck-storage
```

Grants that actually reduce usage are unlimited; only grants that free nothing count against a small rolling budget (so a grant cannot be farmed for extra write time). `recheck-storage` also works standalone -- if you dropped under quota some other way, it restores your keys without waiting for the hourly sweep.
