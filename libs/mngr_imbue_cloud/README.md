# mngr_imbue_cloud

Provider backend plugin and CLI for Imbue Cloud (the imbue-team-hosted leasing
service that talks to `remote_service_connector`).

The plugin owns these concern areas, all reachable only through `mngr` commands:

- **auth** — SuperTokens session: signup, signin, oauth, refresh, signout, status
- **hosts** — lease/release/list pre-provisioned pool hosts
- **keys** — LiteLLM virtual key management (`mngr imbue_cloud keys litellm ...`)
- **buckets** — R2 bucket + scoped-key management (`mngr imbue_cloud bucket ...`)
- **tunnels** — Cloudflare tunnel + service + auth-policy management
- **admin pool** — operator-only pool provisioning (Vultr + Neon)

## Configuration

Each signed-in account is its own provider instance entry in
`~/.mngr/config.toml`:

```toml
[providers.imbue_cloud_alice]
backend = "imbue_cloud"
account = "alice@imbue.com"
# connector_url is optional; defaults to the prod URL.
```

The default connector URL can be overridden via the
`MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL` environment variable.

## Sign in

```bash
mngr imbue_cloud auth signin --account alice@imbue.com
# or browser-based OAuth:
mngr imbue_cloud auth oauth google --account alice@imbue.com
```

Token state lives at `<default_host_dir>/providers/imbue_cloud/sessions/<user_id>.json`.

## Create an agent on a leased host

Use the standard `mngr create` pipeline -- the plugin's provider backend
runs the lease + SSH bootstrap inside `create_host`, and the rest of mngr's
create flow adopts the pool's pre-baked agent under your chosen name:

```bash
mngr create my-agent@my-host.imbue_cloud_alice --new-host \
    -b repo_url=https://github.com/imbue-ai/forever-claude-template \
    -b repo_branch_or_tag=v1.2.3
```

`--build-arg KEY=VALUE` flags become `LeaseAttributes` (`repo_url`,
`repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`); the connector
matches them via JSONB containment against the pool host's
`attributes` row. Any other `-b` entry (e.g. `--file=Dockerfile`, `.`)
is forwarded verbatim as a build arg to the slow-path container rebuild.

## Fast path vs. slow path (`fast_mode`)

`mngr create` against imbue_cloud can land on a pool host two ways, selected
by `-b fast_mode=<require|prevent>`:

- **`fast_mode=require`** -- the *fast path*. Lease a pool host whose
  `attributes` row exactly matches and adopt its pre-baked `system-services`
  agent. The pool host is fully pre-provisioned, so mngr's create pipeline only
  writes the agent env file (and patches the claude config when an
  `ANTHROPIC_API_KEY` lands in env) before starting the tmux session. If no
  exact match is available, this raises `FastPathUnavailableError` rather than
  falling back.

- **`fast_mode=prevent`** -- the *slow path*, and the **default**. Lease any
  adequately-sized available host (resource attributes only;
  `repo_branch_or_tag`/`repo_url` are dropped), destroy its baked container,
  and rebuild it from the FCT `Dockerfile` via the shared `mngr_vps_docker`
  setup path. mngr's standard create pipeline then does full client-side setup
  -- exactly as if this were a fresh OVH host. The rebuilt container keeps the
  lease's pre-baked `host_id`/`agent_id` so identity stays aligned with the
  connector's lease row.

The slow path needs a usable build context: run `mngr create` from (or
`--project` at) a forever-claude-template checkout whose `imbue_cloud` create
template supplies the Dockerfile build args. The logs state which path was
taken (`imbue_cloud[...] FAST PATH` vs `SLOW PATH`).

The client owns the machine the moment the connector marks it `leased`: if any
step after a successful lease fails, the lease is released back to the pool
(no data wipe -- nothing sensitive exists yet) before the error propagates.

minds drives this automatically: it tries `fast_mode=require` first and, on
`FastPathUnavailableError`, retries with `fast_mode=prevent`.

When the pool is genuinely empty, even the relaxed slow-path lease returns
`ImbueCloudLeaseUnavailableError` (distinct from `FastPathUnavailableError`).

## Destroy / delete / stop

- `mngr destroy <agent>` is *terminal* for imbue_cloud-leased hosts. It
  (1) stops + removes the workspace container, drops the per-host docker
  named volume, deletes the per-host btrfs subvolume under `/mngr-btrfs/`,
  runs `docker system prune -a -f --volumes`, and wipes `/root` + `/tmp`
  (keeping `/root/.ssh/authorized_keys` so the pool-management ssh path
  keeps working through cleanup); then (2) releases the lease back to the
  pool via the connector. Privacy-first ordering: the user's data is gone
  before the connector flips the row to `released`. The underlying VPS is
  destroyed later by
  `apps/remote_service_connector/scripts/cleanup_released_hosts.py`.
- `mngr delete <agent>` (or `mngr imbue_cloud hosts release <lease-id>`)
  runs the same flow; it's the path mngr's GC takes after the
  destroyed-host grace period. Safe to re-run on an already-released lease.
- `mngr stop <agent>` is the "I'll resume this workspace later" path:
  `docker stop`s the container on the leased VPS, preserves the lease +
  the on-disk volume, and `mngr start <agent>` brings the same workspace
  back up on the same VPS.

## Buckets

Create an R2 bucket (for storing files remotely) and mint scoped S3 keys for it.
Requires a paid account. Each bucket is isolated (think one per host); the
server derives the real R2 name as `<user_id_prefix>--<your-name>`.

```bash
# Create a bucket; emits {bucket, key} where key includes the one-time secret.
mngr imbue_cloud bucket create my-backups --account alice@imbue.com

# List / inspect / destroy (destroy refuses a non-empty bucket).
mngr imbue_cloud bucket list
mngr imbue_cloud bucket info my-backups
mngr imbue_cloud bucket destroy my-backups

# Mint additional keys, scoped read-only or read-write, to hand to agents.
mngr imbue_cloud bucket keys create my-backups --alias agent-ro --access read
mngr imbue_cloud bucket keys list                # all keys across buckets
mngr imbue_cloud bucket keys list my-backups     # just this bucket's keys
mngr imbue_cloud bucket keys destroy <access-key-id>
```

The emitted credentials (`access_key_id`, `secret_access_key`, `s3_endpoint`,
`bucket_name`) are standard S3-compatible credentials -- point any S3 client at
the endpoint. The secret is shown only once at creation and is never stored by
the service.

## Pool admin

```bash
mngr imbue_cloud admin pool create --count 1 \
    --version v1.2.3 \
    --workspace-dir ./forever-claude-template \
    --management-public-key-file ./id_ed25519.pub \
    --database-url "$NEON_DB_DIRECT"
```
