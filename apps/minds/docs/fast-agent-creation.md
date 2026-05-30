# Fast agent creation - exploration

> Status: exploration / hypothesis. Not a design doc.

On a brand-new Mac, "Create agent" -> "I can chat" takes ~10 minutes in LIMA mode. This document maps where that time goes, what we could bundle / prewarm / parallelize, and proposes a benchmarking plan to put numbers on the hypotheses.

Cross-references:
- `apps/minds/imbue/minds/desktop_client/agent_creator.py:1060` -- `_create_agent_background`, the state machine.
- `libs/mngr_lima/imbue/mngr_lima/instance.py:384` -- `LimaProviderInstance.create_host` (VM boot).
- `libs/mngr_lima/imbue/mngr_lima/lima_yaml.py:149` -- in-VM provisioning script (apt installs).
- `forever-claude-template/.mngr/settings.toml` `[create_templates.lima]` -- the heavy `extra_provision_command` list (uv tool installs, npm ci, playwright, ...).
- `apps/minds/scripts/build.js:379` + `download-binaries.js` -- what gets bundled in `Resources/`.
- `apps/minds/scripts/first-message-verify.sh` -- existing CI benchmark harness.

## 1. Where does the 10 minutes go?

The state machine reports four phases. CREATING_WORKSPACE is by far the longest and is itself a chain of sub-steps that the current logs don't break down.

| Phase | Sub-step | Est. cold | Est. warm | Per-agent or one-time | Confidence |
|---|---|---|---|---|---|
| CLONING_REPO | `git clone FCT` (~hundreds of MB) | 20-60s | 5-15s | per-agent | medium |
| CHECKING_OUT_BRANCH | `git checkout <branch>` | 1-3s | 1-3s | per-agent | high |
| PROVISIONING_AI | mint LiteLLM key (IMBUE_CLOUD only) | 1-3s | 1-3s | per-agent | high |
| CREATING_WORKSPACE | `limactl disk create` btrfs disk | 1-3s | 1-3s | per-agent | medium |
| CREATING_WORKSPACE | download Ubuntu 24 cloudimg (~600MB) | 60-180s | 0s | one-time | high |
| CREATING_WORKSPACE | QEMU boot + cloud-init | 30-90s | 30-90s | per-agent | medium |
| CREATING_WORKSPACE | `cloud-init status --wait` | 5-30s | 5-30s | per-agent | high |
| CREATING_WORKSPACE | `wait_for_sshd` | 1-10s | 1-10s | per-agent | high |
| CREATING_WORKSPACE | in-VM `apt-get update && install` (tmux/git/jq/rsync/curl/xxd/openssh-server/ca-certs) | 20-60s | 20-60s | per-agent (in fresh VM) | medium |
| CREATING_WORKSPACE | host->VM file transfer / rsync of FCT checkout | 10-30s | 10-30s | per-agent | medium |
| CREATING_WORKSPACE | in-VM `apt-get install build-essential fd-find git-lfs ripgrep sqlite3 unison` | 30-90s | 30-90s | per-agent | medium |
| CREATING_WORKSPACE | nodesource setup + apt install nodejs | 15-45s | 15-45s | per-agent | medium |
| CREATING_WORKSPACE | `npm install -g latchkey@2.10.1` | 15-30s | 15-30s | per-agent | medium |
| CREATING_WORKSPACE | uv install script + `uv tool install -e vendor/mngr` (pulls wheels) | 60-180s | 60-180s | per-agent (in fresh VM, hits PyPI for ~100 wheels) | low |
| CREATING_WORKSPACE | `uv tool install -e apps/system_interface --with-editable ...` | 30-90s | 30-90s | per-agent | low |
| CREATING_WORKSPACE | `uv sync --all-packages` | 30-60s | 30-60s | per-agent | low |
| CREATING_WORKSPACE | `apps/system_interface/frontend: npm ci && npm run build` | 60-180s | 60-180s | per-agent | low |
| CREATING_WORKSPACE | `playwright install --with-deps chromium` | 30-60s | 30-60s | per-agent | low |
| CREATING_WORKSPACE | `mngr create` core: tmux session, file transfers, agent provision | 20-60s | 20-60s | per-agent | medium |
| WAITING_FOR_READY | `system_interface` HTTP 200 (frontend already built; just bind) | 5-30s | 5-30s | per-agent | high |
| (post-DONE) | first-message: claude cold-start in TUI, auth, generate | 60-300s | 60-300s | per-agent | medium |

Rough math: cold first run ~10-12 min, warm subsequent run ~8-10 min (Ubuntu image cached, but every other in-VM step repeats from scratch because each agent gets a fresh VM).

What we genuinely don't know yet and need to measure:
1. **Cloudimg download rate** -- 600MB at residential bandwidth swings from 30s to 5min. Needs measurement.
2. **Per-step in-VM seconds** -- the lima `extra_provision_command` list runs as one opaque block; we have no per-line timestamps. Need instrumentation.
3. **Where the wheels go** -- `uv tool install -e vendor/mngr` resolves a lot of transitive deps; we don't know how many MB/s, how many round-trips, or whether `uv` caches across reinstalls.
4. **`npm run build` cost** -- depends on the system_interface frontend bundle size at the time of the build.

## 2. What can we bundle into `/Applications/Minds.app/Contents/Resources/`?

Already bundled (per `download-binaries.js` and `build.js`):

- `uv/uv` (~30MB)
- `git/` + libexec/git-core (~50MB on macOS)
- `lima/bin/limactl` + `share/lima/` + `qemu-system-aarch64` (~80MB after stripping Darwin guest-agents)
- `restic` (~25MB)
- `latchkey` (Node CLI driven via Electron-as-Node, ~5MB unpacked)
- `pyproject/` + `wheels/` for the desktop client (~50MB)

Not bundled today:

- Ubuntu cloudimg
- FCT clone
- In-VM Python deps (every wheel mngr / system_interface needs)
- In-VM `node_modules` for system_interface frontend
- Pre-warmed venv state

### Bundling candidates

| Candidate | Ship size delta | First-run savings | Per-agent savings | Implementation cost | Risk |
|---|---|---|---|---|---|
| **Pre-built Ubuntu qcow2 in `Resources/lima/images/`** (raw Ubuntu, no provisioning) | +400-600MB | 60-180s cloudimg download | 0 (we already cache after first VM) | low: `images:` in lima.yaml can point at `file://` URL; just bake a `.img` into the bundle | re-signing a multi-hundred-MB blob is slow; bundle invalidates when Lima or Ubuntu version moves |
| **Pre-provisioned qcow2** (Ubuntu + uv + nodejs + latchkey + apt deps already installed) | +1-2GB | n/a | 90-300s (skip apt-update, apt-install, nodesource, uv install, npm -g latchkey) | medium: build a "golden" image in CI per release; bundle as `Resources/lima/images/minds-golden-<ver>.qcow2` | image must match FCT template's apt list exactly; version drift across FCT branches becomes a footgun; bundle size hurts app download |
| **Fully baked qcow2** (above + `vendor/mngr` uv tools + system_interface built frontend + wheels) | +2-4GB | n/a | 4-7 min (skip the entire `extra_provision_command` block) | high: bake produces a per-FCT-ref snapshot, has to be regenerated whenever FCT changes; lima `additionalDisks` btrfs path also needs the bake re-applied | huge bundle; tight coupling between minds release and FCT release; risks the agent shipping with stale system_interface |
| **Bundled FCT clone** in `Resources/template/forever-claude-template/`, used as `file://` for `git clone` | +50-100MB | 20-60s clone | 5-15s per-agent | low: change the default `repo_source` to the bundled path; user override still works | bundle goes stale across FCT updates; mitigated by app auto-update cadence |
| **Pre-warmed host-side `uv` cache** in `Resources/uv/cache/` for the bundled desktop venv | +100-200MB | only matters for the Electron host venv, not in-VM | n/a | low | doesn't actually move the needle on agent creation (the slow uv runs are *inside* the VM) |
| **Lima VM template snapshot** (single pre-booted VM image, agents are clones) | +1-2GB | n/a | 60-120s (skip QEMU boot + cloud-init) | high: Lima doesn't officially support VM cloning; would need shell out to qemu-img and bespoke wiring | brittle; not on Lima's supported path; security-isolation per agent suffers |

Strong recommendation: ship the raw cloudimg first (low-risk, immediate win), then layer pre-provisioned image on top once we have a per-release bake pipeline.

## 3. What can the app warm in the background on first launch?

| Prewarm | Trigger | Expected win | UX risk | Bandwidth burn risk |
|---|---|---|---|---|
| Pull Ubuntu cloudimg in the background as soon as the Electron shell is up | first app launch | 60-180s shaved from first agent | none (silent) | high if user is on cellular; gate on `navigator.connection` or a one-time "OK to prefetch?" prompt |
| `git clone forever-claude-template` into `~/Library/Application Support/Minds/templates/` | first app launch | 20-60s shaved from first agent | none | low (~100MB) |
| Pre-create a single hot lima VM, idle, ready to bind to a creation request | first app launch, after Ubuntu image lands | 90-300s shaved from every "first agent on this Mac" (everything before in-VM `apt install` could be skipped) | medium: idle VM consumes RAM (~1-2GB) and CPU spikes during boot; needs user opt-in; pre-warm also needs to happen on lima version bumps | n/a (one-time) |
| In hot VM, run the in-VM `extra_provision_command` list once with placeholder values, then snapshot | post-hot-VM-boot, when user is idle | 4-7 min shaved from every subsequent agent on this Mac | high: lima doesn't support clean snapshots, so "snapshot" is really "keep an extra qcow2 around"; also burns ~1-2GB disk | low |
| Pre-fetch the host venv `uv sync` artifacts | first app launch | seconds-only (host venv is already quick) | none | low |

The "single hot VM" idea is the biggest individual lever after bundling, but introduces stateful background machinery. A reasonable middle ground: do it only after the user explicitly opts in ("Get the next agent ready in the background") with a settings toggle.

Bandwidth-burn mitigation: defer all auto-prefetch behind a one-time first-run dialog ("Speed up the first agent? Downloads ~1GB.") so we never silently burn user quota.

## 4. Parallelization opportunities

Today the flow is strictly linear -- CLONING_REPO blocks CREATING_WORKSPACE, and inside `LimaProviderInstance.create_host` every step is sequential. Concurrency opportunities:

1. **Clone FCT in parallel with lima boot.** Today `clone_git_repo` runs first, then `mngr create` shells out which then `limactl start`s. We could kick off the lima VM start before/concurrently with the host-side clone -- saves whichever leg is shorter (10-30s).
2. **Apt installs in parallel.** The Lima provisioning script (`lima_yaml.py:158`) and the FCT `extra_provision_command` block both run apt-installs. They run serially because they're two separate phases (cloud-init vs `mngr create`'s post-provision). Combining them into one `apt-get install -y <full list>` in cloud-init would save a second `apt-get update` (10-20s) and serialize fewer package-resolver passes.
3. **`uv tool install` vs `npm ci` inside the VM.** These touch disjoint trees and disjoint package managers. Today they're `&&`-chained. A `&` + `wait` pair, or two parallel ssh sessions, would overlap most of their cost. Likely 30-60s saved.
4. **Playwright Chromium download vs `npm run build`.** Same reasoning -- both pull large blobs from different CDNs.
5. **`mngr create` agent-side provisioning vs system_interface frontend build.** Today `system_interface` builds *as part of* extra_provision_command, before agent provisioning runs. Reordering so the agent (claude TUI) and the frontend build start together could overlap 60-90s.
6. **Skip in-VM `apt-get update` when we know it's a fresh Ubuntu image.** The cloudimg already has fresh apt indices; only needed for the second-pass installs.

Total feasible parallelism savings if all five land: ~3-5 min.

## 5. Benchmarking plan

The existing `minds-launch-to-msg.yml` workflow on the self-hosted Mac runner is the natural benchmark. It already emits `[first-msg]   status=X  (Ns remaining)` lines. To turn this into a usable performance harness:

### 5a. Per-stage timings as a CI artifact

- Have `first-message-verify.sh` write a structured JSON file `/tmp/first-message-timings.json` of the form `{"clone": 12.4, "checkout": 1.2, "creating_workspace": 412.7, "waiting_for_ready": 18.3, "first_message_rtt": 78.4}`. The transitions are already detected at lines 132-136; instead of just logging, also `date +%s` on each transition and store the delta.
- Upload via `actions/upload-artifact@v4` named `first-message-timings-<run_id>.json`.
- This is enough to chart stage-level latency over time without any new infra.

### 5b. Break `CREATING_WORKSPACE` into sub-stages

- Inside the FCT `extra_provision_command` block (settings.toml), wrap each step in a `time` invocation that logs to a file in the VM, e.g. `{ time ...; } 2>>/tmp/provision-times.log`. Read it back via `limactl shell <vm> cat /tmp/provision-times.log` at the end and stitch into the JSON above.
- Inside `mngr_lima/instance.py:384`, around each major call (`limactl_disk_create`, `limactl_start_new`, `_wait_for_cloud_init`, `wait_for_sshd`), log a span with `time.monotonic()` deltas. We already use `log_span`; can extend to emit a structured `{"event": "stage", "name": "...", "elapsed": ...}` line.

### 5c. Cold-cache vs warm-cache runs

The self-hosted runner is sticky, so the second and Nth runs are "warm" (lima image cached, host has the FCT clone, etc.). We want both numbers:

- Add a workflow input `cache_state: cold | warm` that, when `cold`, runs `mac-runner-reset.sh` (already exists) plus `rm -rf ~/.lima/_images/ ~/.cache/uv ~/.npm /tmp/minds-clone-*` before the run.
- Schedule one nightly `cold` run and N (4? 8?) `warm` runs back-to-back so we get a distribution.

### 5d. Percentile distribution

- A separate `bench-fanout` workflow that re-runs `minds-launch-to-msg.yml` N times in series (concurrency group keeps it serial), collects each artifact, computes `p50 / p90 / p99` per stage, and writes a markdown summary back into the run.
- Output target: a `bench/` directory committed daily by a scheduled agent (similar to `mngr/changelog-consolidation-*`) that tracks regressions.

### 5e. Define the success metric

Two numbers, both reported per run:

- `time_to_DONE`: button-press to AgentCreationStatus DONE.
- `time_to_first_reply`: button-press to assistant tmux pane containing the expected substring.

The latter is what users actually feel.

## 6. Quantified experiment list (one week each, ordered by ROI)

| # | Experiment | Expected win | Ship size | Complexity | Risk |
|---|---|---|---|---|---|
| 1 | **Add per-sub-stage timing to `first-message-verify.sh` + FCT `extra_provision_command`**. No optimization, just measurement. Output: JSON artifact + a baseline number for every step. | 0s | 0MB | low | none -- pure instrumentation |
| 2 | **Ship raw Ubuntu cloudimg in `Resources/lima/images/`** + point lima.yaml `images:` at the bundled `file://` path. Lima already accepts file URLs. | 60-180s on first VM | +400-600MB | low | bundle invalidates on Ubuntu image bump; codesign/notarize gets slower |
| 3 | **Bundle FCT clone** in `Resources/template/` and default `repo_source` to it on fresh installs. Auto-update via a periodic `git pull` task. | 20-60s per agent on cold cache, 5-15s warm | +50-100MB | low | bundled FCT goes stale; fix via `git pull` on launch |
| 4 | **Parallelize lima boot and host-side FCT clone.** `start_creation` already runs in a thread; can spawn the clone and `mngr create` substep in parallel and join before the VM needs the workspace dir. | 10-30s per agent | 0MB | low | clone failure now races with VM boot; need careful error aggregation |
| 5 | **Pre-pull Ubuntu image + clone FCT on first app launch**, behind a one-time consent dialog. Status banner in main window while running. | 60-240s for the first agent on first run | 0MB (uses cache) | medium | UX: needs progress in main window; bandwidth: needs consent gate |
| 6 | **In-VM parallelization**: split `extra_provision_command` into independent chains that run via `bash -c '... & ... & wait'` or two ssh sessions. | 30-60s per agent | 0MB | medium | apt-get database locking; need to confirm uv install and npm install don't conflict on /tmp |
| 7 | **Pre-provisioned qcow2** baked per minds release, containing Ubuntu + apt deps + uv + nodejs + latchkey CLI already installed. Ship as `Resources/lima/images/minds-golden-<ver>.qcow2`. | 90-300s per agent | +1-2GB | medium | bake pipeline; FCT version drift; |
| 8 | **Hot-VM-on-first-launch** (background): pre-create one lima VM in the background after the app's first successful login. Next "Create agent" snapshots/forks it. | 90-180s per "first agent" | 0MB | high | lima doesn't support clean snapshot; "snapshot" really means "keep a 2nd qcow2 around"; RAM hit while idle |
| 9 | **Fully baked qcow2** with vendor/mngr uv tools + system_interface frontend pre-built + wheels cached. | 4-7 min per agent | +2-4GB | high | tightest coupling between minds and FCT versions; biggest bundle |

If you only run three experiments this week, run #1 (measurement), #2 (raw cloudimg ship), and #4 (parallelize host clone with VM boot). #1 gives ground truth, #2 is a low-risk first-launch win, #4 buys a free 10-30s.

---

Open questions to resolve as we measure:

- Does `uv tool install` inside the VM hit a CDN per dep or use a packed wheel set? If the wheels we already ship in `Resources/wheels/` can be pushed into the VM via the bind-mount, that's a free win.
- Does `cloud-init` actually do anything load-bearing for us, or can we delete `_wait_for_cloud_init` and shave 5-30s? The provisioning script we control runs *after* cloud-init.
- Is the lima cloudimg cache shared across hosts, or per-host? If shared (default lima behaviour), the per-agent download cost on a warm Mac is genuinely zero today.
- How much of the 60-300s post-DONE first-message latency is claude TUI startup (which we can't speed up) vs tmux session bring-up vs system_interface settling? Need to attribute before we can attack it.
