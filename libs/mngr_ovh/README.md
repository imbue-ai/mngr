# mngr OVH Cloud Provider

OVH Cloud VPS provider backend plugin for mngr. Runs agents in Docker containers on OVH classic VPS instances (e.g. VPS-1 at ~$7.60/mo).

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

If none of the above are set, `~/.ovh.conf` is consulted via python-ovh's normal discovery.

## Usage

```bash
mngr create my-agent --provider ovh
mngr create my-agent --provider ovh -b --vps-plan=vps-2025-model1 -b --vps-datacenter=US-EAST-VA
mngr list
mngr exec my-agent "echo hello"
mngr stop my-agent
mngr start my-agent
mngr destroy my-agent
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
| `project_id` | `None` | Reserved for future Public Cloud support; unused for classic VPS |
| `default_region` | `US-EAST-VA` | Default VPS datacenter |
| `default_plan` | `vps-2025-model1` | Default plan code (VPS-1, $7.60/mo) |
| `default_image_name` | `Debian 12 - Docker` | Default OS image (Docker pre-installed) |
| `pricing_mode` | `default` | OVH pricing mode (`default`, `upfront6`, `upfront12`) |
| `duration` | `P1M` | ISO-8601 commitment duration (monthly only) |
| `vps_boot_timeout` | `600.0` | Seconds to wait for the OVH order to deliver a VPS |

## Implementation details

- Uses the official `python-ovh` SDK for HTTP and request signing
- VPSes are tagged with `mngr-provider=<name>` and `mngr-host-id=<id>` via OVH IAM v2 tags on the VPS resource URN
- Discovery: `GET /v2/iam/resource?resourceType=vps` filtered client-side for matching tags
- Provisioning: full `/order/cart` flow (`POST /order/cart` → `POST /cart/{id}/vps` → configure datacenter/OS → assign → checkout → poll `/vps` until the new `serviceName` appears)
- Bootstrap (no cloud-init available): after delivery, `POST /vps/{s}/rebuild` with `publicSshKey` + `doNotSendPassword=true` to pre-install our client key, then SSH in with key auth and pin the host key on first connect (`StrictHostKeyChecking=accept-new` semantics). After pinning, strict checking is enforced.

## Security caveat (first connect)

OVH's VPS API exposes no way to inject SSH host keys at install time or retrieve the freshly generated host key out-of-band (no cloud-init, no userData, no fingerprint endpoint, only graphical VNC). So the *first* SSH connection from mngr to a new VPS performs a one-shot TOFU on the host key.

Because the rebuild already installed our SSH client public key, key-auth is enforced from connection #1. An attacker positioned to MITM the brief first-connect window can **passively read** the session but cannot impersonate the VPS (they would need our SSH private key to satisfy the key-auth handshake). This is comparable to a first-time `git clone git@github.com:...` on a machine that hasn't cached GitHub's host key.

Pinned host keys live under `<profile_dir>/providers/ovh/<provider_name>/known_hosts/<service_name>` and are loaded with strict checking on every subsequent connection.

## Billing caveat

OVH classic VPS is billed monthly (no hourly option). `mngr stop my-agent` halts the Docker container only — the VPS keeps running, and you keep being billed until the next renewal anniversary. `mngr destroy my-agent` removes the VPS but **forfeits the prorated remainder of the current month**. Intended usage is to keep a VPS pool warm (e.g., via `mngr_imbue_cloud`) and reinstall the OS to recycle, rather than destroying.
