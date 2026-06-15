# OVH bare-metal slices — fast-path FCT lease: test plan & required changes

Goal (user's words): allocate a real slice on the box, bake an available FCT host for
that slice, then create a new workspace that leases that fast-path imbue_cloud host and
confirm it actually works (and that release tears the slice down).

The thesis to validate: **a slice can be baked + leased on the fast path in almost exactly
the same way as an OVH VPS pool host.** Below: where that holds, where it diverges, the
concrete changes needed, and the exact commands to run.

Box under test: `15.204.140.221` (serviceName `ns1012536.ip-15-204-140.us`, dc `vin`),
`bare_metal_servers` row `679c46f7-fb1b-4d13-8852-6ae9e7b254cd`, 8 slots / 0 used, lima
service user `limahost`. dev-josh-1 minds env (josh@imbue.com authed), provider instance
`imbue_cloud_josh-imbue-com`.

---

## How the two paths line up

OVH VPS pool bake (`mngr imbue_cloud admin pool create`, `cli/admin.py:_create_single_pool_host`):
1. `mngr create system-services@pool-<hex>-host.ovh --template main --template ovh ...` from the FCT workspace.
2. `mngr stop`, ensure container sshd `MaxStartups`.
3. `mngr list --format json` → capture `agent_id`, `host_id`, `vps_address`, container key path.
4. install + enable ufw on the VPS.
5. install the **pool management public key** on the VPS root (SSH) **and** in the container (`mngr exec`).
6. destroy the bootstrap-created chat agent + remove `/code/runtime/initial_chat_created`.
7. insert the `pool_hosts` row (`ssh_port=22`, `container_ssh_port=2222`, attributes, region).

Lease (fast path), `ImbueCloudProvider._create_host_fast_path`:
- `POST /hosts/lease` matches `status='available' AND attributes @> <req>` (region-agnostic unless `-b region=`).
- Connector injects the user key into **both** `vps_address:ssh_port` and `vps_address:container_ssh_port` as `ssh_user` (root) using `POOL_SSH_PRIVATE_KEY` (`_append_authorized_key`). 502 if either fails.
- mngr waits for container sshd, rewrites `/mngr/data.json` host_name, adopts the pre-baked `system-services` agent (container connection on `container_ssh_port`).

A slice is the same `pool_hosts` row shape (`backend_kind='slice'` + lima fields + box-forwarded
ports). `SliceVpsDockerProvider` reuses `create_host_on_existing_vps` unchanged, so the **bake
mechanics and the lease/adopt mechanics are genuinely the same** — with the divergences below.

---

## Divergences (must address or consciously accept)

### D1 — Outer-host SSH port is hardcoded to 22 in the lease-side provider  (REAL BUG for slices)
`ImbueCloudProvider.outer_host_for` (`instance.py:1844`) and `_ensure_outer_host_key_known`
(`instance.py:802,811`) connect to `vps_address:22`. For an OVH VPS the VM root sshd *is* on
:22, so this is correct. For a slice the VM root sshd is reached at `box:ssh_port` (a forwarded
port like 22xxx); `box:22` is the **bare-metal box's own sshd** (root login disabled; the pool
key authorizes `limahost`, not root).

Impact:
- `mngr list` discovery (`discover_hosts_and_agents` → `_collect_listing_raw_via_outer`) hits
  `box:22`, fails, and falls back to a lease-only stub → the slice shows as **CRASHED /
  UNAUTHENTICATED** even though it is fully working.
- `destroy_host`/`delete_host` outer-wipe hits `box:22`, fails, logs a warning, and proceeds to
  release. The connector release destroys the whole VM via `limactl` anyway, so the privacy
  outcome is fine — just noisy.
- `mngr exec` and the fast-path adopt use the **container** connection (`container_ssh_port`),
  so those are unaffected and work.

Fix: use `leased.ssh_port` instead of the literal `22` in `outer_host_for` and
`_ensure_outer_host_key_known`. OVH rows have `ssh_port=22`, so this is a no-op for them. The
slow path already does this correctly (`_create_host_slow_path` uses `lease_result.ssh_port`).
Without the fix, step 6 must verify via `mngr exec`, not `mngr list` (which will misreport).

### D2 — The slice bake is not codified, and must install the pool key + tear down the bootstrap chat agent
`admin server allocate-slice` is **report-only** (prints placement + attributes; no bake, no row).
There is no slice analogue of `_create_single_pool_host`. A leaseable slice row requires, on the box:
- a. `mngr create system-services@<h>.imbue_cloud_slice --template main --template slice ...`
     (carve lima VM → docker container → FCT bake).
- b. capture `agent_id`, `host_id`, **both forwarded ports** (`ssh_port`=VM root sshd,
     `container_ssh_port`=container sshd), `lima_instance_name`, `lima_disk_name`.
- c. **install the POOL management public key into the VM root authorized_keys AND the container
     root authorized_keys.** This is essential: the connector's lease-time `_append_authorized_key`
     SSHes as root with the pool key on both ports — without it the lease 502s. (The VM root is
     currently authorized only for the provider's per-instance `vps_ssh_key`, not the pool key.)
- d. destroy the bootstrap-created chat agent + `rm -f /code/runtime/initial_chat_created` (so the
     user's first lease re-creates the chat agent under their workspace name).
- e. insert the slice row (`build_slice_pool_host_insert_values` + `insert_slice_pool_host`).

Capturing **both** ports argues for an **in-process** bake command (call `SliceVpsDockerProvider`
directly and read the ports from `SliceProvisionResult`), because `mngr list` only surfaces the
container port — the VM-root forwarded port is never in its output. The OVH "shell `mngr create`
then `mngr list`" approach therefore does not transfer cleanly.

### D3 — Getting this branch's mngr onto the box is not codified
`SliceVpsDockerProvider` must run on the box (limactl is local there). Needs an rsync of this
worktree to `limahost@box` + `uv sync --all-packages`, mirroring `_sync_mngr_into_template` /
the minds vendor-sync. The user explicitly asked NOT to raw-dog ad-hoc SSH — codify as e.g.
`admin server sync-mngr` (or fold into a `bake-slice`). For first validation it can be done by
hand, but the user wants it codified.

### D4 — FCT needs a `slice` create-template + a `[providers.imbue_cloud_slice]` block
Model the template on **`ovh`** (NOT `lima`): a slice runs a docker container *inside* the VM via
vps_docker, exactly like the OVH path; the `lima` template instead runs the agent directly in the
VM as root with no container. Needed in the FCT worktree's `.mngr/settings.toml`:
- `[create_templates.slice]`: `target_path="/mngr/code/"`, `build_arg=["--file=Dockerfile","."]`,
  `idle_mode="disabled"`, `pass_host_env__extend=["ANTHROPIC_API_KEY","ANTHROPIC_BASE_URL","MNGR_PREFIX","GH_TOKEN"]`,
  `post_host_create_command__extend=["/usr/local/bin/fct-seed"]`.
- `[providers.imbue_cloud_slice]`: `is_enabled=false` (mirror lima/ovh so the in-container mngr
  skips the block where the plugin is absent; `@<h>.imbue_cloud_slice` still resolves by name),
  `box_public_address="15.204.140.221"`, `host_dir="/mngr"`. `box_public_address` is provider
  config and cannot be set via a template `--setting`, so it must live in this block (or
  `MNGR__PROVIDERS__IMBUE_CLOUD_SLICE__BOX_PUBLIC_ADDRESS`).

Sub-divergence (runsc): the OVH/vultr templates harden the agent container with gVisor
(`docker_runtime=runsc`, `--workdir=/ --security-opt=no-new-privileges`). The slice VM installs
only plain docker (`get.docker.com`, runc) — so the `slice` template must **not** select runsc.
The QEMU VM is the isolation boundary; the container is runc. Acceptable, but flag it.

### D5 — Inference + connector reachability from/to the box
- The bake exercises real inference (the bootstrap's chat agent runs `/welcome`). Export on the box
  before `mngr create`: `ANTHROPIC_API_KEY` (a LiteLLM key minted for josh@imbue.com),
  `ANTHROPIC_BASE_URL=https://minds-dev-dev-josh-1--llm-dev-proxy.modal.run/`, and
  `REMOTE_SERVICE_CONNECTOR_URL` / `MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL=https://minds-dev-dev-josh-1--rsc-dev-api.modal.run/`.
  The box needs outbound network to those Modal URLs (it does, via VM NAT for the agent; the bake
  driver runs on the box itself).
- The connector (on Modal) must reach the box for lease + release: `box:22` as `limahost` (release
  teardown via limactl) and `box:ssh_port` / `box:container_ssh_port` as root (lease key injection),
  all with `POOL_SSH_PRIVATE_KEY`. Confirm Modal egress reaches `15.204.140.221` on those ports and
  that the dev-josh-1 connector env carries `POOL_SSH_PRIVATE_KEY`.

### Non-divergences (verified to already line up)
- Lease attribute matching: slice advertises `{memory_gb:8, cpus:<derived>}`; fast-path request
  `-b memory_gb=8 -b cpus=N` matches via `attributes @>` containment. ✓
- Region: lease is region-agnostic unless `-b region=` is passed, so the slice's `region='vin'`
  column does not block the lease. ✓
- Release fork: connector branches on `backend_kind='slice'` → `clean_up_slice_on_box` → SSH box
  as `lima_service_user` + `limactl delete`/`disk delete`. Row stays `removing` (slot held) until
  the VM is really gone. ✓ (unit-tested; this test exercises it live for the first time.)
- `vps_docker` btrfs seam: slice provider overrides `_prepare_btrfs_on_outer` to just create the
  per-host subvolume on the VM's already-mounted btrfs data disk (no loopback). ✓

---

## Minimal set of changes to make the test pass

1. **D1 fix** (`instance.py`): `outer_host_for` and `_ensure_outer_host_key_known` use
   `leased.ssh_port` instead of `22`. (Small, low-risk; needed for clean `mngr list`.)
2. **D4** (FCT worktree `.mngr/settings.toml`): add `[create_templates.slice]` + `[providers.imbue_cloud_slice]`.
3. **D2 + D3** (codify): a `mngr imbue_cloud admin server bake-slice` (in-process) that syncs mngr
   to the box (or assumes synced), runs the FCT slice bake there, installs the pool key on VM root +
   container, tears down the bootstrap chat agent + sentinel, and inserts the slice row — and a
   `admin server sync-mngr`. For the *first* live run these may be done semi-manually, but the user
   wants them codified before calling it done.
4. **D5**: mint a LiteLLM key for josh@imbue.com and export the inference/connector env on the box.

---

## Concrete test commands

Prereqs (operator laptop):
```bash
# authed dev env (josh@imbue.com)
eval "$(uv run minds env activate dev-josh-1)"
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin
vault login   # if needed
export POOL_SSH_PRIVATE_KEY="$(vault kv get -format=json -mount=secrets minds/dev/pool-ssh \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["data"]["POOL_SSH_PRIVATE_KEY"])')"
DSN=$(python3 -c "import tomllib;print(tomllib.load(open('$HOME/.minds-dev-josh-1/secrets.toml','rb'))['secrets']['NEON_HOST_POOL_DSN'])")
# sanity: box is registered, ready, 0 used
uv run mngr imbue_cloud admin server list --database-url "$DSN"
```

Step 1 — sync this branch's mngr onto the box (codified D3, or interim rsync):
```bash
# interim (until admin server sync-mngr exists):
rsync -a --delete --filter=':- .gitignore' --exclude=.git --exclude=uv.lock \
  ./ limahost@15.204.140.221:/home/limahost/mngr/
ssh limahost@15.204.140.221 'cd ~/mngr && ~/.local/bin/uv sync --all-packages'
```

Step 2 — confirm placement + the attributes the slice will advertise:
```bash
uv run mngr imbue_cloud admin server allocate-slice --database-url "$DSN"
# note the printed cpus (e.g. {"memory_gb":8,"cpus":3}) — use it as <CPUS> below
```

Step 3 — bake the slice on the box (codified D2; interim = run on the box as limahost):
```bash
# On the box, in the FCT worktree, with box env exported (D5):
#   ANTHROPIC_API_KEY=<minted litellm key>
#   ANTHROPIC_BASE_URL=https://minds-dev-dev-josh-1--llm-dev-proxy.modal.run/
#   MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL=https://minds-dev-dev-josh-1--rsc-dev-api.modal.run/
# and [providers.imbue_cloud_slice].box_public_address=15.204.140.221 in .mngr/settings.toml
uv run mngr create system-services@slice-test.imbue_cloud_slice \
  --new-host --no-connect --idle-mode disabled \
  --template main --template slice \
  --label workspace=system-services --label user_created=true --label is_primary=true \
  --label 'pool_attributes={"memory_gb":8,"cpus":<CPUS>}' \
  --host-env MNGR_HOST_DIR=/mngr --pass-host-env MNGR_PREFIX
# then: install pool key on VM root + container, destroy bootstrap chat agent + rm sentinel,
#       and insert the slice row (these are the codified bake's job — D2 steps c/d/e).
```

Step 4 — verify the row (laptop):
```bash
uv run mngr imbue_cloud admin server list --database-url "$DSN"   # 1/8 used
psql "$DSN" -c "SELECT id,status,backend_kind,ssh_port,container_ssh_port,attributes,lima_instance_name \
  FROM pool_hosts WHERE backend_kind='slice';"
```

Step 5 — fast-path lease as a workspace (laptop, dev-josh-1 authed):
```bash
uv run mngr create slice-ws@.imbue_cloud_josh-imbue-com \
  -b fast_mode=require -b memory_gb=8 -b cpus=<CPUS>
# (or create from the minds desktop app: `just minds-start`)
```

Step 6 — verify it works, then exercise release:
```bash
uv run mngr exec slice-ws "echo works && hostname && nproc"     # primary check (container conn)
uv run mngr list                                                # state check — accurate ONLY after D1 fix
uv run mngr connect slice-ws                                    # optional: chat agent runs
uv run mngr destroy slice-ws --force                            # -> connector slice-release fork
# confirm teardown:
ssh limahost@15.204.140.221 'limactl list'                      # the slice VM is gone
uv run mngr imbue_cloud admin server list --database-url "$DSN" # back to 0/8 used
```

## Cleanup
- The box bills ~$93/mo. Tear down any slices baked during testing (release, or `limactl delete`
  on the box). Decide whether to keep the box (real pool capacity) or cancel it (`admin server
  destroy` is not implemented; cancel via OVH).
