# OVH bare-metal slices — implementation handoff

Status snapshot for the next agent. The feature (carve VPS-like "slices" = lima/QEMU
VMs on rented OVH bare-metal, as an alternative to ordering OVH VPSes) is **built and
unit/VM-verified**; the **live end-to-end validation on the real box (FCT bake + fast-path
workspace lease) is the remaining work.**

- **Spec:** `blueprint/ovh-baremetal-slices/plan-ovh-baremetal-slices.md` (read this first — it has the full design, the Q&A decisions, and the pricing gotcha).
- **Branch:** `mngr/ovh-exploration`  •  **PR:** #2135 (draft).
- **Base note:** `origin/main` was merged into the branch; local `main` is stale/locked in another worktree, so always diff/review against **`origin/main`**, not `main`.

---

## What's implemented (all committed + pushed)

All in `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/` unless noted. Every module has a `*_test.py`.

| Area | Files | Verified |
|---|---|---|
| OVH order pricing (base + addon deltas + setup; the "gotcha") | `pricing.py` | unit |
| Slice data model + pure logic (slot count, slice vCPU/overcommit, RAID choice, lima naming, port allocation, lifecycle, placement) | `bare_metal.py`, `data_types.py` (`BareMetalServer`, `BareMetalServerCapacity`), `primitives.py` (`BackendKind`, `BareMetalServerStatus`, `BareMetalServerDbId`, status/kind constants), `errors.py` | unit |
| Lima slice VM (VPS-parity YAML: root SSH, btrfs data disk at host_dir, docker, 2 forwarded ports) + client | `lima_slice.py`, `lima_slice_client.py` (`LimaSliceVpsClient`) | **real lima VM** (docker installed, `/mngr` is btrfs, root sshd on :2200, clean teardown) |
| `SliceVpsDockerProvider` (subclass of `VpsDockerProvider`; overrides only per-host-port + btrfs-subvolume seams; reuses `create_host_on_existing_vps` unchanged) | `slice_provider.py` | **real VM end-to-end** (`test_slice_provider.py`, release-marked, ~151s: VM→btrfs subvolume→container bake→exec over forwarded port→destroy) |
| Slice provider backend registration (`imbue_cloud_slice`) | `slice_provider.py` (`SliceVpsDockerProviderBackend`), `slice_plugin.py`, `pyproject.toml` 2nd entry point | unit |
| Neon DB layer (insert/fetch servers, count slices, capacity, insert slice pool_hosts row) | `bare_metal_db.py` | unit (SQL column-consistency + round-trip) |
| Admin CLI `mngr imbue_cloud admin server` (prep / list / register / allocate-slice / set-status) | `cli/server.py`, wired in `cli/root.py` | unit + **prep/register/list ran live** |
| Box prep script builder (qemu+lima+uv, lima service user) | `bare_metal_prep.py` | unit + **ran live on the box** |
| Connector release fork on `backend_kind` (slice → SSH box + `limactl delete`; VPS → OVH cancel) + sweep | `apps/remote_service_connector/imbue/remote_service_connector/app.py`, `testing.py` (fake DB) | unit (`build_slice_teardown_commands`, branch + columns); **endpoint-level test deferred** |
| Migrations | `apps/remote_service_connector/migrations/008_bare_metal_servers.sql`, `009_pool_host_slice_columns.sql` | applied to dev-josh-1 Neon |
| Deps | `pyproject.toml`: `imbue-mngr-lima`, `concurrency-group` | — |

**Tests:** `mngr_imbue_cloud` 224 passing; `remote_service_connector` 265 passing; pyright clean; ratchets green (yaml ratchet bumped 0→18 with justification — lima config is YAML-native). CI on PR #2135 was re-running after a CLI-docs regen fix (see "CI" below).

### Key design decisions baked into the code (from the spec Q&A)
- Slices are ordinary `pool_hosts` rows + a `backend_kind` discriminator (`ovh_vps` | `slice`) + FK `bare_metal_server_id` + `lima_instance_name` + `lima_disk_name`. Leasing is unchanged; the connector branches on `backend_kind` only at release.
- One shared pool; a lease takes whichever `available` row matches first (VPS or slice), transparent to minds.
- Slot count = `floor(RAM_GB / 8)`; each slice advertises `memory_gb=8`, allocated slightly under (`SLICE_VM_MEMORY_MIB=7680`); `cpus` = `floor(threads * overcommit / slots)` (mild CPU overcommit, default ratio 1.5).
- Networking: lima default per-VM user-mode NAT (no VM↔VM), two `0.0.0.0` port-forwards per slice — **guest 2200** → VM root sshd (lima reserves guest 22, so sshd also listens on 2200), **guest 2222** → inner container sshd. All other guest ports suppressed.
- btrfs: lima `additionalDisk` mounted at `btrfs_mount_path` (`/mngr-btrfs`); the provider creates the per-host subvolume there (no loopback). Per-host-hex subvolume layout kept for parity.
- On release: slice → destroy the lima VM + disk (free the slot); VPS → cancel OVH (unchanged). Manual-only server destroy; no `--force`; no auto failure recovery (app-level backups are the recovery path).
- Bake model (updated 2026-06-14, carve-over-SSH): a slice is provisioned + baked exactly like an OVH VPS, **from the operator's machine** -- NOT on the box. `LimaSliceVpsClient` drives `limactl` over SSH on the box to carve a bare Debian VM (the "OS reinstall" equivalent), then the shared `VpsDockerProvider` reaches the VM's box-forwarded ports to build the container. `get_instance_ip` returns the box's external address. The earlier "run the whole bake on the box" model (ship the monorepo + FCT, `uv sync`, `git init`, run `mngr create` there) is **gone** -- see "Carve-over-SSH refactor" below.

---

## The real box we set up

- **OVH order:** `8144904` (RISE-2, eco line), placed 2026-06-13, month-to-month (~$80 setup + $93/mo).
- **serviceName:** `ns1012536.ip-15-204-140.us`  •  **public IP: `15.204.140.221`**  •  **datacenter: `vin` (Vint Hill, VA)**.
- **HW:** Intel Xeon-E 2388G **8c/16t**, **62 GiB RAM**, **2× 477 GB NVMe in active RAID1** (md2/md3), `/dev/kvm` present.
- **OS:** Debian 12 installed by us via OVH **reinstall API** (`POST /dedicated/server/{s}/reinstall`, operatingSystem `debian12_64`, inline `customizations.sshKey` = the pool ed25519 pubkey). Default partitioning gave RAID1. (The legacy `/install/start` path errored — use `/reinstall`.)
- **Prepped via `mngr imbue_cloud admin server prep --server-address 15.204.140.221`:** qemu-system-x86 + qemu-utils + btrfs-progs installed, **limactl 2.1.2** installed to /usr/local, non-root user **`limahost`** created (in `kvm` group, pool key authorized), `uv` installed for limahost.
- **Registered in dev-josh-1 Neon** via `admin server register`: row id **`679c46f7-fb1b-4d13-8852-6ae9e7b254cd`**, plan `24rise02-v1-us`, region `vin`, `15.204.140.221`, ram_gb 64, 8c/16t, RAID1, lima_service_user `limahost`, status `ready`, **8 slots, 0 used**.
- **SSH:** as `debian` (cloud default user; root login disabled) or `limahost`, with the pool key. Box ssh host key already pinned to local known_hosts.

---

## Access / credentials (all via HCP Vault; you must `vault login` first)

- `export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin`
- **OVH creds:** `secrets/minds/dev/ovh` → `OVH_APPLICATION_KEY/SECRET`, `OVH_CONSUMER_KEY` (endpoint `ovh-us`). Used with the official `ovh` python SDK.
- **Pool SSH key:** `secrets/minds/dev/pool-ssh` → `POOL_SSH_PRIVATE_KEY` (ed25519). This is what SSHes the box (authorized for `debian` and `limahost`) and what the connector uses to tear down slices.
- **Neon (the DB the dev-josh-1 connector reads):** `~/.minds-dev-josh-1/secrets.toml` → `secrets.NEON_HOST_POOL_DSN`. **Not** `secrets/minds/dev/neon` (that's a different/tier project). Migrations 008/009 already applied to the dev-josh-1 DB.
- **imbue_cloud auth (for the fast-path lease):** `josh@imbue.com` is ALREADY signed in for the dev-josh-1 minds env. To use it from the CLI, activate the env:
  ```
  eval "$(uv run minds env activate dev-josh-1)"
  # exports MNGR_HOST_DIR=~/.minds-dev-josh-1/mngr, MINDS_CLIENT_CONFIG_PATH=~/.minds-dev-josh-1/client.toml, MNGR_PREFIX=minds-dev-josh-1-, MINDS_ROOT_NAME=minds-dev-josh-1
  ```
  Provider instance is **`imbue_cloud_josh-imbue-com`** (account josh@imbue.com); session is at `~/.minds-dev-josh-1/mngr/profiles/3bdad98064fd489389b1cc868da5045f/providers/imbue_cloud/sessions/`. Connector URL: `https://minds-dev-dev-josh-1--rsc-dev-api.modal.run/`; LiteLLM proxy: `https://minds-dev-dev-josh-1--llm-dev-proxy.modal.run/`.
- This env has leased imbue_cloud hosts before (there are `remote-*` agents under `~/.minds-dev-josh-1/mngr/.../preserved_sessions/`), so the lease path works in this env.

---

## Live end-to-end validation — DONE (2026-06-13, both fast + slow paths)

Validated live against the real box (`15.204.140.221`, dev-josh-1 env, josh@imbue.com).
See `blueprint/slice-fast-path-fixes/plan-slice-fast-path-fixes.md` for the design of the
fixes this required.

- **`admin server allocate-slice` actually bakes** (codified): syncs this branch's mngr +
  the FCT workspace to the box once, bakes the slice(s) in parallel (`--count`, `--dry-run`),
  authorizes the pool key on the VM root + container, tears down the bootstrap chat agent +
  sentinel, and inserts an `available` slice `pool_hosts` row. Confirmed: VM carved, FCT image
  built inside it, row written with the box-forwarded ports + lima names + `{cpus:3,memory_gb:8}`.
- **Fast path** (`-b fast_mode=require`): leased the slice, connector injected the user key on
  both forwarded ports, adopted the pre-baked `system-services` agent; `mngr list` shows HOST
  STATE RUNNING (D1 fix), `mngr exec` works, `/mngr` is btrfs.
- **Slow path** (`-b fast_mode=prevent`): leased + tore down the baked container + rebuilt it on
  the slice VM (slice-aware rebuild: publish guest 2222 → connect box-forwarded 22001); `mngr
  exec` works, `nproc=3` (matches advertised cpus), `/mngr` btrfs, FCT workspace present.
- **Release teardown**: the connector's exact `limactl delete` / `disk delete` commands were run
  against the box and destroyed the VM + disk cleanly (slot freed).

### Known gap (not a code bug): connector deployment
The dev-josh-1 connector is deployed from `20260608T180019Z`, which **predates this branch's
slice-release fork**. So the full `release_host` *endpoint* (which branches on `backend_kind` →
`clean_up_slice_on_box`) was NOT exercised end-to-end there: `mngr destroy` reports success but
the old connector leaves the slice VM + row behind. Leasing works because the old connector's
lease path is unchanged. The slice-release **code** is on this branch + unit-tested, and the
**teardown commands it issues were verified live on the box**. To close this fully: redeploy the
connector for the tier (`minds env deploy`), then re-run `mngr destroy` on a leased slice and
confirm the VM is gone. (Deploying is outward-facing; left for an operator to trigger.)

---

## Original remaining-work notes (now superseded by the validation above)

Goal (user's words): "allocate a real slice on it, bake an available host for that slice, then create a new workspace that uses that fast-path imbue_cloud host and make sure it actually works." The user wants the **FCT** bake (a real minds workspace), not a plain image.

### Step 1 — get this branch's mngr onto the box
`SliceVpsDockerProvider` must run on the box (limactl is local there). rsync this worktree to `limahost@15.204.140.221` (exclude `.venv`, `.git`, `node_modules`, `.test_output`) and `uv sync --all-packages` there. **This should be codified** (e.g. an `admin server sync-mngr` command or fold into a `bake-slice` command — do NOT raw-dog ad-hoc SSH; the user explicitly called that out). Mirror how `admin pool create` / the minds vendor-sync does it.

### Step 2 — FCT `slice` create-template
`admin pool create` bakes via `mngr create system-services@<host>.ovh --template main --template ovh` from the FCT workspace (`~/project/forever-claude-template`). Add an analogous **`slice` create-template** to FCT's `.mngr/settings.toml` (model it on the existing `ovh` / `lima` / `imbue_cloud` templates) that sets `--provider imbue_cloud_slice` + `--host-env MNGR_HOST_DIR=/mngr` + whatever the slice provider config needs (`box_public_address=15.204.140.221`, `lima_service_user=limahost`, etc.). Configure a `[providers.imbue_cloud_slice]` instance (on the box) with `box_public_address` set to the box's public IP so the recorded row + the outer/container SSH target the box externally.

### Step 3 — bake a slice on the box
Run the FCT bake on the box (as `limahost`): `mngr create system-services@slice-test.imbue_cloud_slice --template main --template slice ...`. This carves a lima VM → vps_docker container → FCT bootstrap (system-services agent). **This exercises inference via the dev-josh-1 LiteLLM proxy** — make sure the box/agent can reach it. Capture the baked `agent_id` / `host_id` and the two forwarded host ports.

### Step 4 — insert the available slice pool_hosts row
Use `bare_metal_db.insert_slice_pool_host` / `build_slice_pool_host_insert_values` (or finish wiring `admin server allocate-slice` to actually do steps 3+4, which currently only reports placement + attributes). Row needs: `backend_kind='slice'`, `bare_metal_server_id=679c46f7-...`, `vps_address=15.204.140.221`, `ssh_port`=VM-sshd forwarded port, `container_ssh_port`=container forwarded port, `lima_instance_name`, `lima_disk_name`, `attributes` `{memory_gb:8, cpus:<derived>}`, `region=vin`, status `available`.

### Step 5 — fast-path lease as a workspace
With the dev-josh-1 env activated (josh@imbue.com), create a workspace that leases the slice via the fast path:
`mngr create <ws>@.imbue_cloud_josh-imbue-com -b fast_mode=require -b memory_gb=8 -b cpus=<the slice's advertised cpus>` (or create the workspace from the minds desktop app, which is authed). It should pick the slice row, adopt the baked system-services agent, and come up as a working host.

### Step 6 — verify it actually works
Confirm: the leased host is reachable (`mngr exec <ws> "echo works"`), the workspace/chat agent runs, and the attributes match. Then exercise **release**: destroy the workspace and confirm the connector's slice branch SSHes the box and `limactl delete`s the VM + disk (slot freed) — this validates the connector release fork end-to-end (the part with only unit coverage today).

---

## Things to check / verify (and known gaps)

- **CI on PR #2135:** earlier failures were (a) `test_cli_docs_are_up_to_date` — FIXED by regenerating `libs/mngr/docs/commands/secondary/imbue_cloud.md` (re-run after any new CLI command); (b) two `libs/mngr` core tests timing out at 10s (`test_extras_no_args_shows_status`, `test_exec_command_on_agent_uses_custom_cwd`) — **CI-load flakiness, not ours** (pass locally in ~5s). Confirm the latest run is green.
- **`admin server allocate-slice` is currently report-only** — it picks the server + prints slice attributes but does NOT bake or insert the row yet. Wire it (or a new `bake-slice`) to do steps 3–4, codified.
- **Live-OVH `admin server order` / `install` / `reconcile` commands are NOT implemented.** The OS install was done ad-hoc via the OVH `/reinstall` API (see box section). Codify these (order via the eco cart flow — already proven for order 8144904; install via `/reinstall`; reconcile = poll the task + advance `bare_metal_servers.status`).
- **Connector slice-release endpoint test is deferred** — `build_slice_teardown_commands` is unit-tested and the branch/columns are covered, but the `release_host`→`clean_up_slice_on_box` path has no integration test (needs a box or a `LimaOps` DI seam). Validate it live in step 6; consider adding the seam + a fake-backed test.
- **Does the dev-josh-1 connector reach the box for teardown?** The connector runs on Modal; it SSHes `15.204.140.221` as `limahost` with `POOL_SSH_PRIVATE_KEY`. Confirm Modal egress can reach the box's :22 and that the connector env has `POOL_SSH_PRIVATE_KEY` for this env.
- **Lima socket-path length:** the e2e test pins `LIMA_HOME`/`HOME` short because pytest's deep tmp HOME blew the 108-char UNIX socket limit. On the real box (`limahost` home is short) this is a non-issue, but watch for it if any path gets deep.
- **Slot accounting** treats a `removing` row as freeing its slot immediately (before the VM is actually gone) — intentional optimistic placement; fine for now.
- **RAID confirmed** on the box (md1 RAID1). If you order more boxes, the install must keep RAID1 (default on 2-disk) or RAID10 (4+).
- **Cost/cleanup:** the box bills monthly (~$93/mo). When done validating, decide whether to keep it (it's real pool capacity) or cancel (`admin server destroy` is not implemented yet — cancel via OVH `PUT /vps`-style serviceInfos `deleteAtExpiration`, or the dedicated-server equivalent). Slices baked during testing should be torn down (release, or `limactl delete` on the box) to avoid leaking VMs.
- **Unfixed sub-threshold review notes** are logged under `.reviewer/outputs/autofix/unfixed/*.jsonl` (NITPICKs: `_as_datetime` defensive coercion, disk-name suffix duplication, the deferred connector release test). Nothing blocking.

## Quick commands
```
# activate the authed dev env (josh@imbue.com)
eval "$(uv run minds env activate dev-josh-1)"
# pool DSN for admin server commands
DSN=$(python3 -c "import tomllib;print(tomllib.load(open('$HOME/.minds-dev-josh-1/secrets.toml','rb'))['secrets']['NEON_HOST_POOL_DSN'])")
uv run mngr imbue_cloud admin server list --database-url "$DSN"
# pool key (for SSH to the box / prep)
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin
export POOL_SSH_PRIVATE_KEY="$(vault kv get -format=json -mount=secrets minds/dev/pool-ssh | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["data"]["POOL_SSH_PRIVATE_KEY"])')"
```

### Post-review hardening (2026-06-14) — all validated live on the box
- Per-slice sizing is computed per server (memory-per-slice + CPU-overcommit + usable disk stored at `register`); no hardcoded vCPU/RAM/disk defaults. `allocate-slice` is one server per invocation. `--cpu-overcommit` defaults to 2.0.
- Disk no longer overcommits: each slice VM = a fixed 32 GiB boot disk (OS + Docker) + a btrfs data disk, summing to the per-slice budget `(usable_disk - 20) / slots`. (Previously the lima boot disk defaulted to 100 GiB, unaccounted.) Verified on the box: `disk: 32GiB` + data `23GiB` = 55 = budget for the RISE-2 (467 GiB / 8 slots).
- Slices record the box's real region; the slow-path rebuild pins the outer host key (no certified-data warning) and is slice-aware (forwarded ports).
- autofix found + fixed 3 MAJOR bugs: orphaned-VM rollback when a post-bake step or the create-JSON parse fails, and disjoint per-bake port windows so concurrent `--count N` bakes don't collide.
- Connector release endpoint still needs a redeploy of the dev-josh-1 connector to exercise the slice-release fork live (the deployed connector predates it); teardown commands themselves are verified on the box.

### Carve-over-SSH refactor (2026-06-14)
The slice bake no longer runs on the box. It used to ship the monorepo + FCT to the box, `uv sync`, `git init`, and run `mngr create` there -- which was a near-verbatim duplicate of the OVH `admin pool create` bake (FCT templates, `system-services`, chat-agent teardown, sentinel, pool insert) living a second time inside the slice tooling.

Now a slice is provisioned + baked **exactly like an OVH VPS, from the operator's machine**:
- `LimaSliceVpsClient` drives `limactl` over SSH on the box (render YAML, ship it, carve/destroy/list remotely). Carving a bare Debian VM = the OVH "reinstall the OS" equivalent. Free box ports are probed over SSH (`ss -Htln`).
- The slice provider's `create_host` carves, then runs the shared `vps_docker` container bake against the VM's box-forwarded ports (laptop -> `box:vm_port`) -- so the whole bake is off-box, identical in shape to OVH.
- The FCT bake itself (create with the FCT templates + `--format json`, stop, sshd hardening, chat-agent teardown) now lives once in provider-generic `pool_bake.py`, shared by `cli/admin.py` (OVH) and `cli/server.py` (slices). OVH keeps ufw + management-key install + recycle + VPS cancel + OVH insert; slices keep server selection + sizing + carve config + slice insert + orphan-VM rollback.
- `slice_bake.py` and the box-side rsync/`uv sync`/`git init` machinery are **deleted**. `allocate-slice` now just vendors mngr into the FCT workspace once (`sync_mngr_into_template`, local) and parallel-bakes `--count N` from here. `mngr create --format json` gained `ssh_key_path` so the shared bake resolves all host details from one create call.
- The box must expose the slice port range externally (the connector already needs this for lease/release), which is also what lets the laptop-driven bake reach `box:vm_port`.

**Live validation (2026-06-14, real box 15.204.140.221).** Validated end-to-end across two successful bakes + direct live-container checks:
- Carve over SSH: lima VM created with the exact computed sizing (4 vCPU, 7.5 GiB, **32 GiB boot + 23 GiB data**).
- Container bake (Docker) over the box-forwarded VM port, agent created, `mngr create --format json` emitted all fields (`ssh_user`/`ssh_host`/`ssh_port`/`ssh_key_path`/`outer_ssh_port`), slice `pool_hosts` row inserted (`succeeded: 1`).
- **btrfs disk model confirmed on the live VM**: `vda`=32 GiB ext4 boot, `vdb`=23 GiB **btrfs** data disk mounted at the btrfs mount path, and the Docker host volume is a **btrfs subvolume** (`subvolid=256`, `subvol=/<hex>`) on it -- the real per-host subvolume layout, **no loopback image**. 32+23 = 55 = budget.
- Post-create container SSH (the fixed `_slice_run_in_container` transport): connect + `sshd`-harden + sentinel-detect + `cd /mngr/code && uv run mngr destroy <host> --force` + sentinel-rm all succeed against the live container.
- Orphan-VM rollback: a `limactl start` failure (see infra note below) correctly destroyed the VM + data disk; no leaked slot.

Two bugs were found *by* this live testing and fixed (commit `d5528c281`): (1) `_slice_run_in_container` used the operator's shared `~/.ssh/known_hosts` with `accept-new`, so a reused box port with a new container host key failed strict checking and broke teardown -- now `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null`; (2) the sentinel-wait silently treated *any* non-zero exit as "no chat agent", so a broken transport shipped a pool host with a stale chat agent -- now only the `timeout` exit (124) skips, every other exit raises (and the slice caller rolls back).

**Known infra flakiness (NOT the refactor):** lima revalidates a digest-less cached image with a last-modified `HEAD` to `cloud.debian.org` before boot; that mirror intermittently TLS-times-out from this box, and lima's fallback then fatally fails to materialize the (otherwise complete) cached image (`open .../image: no such file or directory`). When the mirror is reachable the bake succeeds; when it flaps, `limactl start` fails and the VM is rolled back. **Follow-up:** pin the lima image digest (or pre-stage the image on the box at a stable `file://` path) so repeated slice bakes don't depend on `cloud.debian.org` reachability per bake. This is `mngr_lima`/box-prep territory, out of scope for the carve-over-SSH refactor.
