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

If none of the above are set, `~/.ovh.conf` is consulted via python-ovh's normal discovery.

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
| `project_id` | `None` | Reserved for future Public Cloud support; unused for classic VPS |
| `default_region` | `US-EAST-VA` | Default VPS datacenter |
| `default_plan` | `vps-2025-model1` | Default plan code (1 vCPU / 8 GB RAM / 80 GB SSD, ~$7.99/mo as of 2025-05) |
| `default_image_name` | `Debian 12 - Docker` | Default OS image (Docker pre-installed) |
| `bootstrap_ssh_user` | `debian` | Non-root user the OVH image installs the rebuild key for. Only override if you change `default_image_name` to a non-Debian image (e.g. `ubuntu` for Ubuntu images, `almalinux` for AlmaLinux). |
| `pricing_mode` | `default` | OVH pricing mode (`default`, `upfront6`, `upfront12`) |
| `duration` | `P1M` | ISO-8601 commitment duration (monthly only) |
| `instance_boot_timeout` | `600.0` | Seconds to wait for the OVH order to deliver a VPS |
| `enable_recycle_cancelled` | `True` | Whether `mngr create` may reuse a cancelled-but-still-alive VPS instead of ordering fresh |
| `recycle_safety_margin_hours` | `2` | Min hours of remaining `expiration` for a cancelled VPS to be recyclable |
| `recycle_max_candidates_considered` | `10` | Cap on the number of cancelled VPSes evaluated before falling through to a fresh order |

## Implementation details

- Uses the official `python-ovh` SDK for HTTP and request signing
- VPSes are tagged with `mngr-provider=<name>` and `mngr-host-id=<id>` via OVH IAM v2 tags on the VPS resource URN
- Discovery: `GET /v2/iam/resource?resourceType=vps` filtered client-side for matching tags
- Provisioning: full `/order/cart` flow (`POST /order/cart` → `POST /cart/{id}/vps` → configure datacenter/OS → assign → checkout → poll `/vps` until the new `serviceName` appears)
- Bootstrap (no cloud-init available): after delivery, `POST /vps/{s}/rebuild` with `publicSshKey` + `doNotSendPassword=true` pre-installs our client key. OVH installs that key for the image's default non-root user (e.g. `debian` on `Debian 12 - Docker`), not for root, so the provider then SSHes in as that user, pins the host key on first connect (`StrictHostKeyChecking=accept-new` semantics), sudo-copies `authorized_keys` into `/root/.ssh/`, and verifies SSH-as-root works before handing off to the rest of the provider. After pinning, strict host-key checking is enforced on every subsequent connection.
- Backups disabled: as the final bootstrap step, the provider purges all `qemu*` packages (`apt-get purge --auto-remove 'qemu*'`). OVH automated backups drive the image's `qemu-guest-agent` to freeze the guest filesystem, which causes serious runtime problems on the agent; removing qemu removes the mechanism the backups hook into. The purge runs on both the fresh-order and recycle paths (rebuilding the OS reinstalls the agent), and a failure aborts provisioning so no host is left running with backups enabled. mngr also never orders an OVH backup option in the cart in the first place.

## Security caveat (first connect)

OVH's VPS API exposes no way to inject SSH host keys at install time or retrieve the freshly generated host key out-of-band (no cloud-init, no userData, no fingerprint endpoint, only graphical VNC). So the *first* SSH connection from mngr to a new VPS performs a one-shot TOFU on the host key.

Because the rebuild already installed our SSH client public key, key-auth is enforced from connection #1. An attacker positioned to MITM the brief first-connect window can **passively read** the session but cannot impersonate the VPS (they would need our SSH private key to satisfy the key-auth handshake). This is comparable to a first-time `git clone git@github.com:...` on a machine that hasn't cached GitHub's host key.

Pinned host keys live under `<profile_dir>/providers/ovh/<provider_name>/known_hosts/<service_name>` and are loaded with strict checking on every subsequent connection.

## Billing caveat

OVH classic VPS is billed monthly (no hourly option). `mngr stop my-agent` halts the Docker container only — the VPS keeps running, and you keep being billed until the next renewal anniversary. `mngr destroy my-agent` cancels auto-renewal via `PUT /vps/{s}/serviceInfos` (`renew.deleteAtExpiration=true`) — no email confirmation step, no human in the loop. The VPS still keeps running until the OVH-side `expiration` date (OVH does not prorate classic VPS cancellations) and then auto-decommissions. For monthly subscriptions that's the rest of the current month; for `upfront6` / `upfront12` it can be up to 6 / 12 months of prepaid balance respectively. The cancelled-but-still-alive window is what the auto-reuse logic below exploits.

## Auto-reuse of cancelled VPSes

To avoid wasting the remainder of a paid month, `mngr create --provider ovh` first checks for cancelled VPSes (those with `renew.deleteAtExpiration=true`) tagged by this provider instance. If one matches the requested plan + datacenter and has enough buffer until `expiration`, it is **un-cancelled** in place via `PUT /vps/{s}/serviceInfos` (no email round-trip needed for the reversal), its OS is rebuilt with our SSH key, and the `mngr-host-id` IAM tag is swapped to the new host. Only when no eligible cancelled VPS exists does a fresh order go out.

Eligibility filters (all required):
- **`mngr-provider` IAM tag matches this provider instance's name.** A VPS that mngr ordered but never finished tagging — e.g. an order whose delivery timed out before `_provision_vps`'s tag-immediately step ran — is invisible to the recycle path. See "Adopting slowly-delivered orphan VPSes" below.
- `renew.deleteAtExpiration == true` and `status == "ok"`
- `engagedUpTo` is null (no active engagement commitment)
- `expiration` is at least `recycle_safety_margin_hours` (default 24h) into the future — guards against the billing boundary
- VPS `state` is `running` or `stopped` (not installing/maintenance/etc)
- VPS plan/model and datacenter match the request
- No `mngr-recycling-by` lock tag (another `mngr create` is mid-recycle)

A cooperative lock tag (`mngr-recycling-by=<uuid>`) is attached before any mutating call, and re-read after attach to detect concurrent recyclers. The lock is best-effort: OVH IAM tag writes are not atomic, so under extreme contention two recyclers could both attach their UUIDs. The mitigation is that the second one's rebuild will hit OVH's per-VPS task-lock and fail, falling through to a fresh order.

This is opt-in via `enable_recycle_cancelled` (default `True`). Disable by setting `enable_recycle_cancelled = false` in your provider config if you'd rather see fresh VPS deliveries on every `mngr create`.

Intended usage is to keep a VPS pool warm (e.g., via `mngr_imbue_cloud`) so that destroy → create within the same billing month is essentially free.

## Adopting slowly-delivered orphan VPSes

OVH's order pipeline is asynchronous: a `POST /order/cart/{id}/checkout` returns immediately with an `orderId`, but the actual VPS `serviceName` is only assigned during a later delivery phase. `mngr create` waits up to `instance_boot_timeout` (default 10 min) for that delivery. If OVH is slow (busy region, new-account fraud-review hold, etc.) and the VPS shows up *after* the timeout, the order silently produces a VPS that mngr never tags — invisible to discovery and to the recycle path. A full month of billing would leak.

The recovery mechanism is a **pending-order marker** pattern:

1. **Marker write on timeout.** When `order_and_wait_for_vps` raises `OvhOrderDeliveryTimeoutError`, `_provision_vps` writes a JSON marker file at `<profile_dir>/providers/ovh/<instance>/pending_orders/order-<N>.json` carrying `order_id` + `plan_code` + `region`, then re-raises the original failure. The bake exits at its normal timeout — no extra wait.

2. **Reconcile sweep on every subsequent bake.** At the top of every `_provision_vps`, `_reconcile_pending_orders` walks the marker dir. For each marker, one short OVH poll (`try_poll_order_for_delivered_vps`):
   - If the order has delivered → attach `mngr-provider` / `mngr-host-id` IAM tags, flip `deleteAtExpiration=true`, delete the marker. The `_maybe_claim_recycled_vps` call that runs immediately after sees the newly-tagged orphan as a candidate and claims it for the current bake.
   - If still pending → keep the marker for the next bake's reconcile.

Failure modes are all swallowed (logged at WARNING) so a transient OVH error or a broken marker doesn't block the current bake from proceeding to its normal order path. Worst case the orphan retries on the next bake. No operator intervention required.

Mirrors the shape of `minds.envs.recover`'s recover-target file pattern.
