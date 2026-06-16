# Slice release-endpoint test — complete handoff

Goal: prove that **releasing a leased slice actually tears down its lima VM on the
bare-metal box and frees the slot**, end-to-end, against the real dev-josh-1
connector. This is the one remaining validation gap for the OVH bare-metal slices
work (the carve/bake side is fully validated; see `HANDOFF.md`).

Everything you need is below. Read `HANDOFF.md` in this directory first for the
overall architecture; this doc is the operational runbook for the release test.

---

## 0. TL;DR of what you're testing

When a user releases a leased slice, the connector's `release_host` HTTP endpoint
(or the hourly cleanup sweep) must:
1. SSH into the slice's bare-metal box (as the lima user, with the pool key),
2. run `limactl delete --force <instance>` + `limactl disk delete --force <disk>`,
3. delete the `pool_hosts` row (freeing the box slot).

**The connector currently deployed to dev-josh-1 PREDATES the slice-release code**,
so step 1 is the gating action: **you must redeploy the connector first**, then
lease a slice and release it, then verify the VM + row are gone.

The teardown commands themselves are already proven (we ran exactly
`limactl delete --force ...` on the box repeatedly to clean up test slices). What's
unverified is the *deployed connector's* `release_host` → `backend_kind == "slice"`
→ `clean_up_slice_on_box` wiring running for real.

---

## 1. Branch / PR state (starting point)

- Branch: `mngr/ovh-exploration`, PR **#2135**. Worktree:
  `/home/user/.mngr/worktrees/ovh-exploration-d98cd14c3ce849a58af09162fd61b187`.
- HEAD at handoff: `c83526fc4` (carve-over-SSH refactor + `file://` image staging,
  all CI green). Run everything with `uv run` from the worktree root.
- The FCT workspace checkout used for bakes:
  `.external_worktrees/forever-claude-template` (already a git repo with `.mngr`).

---

## 2. Environment / credentials setup (do this first, every session)

These are needed for the box (SSH), the pool DB (Neon), and Vault.

```bash
cd /home/user/.mngr/worktrees/ovh-exploration-d98cd14c3ce849a58af09162fd61b187

# --- HCP Vault (for the pool SSH management key) ---
export VAULT_ADDR="https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200"
export VAULT_NAMESPACE="admin"
# Token is at ~/.vault-token; if expired, the user must run `vault login` (interactive -> use `! vault login`).

# --- Pool SSH management private key -> /tmp/slicekey (0600) ---
vault kv get -format=json -mount=secrets minds/dev/pool-ssh \
  | python3 -c "import json,sys; open('/tmp/slicekey','w').write(json.load(sys.stdin)['data']['data']['POOL_SSH_PRIVATE_KEY'])"
chmod 600 /tmp/slicekey

# --- Pool host DB DSN (Neon), from the dev-josh-1 secrets file ---
export DSN=$(python3 -c "import tomllib,os; print(tomllib.load(open(os.path.expanduser('~/.minds-dev-josh-1/secrets.toml'),'rb'))['secrets']['NEON_HOST_POOL_DSN'])")

# psycopg2 is not in the base python; use `uv run --with psycopg2-binary python` for ad-hoc DB queries,
# or `uv run mngr imbue_cloud admin server list --database-url "$DSN"` for the CLI view.
```

### The box + registered server (facts)
- Box public address: **15.204.140.221** (OVH RISE-2, region `vin`).
- `bare_metal_servers` row id: **679c46f7-fb1b-4d13-8852-6ae9e7b254cd**
  (slot_count=8, disk_gb=467, memory_per_slice_gb=8, cpu_overcommit=2.0,
  lima_service_user=`limahost`).
- SSH to the box: `ssh -i /tmp/slicekey limahost@15.204.140.221` (the pool key is
  authorized for `limahost`; `limactl` lives at `/usr/local/bin/limactl` and runs as
  `limahost`).
- Base OS image already staged on the box at
  `/home/limahost/.cache/mngr-slice-base/debian-base.qcow2` (so bakes use `file://`
  and never touch the flaky Debian mirror — see HANDOFF "Carve-over-SSH refactor").
- Per-slice advertised lease attributes: **`{"memory_gb": 8, "cpus": 4}`**
  (this matters for matching a lease to a slice).

### Quick health checks
```bash
# Pool DB view (server + slot usage):
POOL_SSH_PRIVATE_KEY="$(cat /tmp/slicekey)" uv run mngr imbue_cloud admin server list --database-url "$DSN"
# Box VMs (should be empty when idle):
ssh -i /tmp/slicekey -o StrictHostKeyChecking=accept-new limahost@15.204.140.221 'PATH=/usr/local/bin:$PATH limactl list'
```

---

## 3. Step 1 — redeploy the dev-josh-1 connector (MANDATORY)

The deployed connector lacks the slice-release code; redeploy from this branch.
`minds env deploy` re-provisions the whole env tier (Modal env + Neon + SuperTokens
+ **both** apps: connector `rsc-dev` and litellm). It requires **deploy-mode
activation** (sets `MODAL_PROFILE` to the tier's modal workspace) and a valid
`vault login`.

```bash
# Deploy-mode activation must be eval'd into the shell; do activation + deploy in ONE
# shell so the env vars persist (the Bash tool does not keep shell state across calls):
eval "$(uv run minds env activate --deploy dev-josh-1)" && uv run minds env deploy --hard
```
- `--hard` forces `modal deploy --strategy=recreate` so the next request cold-boots
  the new code (no stale containers serving the old connector).
- Modal creds are at `~/.modal.toml` (present). If `vault login` is needed it's
  interactive — have the user run `! vault login` (the deploy reads tier-shared
  Vault secrets, incl. the pool key it pushes into the connector's Modal secret).
- This is heavy (several minutes) and **affects the dev env** — the user explicitly
  authorized redeploying dev-josh-1 for testing.
- Deploy code: `apps/minds/imbue/minds/cli/env.py` (`env deploy`, ~line 1103) ->
  `apps/minds/imbue/minds/envs/per_env_deploy.py` (`deploy_remote_service_connector`,
  which does `modal deploy app.py` into modal env `rsc-dev`).
- After deploy, the connector reads `POOL_SSH_PRIVATE_KEY` from its Modal secret
  (`pool-ssh-dev`, pushed from Vault by the deploy) — this is the key it uses to SSH
  the box for teardown. Same keypair as `/tmp/slicekey`.

Connector URL for dev-josh-1 (from `~/.minds-dev-josh-1/client.toml`):
**`https://minds-dev-dev-josh-1--rsc-dev-api.modal.run/`**

Confirm the new code is live, e.g. `GET <connector_url>/generation` returns, and/or
check the Modal dashboard shows a fresh `rsc-dev` deploy timestamp.

---

## 4. Step 2 — bake a slice to lease

You need an `available` slice in the pool. Bake one (~7-10 min; uses the staged
`file://` image, so it's mirror-independent now):

```bash
POOL_SSH_PRIVATE_KEY="$(cat /tmp/slicekey)" \
  uv run mngr imbue_cloud admin server allocate-slice \
  --count 1 \
  --workspace-dir .external_worktrees/forever-claude-template \
  --database-url "$DSN"
```
Success prints `"succeeded": 1` and a slice with `vm_ssh_port`/`container_ssh_port`
(e.g. 22000/22001) and `host_id`/`agent_id`. The `pool_hosts` row is inserted with
`status='available'`, `backend_kind='slice'`, and `lima_instance_name` /
`lima_disk_name` / `bare_metal_server_id` set (these drive the teardown).

If the box isn't prepped (fresh box), run `prep` first (stages lima + the OS image):
```bash
POOL_SSH_PRIVATE_KEY="$(cat /tmp/slicekey)" uv run mngr imbue_cloud admin server prep \
  --server-address 15.204.140.221 --ssh-user debian --lima-service-user limahost
# (the current box is already prepped + image staged; only needed for a new box,
#  and --ssh-user debian needs the OVH-delivered debian key, not the pool key.)
```

---

## 5. Step 3 — lease the slice, then release it

The release endpoint is **authenticated**: `POST /hosts/{host_db_id}/release`
requires a SuperTokens user JWT (Bearer), `require_admin` + `require_paid_account`,
and the caller must be the lease owner (`leased_to_user == username`). Routes:
- `POST /hosts/lease` (app.py ~2470) — lease a matching available host.
- `POST /hosts/{host_db_id}/release` (app.py ~2550) — release + tear down.
- `GET /hosts` (app.py ~2657) — list your leased hosts (to find `host_db_id`).

### 5a. Authenticate (get a session)
mngr stores a SuperTokens session via the `imbue_cloud auth` CLI; the imbue_cloud
provider then uses it (auto-refresh via `auth_helper.py` / `session_store.py`):
```bash
uv run mngr imbue_cloud auth signin --account <DEV_JOSH_1_USER_EMAIL> --password <PASSWORD>
# The user for dev-josh-1 is josh_staging@imbue.com (this session's userEmail).
# Get the password from the user / Vault. Session is stored under the mngr profile's sessions dir.
```
Requirements to satisfy `require_paid_account`: the account email/domain must be on
the connector's paid list. Check / add via the paid-admin API (needs the connector's
`_PAID_ADMIN_KEY` env value):
- `GET <connector_url>/paid/emails`, `POST /paid/emails/add` (app.py ~2786-2840;
  auth via `require_paid_admin_key`, app.py ~1833).

### 5b. Recommended path — real user flow via mngr (lease = adopt, release)
This exercises the production fast-path lease + the release endpoint:
```bash
# Lease + adopt the baked slice (attributes must match {memory_gb:8, cpus:4}).
# The provider instance is imbue_cloud_<account-slug>; find the exact slug in the FCT
# .mngr/settings.toml ([create_templates.imbue_cloud] / providers) or the minds client config.
uv run mngr create system-services@<workspace-name>.imbue_cloud_<slug> --new-host --no-connect
#   -> ImbueCloudProvider.create_host POSTs /hosts/lease; the row flips to
#      status='leased', leased_to_user=<you>. Confirm with GET /hosts or the DB.

# Release it (this is the endpoint under test):
uv run mngr destroy system-services@<workspace-name>.imbue_cloud_<slug> --force
#   -> drives the connector POST /hosts/{id}/release.
```
NOTE: confirm `mngr destroy` actually calls the connector release for an
imbue_cloud-leased host — trace `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/instance.py`
(the release/destroy path). If `mngr destroy` doesn't call release, use 5c.

### 5c. Direct-API path (most controllable)
Get the access token from the stored session (see `session_store.py` /
`auth_helper.get_active_token`), then:
```bash
TOKEN=...   # SuperTokens access JWT for the paid-admin user
BASE=https://minds-dev-dev-josh-1--rsc-dev-api.modal.run
# Lease (attributes must match the slice):
curl -sS -X POST "$BASE/hosts/lease" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"attributes": {"memory_gb": 8, "cpus": 4}}'
# -> note the returned host id; confirm the row is status='leased'.
# Release:
curl -sS -X POST "$BASE/hosts/<host_db_id>/release" -H "Authorization: Bearer $TOKEN"
# -> expect {"status":"released"} ONLY after the VM is actually torn down.
```

### 5d. Auth-free fallback — the cleanup sweep (tests the SAME teardown)
If the authenticated flow is blocked, you can still validate `clean_up_slice_on_box`
(shared by release_host and the sweep) without auth:
1. Mark the baked slice row `status='removing'` directly in the DB.
2. Trigger the sweep. It's a Modal scheduled function (`app.py` ~line 4094,
   `schedule=modal.Cron("0 * * * *")`, calling `run_pool_host_cleanup_sweep`).
   Trigger on demand via the Modal SDK/CLI against the deployed `rsc-dev` app
   (e.g. invoke the scheduled function), or just wait for the hourly run.
3. Verify teardown (section 6). This proves the teardown wiring but not the HTTP
   auth/ownership path, so prefer 5b/5c for the actual "release endpoint" claim.

---

## 6. Step 4 — verify the teardown actually happened

After release returns success, ALL of these must hold:

```bash
# (a) The VM + data disk are gone on the box:
ssh -i /tmp/slicekey -o StrictHostKeyChecking=accept-new limahost@15.204.140.221 \
  'PATH=/usr/local/bin:$PATH limactl list; echo ---; limactl disk list'
#   -> the slice's mngr-slice-<host_uuid_hex> instance and -data disk must be ABSENT.

# (b) The pool_hosts row is deleted (release deletes the row on full success):
POOL_SSH_PRIVATE_KEY="$(cat /tmp/slicekey)" uv run mngr imbue_cloud admin server list --database-url "$DSN"
#   -> the box's used-slots count must drop back (slot freed).
#   For the raw row, query pool_hosts WHERE host_id=<host_id> -> expect 0 rows
#   (or status='removing' if a step failed and the sweep will retry).
```
Failure semantics to know: `release_host` flips the row to `status='removing'`
(durable, retryable) BEFORE teardown; if teardown fails it returns 5xx and the row
stays `removing` for the hourly sweep to retry. So "still `removing` + VM still
present" = teardown failed; "row gone + VM gone" = success.

---

## 7. Step 5 — cleanup after the test

```bash
# If anything is left over (failed test, leftover slice), tear it down + clear the row:
ssh -i /tmp/slicekey -o StrictHostKeyChecking=accept-new limahost@15.204.140.221 \
  'PATH=/usr/local/bin:$PATH limactl delete --force <instance>; limactl disk delete --force <instance>-data'
DSN="$DSN" uv run --with psycopg2-binary python - <<'PY'
import os, psycopg2
c = psycopg2.connect(os.environ["DSN"])
with c, c.cursor() as cur:
    cur.execute("DELETE FROM pool_hosts WHERE host_id = %s", ("<host_id>",))
    print("deleted", cur.rowcount)
c.close()
PY
rm -f /tmp/slicekey
# Leave the box at 0/8 used, no VMs.
```

---

## 8. Key code references (for debugging the connector behavior)

Connector: `apps/remote_service_connector/imbue/remote_service_connector/app.py`
- `BACKEND_KIND_SLICE` constant ~2110; `build_slice_teardown_commands` ~2118
  (`limactl delete --force` / `limactl disk delete --force`).
- `_run_ssh_commands_on_box` ~2126 (SSH to box:22 as lima user with
  `POOL_SSH_PRIVATE_KEY`); `clean_up_slice_on_box` ~2141 (looks up
  `bare_metal_servers.public_address` + `lima_service_user`).
- `run_pool_host_cleanup_sweep` ~2176; sweep Modal function ~4094.
- `release_host` ~2551 (auth + ownership + flip to `removing` + finish);
  `_finish_releasing_pool_host` ~2629 (branches on `backend_kind`).
- `authenticate_request` ~1501; `require_admin` ~1662; `require_paid_admin_key` ~1833.
- HTTP routes: `/hosts/lease` ~2470, `/hosts/{id}/release` ~2550, `/hosts` ~2657.

Pool-host schema (slice columns `backend_kind` / `lima_instance_name` /
`lima_disk_name` / `bare_metal_server_id`): `apps/remote_service_connector/migrations/`
(008/009/010).

Slice bake / provider (already validated, for reference):
`libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/`: `cli/server.py` (allocate-slice,
prep), `pool_bake.py`, `slice_provider.py`, `lima_slice_client.py`, `bare_metal.py`,
`bare_metal_db.py`.

mngr connector auth: `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/auth_helper.py`,
`session_store.py`; the `imbue_cloud auth` CLI in
`apps/minds/imbue/minds/desktop_client/imbue_cloud_cli.py` (`auth_signin` ~221).

---

## 9. Open unknowns to resolve at test time (don't assume)

1. **dev-josh-1 user password** for `auth signin` — get from the user or Vault.
2. **`imbue_cloud_<slug>` provider-instance name** for dev-josh-1 — read the FCT
   `.external_worktrees/forever-claude-template/.mngr/settings.toml` (the
   `imbue_cloud` create-template / provider block) or `~/.minds-dev-josh-1/client.toml`.
3. **Paid-account status** of the test user — check `GET /paid/emails`; add via the
   paid-admin API if missing (needs `_PAID_ADMIN_KEY`).
4. Whether `mngr destroy` on an imbue_cloud-leased host calls `/hosts/{id}/release`
   (5b) — if not, use the direct-API path (5c).

---

## 10. Gotchas learned this session

- The Debian mirror (`cloud.debian.org`) intermittently TLS-times-out from the box.
  Bakes are now immune (staged `file://` image), but anything else that fetches from
  it can still flake — don't be surprised.
- When SSHing to a *container* at a reused box-forwarded port, use
  `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` (prior slices leave
  stale host keys for the same `box:port`). SSH to the *box* itself (port 22) is
  fine with `accept-new`.
- `psycopg2` isn't in the base python — use `uv run --with psycopg2-binary python`.
- Every commit on this branch re-fires the autofix + CI review gates; batch trivial
  doc/changelog regens into substantive commits to cut gate cycles.
