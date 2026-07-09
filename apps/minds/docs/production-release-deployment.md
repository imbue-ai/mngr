# Production release deployment playbook

Standard protocol for rolling a new minds release out to **production**: deploy
the release code, then add one fresh bare-metal box **per US region** and bake a
full set of slices on each at the release tag.

This playbook is **production-only**. For dev / staging you do *not* buy new
boxes each release -- free capacity by destroying old slices
(`just destroy-pool-host` / `minds env destroy`) and re-bake on the existing
boxes.

## What each deployment adds

Every release we add **one `24sys032-us` box in each of the two US regions**
(the row we standardized on from `admin server pricing`):

| Region label (lease) | OVH datacenter code | Box | RAM | Storage | Slices/box |
|---|---|---|---|---|---|
| `US-EAST-VA` | `vin` | `24sys032-us` (Intel Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x960nvme` | 14 |
| `US-WEST-OR` | `hil` | `24sys032-us` (Intel Xeon-E 2288G, 8c/16t) | 128 GB | `softraid-2x960nvme` | 14 |

So each release nets **+28 baked, leasable production slices** (14 per box, at
8 GB/slice), all advertising the new release's FCT tag so leases land on the
fast path.

> Region wrinkle: OVH **orders** take the datacenter code (`vin` / `hil`); slice
> **bakes** take the lease-region label (`US-EAST-VA` / `US-WEST-OR`). Pair them
> correctly: bake `US-EAST-VA` onto the `vin` box, `US-WEST-OR` onto the `hil` box.

## Prerequisites (once per machine / session)

- `vault` CLI installed, and a **production** Vault token at
  `~/.vault-tokens/production.token` (or run `vault login -method=oidc`).
- `mngr` dev shim on PATH (this repo's `scripts/mngr`); run everything with
  `uv run` from the repo root.
- A Modal profile named `minds-production` in `~/.modal.toml` (created once via
  `modal token set --profile minds-production ...`). This is what authorizes the
  Step 1 deploy. **Modal deploy auth is deliberately NOT in Vault** -- only the
  application secrets the deployed apps read at runtime live in Vault (pushed into
  Modal as `*-production` Secrets by the deploy). `minds env activate --deploy
  production` pins `MODAL_PROFILE=minds-production` and the deploy fails closed if
  that profile is missing or does not match the tier's `modal_workspace`, so a
  misroute to the wrong workspace is caught before anything ships.
- The `24sys032-us` boxes are ordered fresh each release; nothing pre-exists.

### Step 0 -- pick the version and export credentials

```bash
cd <repo-root>

# The release you are deploying. Both mngr and forever-claude-template are tagged
# minds-v<version> (see apps/minds/docs/release.md). Example: 0.3.6
export REL_VERSION=0.3.6
export REL_TAG="minds-v${REL_VERSION}"

# --- Vault: point at the HCP cluster and load the production token ---
export VAULT_ADDR="https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
export VAULT_NAMESPACE="admin"
export VAULT_TOKEN="$(cat ~/.vault-tokens/production.token)"

# --- OVH supplier creds (for `admin server` order/await/setup/list) ---
export OVH_APPLICATION_KEY="$(vault kv get -mount=secrets -field=value minds/production/ovh/OVH_APPLICATION_KEY)"
export OVH_APPLICATION_SECRET="$(vault kv get -mount=secrets -field=value minds/production/ovh/OVH_APPLICATION_SECRET)"
export OVH_CONSUMER_KEY="$(vault kv get -mount=secrets -field=value minds/production/ovh/OVH_CONSUMER_KEY)"

# --- Pool DB DSN + pool SSH key (raw `admin server` commands are not env-aware) ---
# production keeps no local secrets.toml, so the raw server commands need these
# from Vault. (The bake step in Step 6 uses `minds pool create`, which resolves
# both from Vault itself once the tier is activated -- these exports just cover
# the raw `admin server` commands in Steps 3-5 and `server list`.)
export MINDS_HOST_POOL_DSN="$(vault kv get -mount=secrets -field=value minds/production/neon/DATABASE_URL)"
export POOL_SSH_PRIVATE_KEY="$(vault kv get -mount=secrets -field=value minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY)"
```

Sanity check the token and that the OVH creds resolved:

```bash
vault token lookup >/dev/null && echo "vault ok"
[ -n "$OVH_APPLICATION_KEY" ] && [ -n "$OVH_CONSUMER_KEY" ] && echo "ovh creds ok"
[ -n "$MINDS_HOST_POOL_DSN" ] && echo "dsn ok"
```

## Step 1 -- start the production deploy (in the background)

Check out the release you are shipping, then kick off the connector + LiteLLM
proxy deploy **in the background** so it runs while you confirm the order
(instead of blocking on it):

```bash
git fetch --tags
git checkout "${REL_TAG}"            # or the release SHA on main

( eval "$(uv run minds env activate --deploy production)" \
  && uv run minds env deploy --yes-i-mean-production ) \
  > /tmp/minds-deploy-${REL_VERSION}.log 2>&1 &
DEPLOY_PID=$!
echo "deploy running in background (pid ${DEPLOY_PID}); log: /tmp/minds-deploy-${REL_VERSION}.log"
```

This pushes every production Vault secret into Modal and `modal deploy`s both
`rsc-production` (connector) and `llm-production` (proxy). `--yes-i-mean-production`
is the mandatory safety bar. It typically finishes in a few minutes -- long
before the boxes ordered below are delivered.

> The connector/proxy deploy is independent of the slice FCT version (slices
> carry the release via their baked FCT tag, Step 6). We start it first only so
> the runtime services match the release before the new capacity comes online; we
> gate on its success before setting up the boxes (Step 5).

## Step 2 -- preview + approve the orders (while the deploy runs)

Print OVH's **real** price preview (base + mandatory add-ons + one-time setup)
and the exact server specs for both regions **without charging**, using
`order --dry-run` (builds + assigns a non-committal cart, prints the preview, then
deletes the cart -- no charge, no prompt, no DB write):

`24sys032-us` has **two mandatory option families** that each offer a choice, so
both must be passed via `--option` (discovered on the first run; the command
errors and lists the offers + monthly prices until every such family is chosen):

- `--option bandwidth-1000-24sys-us` -- 1 Gbps public bandwidth, **$0/mo** (the
  paid `bandwidth-2000-24sys-us` is +$120/mo; slices don't need it).
- `--option vrack-bandwidth-500-24sys-us` -- vRack private-network bandwidth,
  **$0/mo** (we don't use vRack for slices; the paid 1000 tier is +$23/mo).

```bash
for DC in vin hil; do
  echo "===== ${DC} ====="
  uv run mngr imbue_cloud admin server order --dry-run \
      --plan-code 24sys032-us \
      --region "${DC}" \
      --memory-gb 128 \
      --storage softraid-2x960nvme \
      --option bandwidth-1000-24sys-us \
      --option vrack-bandwidth-500-24sys-us
done
```

Each block prints `About to order 24sys032-us in <dc>: 128GB RAM,
softraid-2x960nvme, 8c/16t, 960GB usable disk (RAID1) -> 14 slices of 8GB` and an
`OVH price preview:` (subtotal / tax / due now), followed by `Dry run: cart
deleted, no order placed.` Review the price, specs, and slice count, and approve
before Step 3.

> **Expected cost:** ~$100/mo recurring per box, plus a **~$60 one-time setup fee**
> the first month, so budget **~$160 due now per box** (~$320 for the pair). OVH
> periodically runs promotions that waive the setup fee (e.g. a run on 2026-07-09
> showed exactly $100 due now, $0 setup) -- treat any such waiver as a bonus, not
> the norm. The dry-run cart preview's "due now" is authoritative for what you'll
> actually be charged on the day; trust it over the `pricing` table's
> catalog-derived `SETUP` column.

## Step 3 -- place the orders (after approval)

Ordering does not depend on the deploy, so place both as soon as the price is
approved (the background deploy keeps running). Since you've already reviewed the
preview, use `--yes` to skip the interactive confirm:

```bash
uv run mngr imbue_cloud admin server order --yes \
    --plan-code 24sys032-us --region vin \
    --memory-gb 128 --storage softraid-2x960nvme \
    --option bandwidth-1000-24sys-us \
    --option vrack-bandwidth-500-24sys-us

uv run mngr imbue_cloud admin server order --yes \
    --plan-code 24sys032-us --region hil \
    --memory-gb 128 --storage softraid-2x960nvme \
    --option bandwidth-1000-24sys-us \
    --option vrack-bandwidth-500-24sys-us
```

Each records a `bare_metal_servers` row at status `ordered` and echoes its
**server id**. Save both:

```bash
export SRV_VIN=<server-id-printed-for-vin>
export SRV_HIL=<server-id-printed-for-hil>
```

## Step 4 -- await delivery

Delivery for `24sys032-us` is usually ~1h (the pricing table showed `~1h` /
high stock). Resumable; a no-op once delivered.

```bash
uv run mngr imbue_cloud admin server await-delivery --server-id "$SRV_VIN"
uv run mngr imbue_cloud admin server await-delivery --server-id "$SRV_HIL"
```

Each flips the row to `delivered` and records the serviceName + public IP.

## Step 5 -- confirm the deploy landed, then setup boxes -> ready

First make sure the background deploy from Step 1 finished cleanly (by now it will
have completed long before delivery). Do not bake against production until it has:

```bash
wait "$DEPLOY_PID" \
  && echo "deploy OK" \
  || { echo "DEPLOY FAILED -- inspect the log and re-run before continuing"; tail -n 40 /tmp/minds-deploy-${REL_VERSION}.log; }
```

Then provision both delivered boxes to `ready`.

`setup` reinstalls Debian with our injected SSH host key (destructive, expected),
waits for SSH, then installs qemu/lima/tooling and stages the slice guest image.
Resumable via status.

```bash
uv run mngr imbue_cloud admin server setup --server-id "$SRV_VIN"
uv run mngr imbue_cloud admin server setup --server-id "$SRV_HIL"
```

Both end at status `ready`. Confirm:

```bash
uv run mngr imbue_cloud admin server list
```

You should see both new boxes `ready`, plan `24sys032-us`, 14 slots each, in
their regions.

## Step 6 -- bake 14 slices per box at the release tag

Activate the production tier (use-mode is enough; the bake resolves the pool key
+ DSN from Vault). Then bake a full box's worth of slices, at the release FCT
tag, pinned to each box by `--server-id`:

```bash
eval "$(uv run minds env activate production)"

# US-EAST-VA slices onto the vin box
just bake-slice-prod US-EAST-VA "${REL_TAG}" 14 --server-id "$SRV_VIN"

# US-WEST-OR slices onto the hil box
just bake-slice-prod US-WEST-OR "${REL_TAG}" 14 --server-id "$SRV_HIL"
```

Notes:
- `bake-slice-prod` clones forever-claude-template at exactly `${REL_TAG}` and
  bakes byte-for-byte tag content, stamping `repo_branch_or_tag=${REL_TAG}` so the
  production binary's leases land on the fast path.
- Slices bake at most 4 at a time per box (the rest queue); a full box takes a
  while. The two boxes are independent -- run them in **parallel** in two shells
  to halve wall-clock.
- Per-slice size (`memory_gb=8`, `cpus`) is computed from the box and stamped
  automatically; don't pass it.
- Tip: `--dry-run` first (append to the recipe's extra args) confirms placement +
  per-slice sizing without baking.

## Step 7 -- verify

```bash
# 28 new `available` rows (14 per box), all at repo_branch_or_tag = ${REL_TAG}
just list-pool-hosts

# boxes healthy + slot accounting
uv run mngr imbue_cloud admin server list
```

Optionally exercise a real production create against the new tag from the minds
app and confirm it takes the **fast path** (leases a baked slice, no container
rebuild).

## Rollback / cleanup

- A bake that fails mid-flight rolls back its own VM; re-run the same
  `bake-slice-prod` to top the box back up to 14 (it bakes into free slots).
- To retire capacity later: `just list-pool-hosts` to find row ids, then
  `just destroy-pool-host <id>` (tears down the slice VM, frees the slot, drops
  the row). To decommission a whole box: destroy all its slices, then
  `mngr imbue_cloud admin server` teardown (manual OVH cancel).
- OVH classic billing: cancelling a box stops renewal but you keep it (and pay)
  until its expiration date; there's no proration.

## Open questions / future improvements

- Consider adding first-class justfile recipes for the server lifecycle
  (`order` / `await-delivery` / `setup`) so the whole protocol is
  `just`-driven, mirroring `bake-slice-prod` / `list-pool-hosts`.
- Consider a single wrapper recipe (e.g. `just deploy-release <version>`) that
  chains Steps 2-6 for both regions with the right server-id plumbing.
</content>
</invoke>
