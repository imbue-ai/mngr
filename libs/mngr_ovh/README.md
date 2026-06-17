# mngr OVH Cloud Provider

OVH Cloud VPS provider backend plugin for mngr. Runs agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` with 1 vCPU / 8 GB RAM / 80 GB SSD at ~$7.99/mo).

See `mngr_vps_docker` for the base architecture and shared infrastructure.

## Setup

OVH's API requires either OAuth2 service-account credentials or the legacy AK/AS/CK signed-request credentials. The plugin uses the official `python-ovh` SDK, so it can also read credentials from the standard `~/.ovh.conf` file.

Set the env vars or add to `~/.mngr/config.toml`:

```toml
[providers.ovh]
backend = "ovh"
endpoint = "ovh-us"
# Either OAuth2:
client_id = "..."
client_secret = "..."
# Or AK/AS/CK:
application_key = "..."
application_secret = "..."
consumer_key = "..."
```

Recognised env vars (in order of precedence after explicit config):
- `OVH_ENDPOINT` (default `ovh-us`)
- `OVH_CLIENT_ID`, `OVH_CLIENT_SECRET`
- `OVH_APPLICATION_KEY` / `OVH_APP_KEY`, `OVH_APPLICATION_SECRET` / `OVH_APP_SECRET`, `OVH_CONSUMER_KEY`

If none are set, `~/.ovh.conf` is consulted via python-ovh's normal discovery.

## Usage

```bash
mngr create my-agent --provider ovh
mngr create my-agent --provider ovh -b --ovh-plan=vps-2025-model1 -b --ovh-datacenter=US-EAST-VA
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
```

### Operator inspection

```bash
mngr ovh list           # show all mngr-tagged OVH VPSes (plan, datacenter, state, expiration, cancel?, IAM tags)
mngr ovh list --all     # include untagged VPSes too -- useful for sanity-checking the account contents
```

## OVH-specific configuration

These fields extend the base `VpsDockerProviderConfig` (see `mngr_vps_docker`):

| Field | Default | Description |
|-------|---------|-------------|
| `endpoint` | `ovh-us` | python-ovh endpoint id (`ovh-eu`, `ovh-us`, `ovh-ca`, ...) |
| `application_key` | `None` (env / `~/.ovh.conf`) | AK |
| `application_secret` | `None` (env / `~/.ovh.conf`) | AS |
| `consumer_key` | `None` (env / `~/.ovh.conf`) | CK |
| `client_id` | `None` (env / `~/.ovh.conf`) | OAuth2 client id |
| `client_secret` | `None` (env / `~/.ovh.conf`) | OAuth2 client secret |
| `default_region` | `US-EAST-VA` | Default VPS datacenter (e.g. `US-EAST-VA`, `US-WEST-OR` for US accounts) |
| `default_plan` | `vps-2025-model1` | Default plan code (1 vCPU / 8 GB RAM / 80 GB SSD, ~$7.99/mo) |
| `default_image_name` | `Debian 12 - Docker` | Default OS image (Docker pre-installed) |
| `bootstrap_ssh_user` | `debian` | Non-root user the OVH image installs the rebuild key for. Override only if you change `default_image_name` to a non-Debian image (e.g. `ubuntu`, `almalinux`). |
| `pricing_mode` | `default` | OVH pricing mode (`default`, `upfront6`, `upfront12`); `upfront6` / `upfront12` get a discount in exchange for prepayment |
| `duration` | `P1M` | ISO-8601 commitment duration (monthly only) |
| `instance_boot_timeout` | `600.0` | Seconds to wait for the OVH order to deliver a VPS |
| `enable_recycle_cancelled` | `True` | Whether `mngr create` may reuse a cancelled-but-still-alive VPS instead of ordering fresh |
| `recycle_safety_margin_hours` | `2` | Min hours of remaining `expiration` for a cancelled VPS to be recyclable |
| `recycle_max_candidates_considered` | `10` | Cap on provider-tagged VPSes evaluated before falling through to a fresh order. Applied before the cancellation/state/expiration filters, so a recyclable candidate further down a long tagged-VPS list may be missed. |

## Billing caveat

OVH classic VPS is billed monthly (no hourly option). `mngr stop my-agent` halts the Docker container only — the VPS keeps running, and you keep being billed until the next renewal anniversary. `mngr destroy my-agent` cancels auto-renewal (no email confirmation needed), but the VPS keeps running until its OVH-side `expiration` date and then auto-decommissions (OVH does not prorate classic VPS cancellations). For monthly subscriptions that's the rest of the current month; for `upfront6` / `upfront12` it can be up to 6 / 12 months of prepaid balance.

## Auto-reuse of cancelled VPSes

To avoid wasting the remainder of a paid month, `mngr create --provider ovh` first looks for a cancelled VPS (tagged by this provider instance) that matches the requested plan + datacenter and has enough buffer until `expiration`. If one is found, it is un-cancelled and reused instead of ordering fresh. Only when no eligible cancelled VPS exists does a new order go out.

This is opt-in via `enable_recycle_cancelled` (default `True`). Set `enable_recycle_cancelled = false` if you'd rather see fresh VPS deliveries on every `mngr create`. Intended usage is to keep a VPS pool warm (e.g. via `mngr_imbue_cloud`) so that destroy → create within the same billing month is essentially free.

## Security caveat (first connect)

OVH's VPS API exposes no way to inject SSH host keys at install time or retrieve the freshly generated host key out-of-band. So the *first* SSH connection from mngr to a new VPS trusts the host key on first use.

Because the OS rebuild already installed our SSH client public key, key-auth is enforced from connection #1. An attacker positioned to MITM the brief first-connect window can passively read the session but cannot impersonate the VPS (they would need our SSH private key). This is comparable to a first-time `git clone git@github.com:...` on a machine that hasn't cached GitHub's host key. After the first connection, strict host-key checking is enforced on every subsequent connection.
