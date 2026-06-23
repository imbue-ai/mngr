# mngr_imbue_cloud

Provider backend plugin and CLI for Imbue Cloud, the imbue-team-hosted leasing service for pre-provisioned pool hosts. All functionality is reachable through `mngr` commands: auth, host leasing, LiteLLM virtual keys, R2 buckets, and Cloudflare tunnels.

## Configuration

Each signed-in account is its own provider instance entry in `~/.mngr/config.toml`:

```toml
[providers.imbue_cloud_alice]
backend = "imbue_cloud"
account = "alice@imbue.com"
# connector_url is optional; when unset, the env var below is used.
```

There is no baked-in default connector URL: it comes from the per-instance `connector_url` field, or, when that is unset, the `MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL` environment variable. If neither is set, the provider raises.

## Sign in

```bash
mngr imbue_cloud auth signin --account alice@imbue.com
# or browser-based OAuth:
mngr imbue_cloud auth oauth google --account alice@imbue.com
```

## Create an agent on a leased host

Use the standard `mngr create` pipeline -- the provider leases a pool host and bootstraps it, and the rest of create adopts the pool's pre-baked agent under your chosen name:

```bash
mngr create my-agent@my-host.imbue_cloud_alice --new-host \
    -b repo_url=https://github.com/imbue-ai/forever-claude-template \
    -b repo_branch_or_tag=v1.2.3
```

The recognized build args (`repo_url`, `repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`) select which pool host to lease. Any other `-b` entry (e.g. `--file=Dockerfile`, `.`) is forwarded as a build arg to the slow-path container rebuild.

## Fast path vs. slow path (`fast_mode`)

`mngr create` against imbue_cloud can land on a pool host two ways, selected by `-b fast_mode=<require|prevent>`:

- **`fast_mode=require`** (fast path) -- lease a pool host that exactly matches and adopt its pre-baked agent. Almost no client-side setup is needed. If no exact match is available, this raises `FastPathUnavailableError` rather than falling back.
- **`fast_mode=prevent`** (slow path, the **default**) -- lease any adequately-sized available host, rebuild its container from your `Dockerfile`, and do full client-side setup, as if it were a fresh host.

The slow path needs a usable build context: run `mngr create` from (or `--project` at) a forever-claude-template checkout whose `imbue_cloud` create template supplies the Dockerfile build args. The logs state which path was taken (`FAST PATH` vs `SLOW PATH`).

If a step fails after a successful lease, the lease is released back to the pool before the error propagates. When the pool is empty, even the slow-path lease returns `ImbueCloudLeaseUnavailableError`.

minds drives this automatically: it tries `fast_mode=require` first and, on `FastPathUnavailableError`, retries with `fast_mode=prevent`.

## Destroy / delete / stop

- `mngr destroy <agent>` is **terminal**: it wipes the workspace and its data, then releases the lease back to the pool. The user's data is gone before the lease is released.
- `mngr delete <agent>` (or `mngr imbue_cloud hosts release <host-db-id>`) runs the same flow; it's the path mngr's GC takes after the destroyed-host grace period. Safe to re-run on an already-released lease.
- `mngr stop <agent>` is the "resume later" path: it stops the container but preserves the lease and on-disk data, and `mngr start <agent>` brings the same workspace back up.

## Buckets

Create an R2 bucket (for storing files remotely) and mint scoped S3 keys for it. Requires a paid account. Each bucket is isolated (think one per host).

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

The emitted credentials (`access_key_id`, `secret_access_key`, `s3_endpoint`, `bucket_name`) are standard S3-compatible credentials -- point any S3 client at the endpoint. The secret is shown only once at creation and is never stored by the service.
