# Plan: Accelerate imbue_cloud slice bakes (build-once-per-box + load)

Accelerate imbue_cloud bare-metal slice bakes by building the forever-claude-template (FCT) Docker image **once per bare-metal box** and loading it into each slice's dockerd, instead of every slice rebuilding it from the Dockerfile. Also bake Playwright/Chromium into the cached image so the deferred first-boot install becomes a no-op. The whole change is **mngr-only / single repo** (no forever-claude-template change).

## Overview

- **Problem:** every slice bake runs an inner `mngr create --template main --template pool_host` that builds the FCT image from the Dockerfile inside the slice (10-20 min, network-bound; `pool_bake.py:60-62`), plus a per-slice deferred Playwright/Chromium install (capped 900s; `pool_bake.py:79-84`). This is the bulk of slice-bake latency and is paid per slice.
- **Key decision:** keep the build, but pay it **once per box**. The first bake on a box (per tag) builds + saves the image as a `docker save` tar on the box; subsequent bakes `docker load` that tar into their slice's dockerd. Target: ~25-35 min/slice of setup → a single ~1-3 min local `docker load`.
- **Build-vs-load decision lives entirely in the slice provider** (`SliceVpsDockerProvider`). The `pool_host` template's `build_arg` is untouched (it must keep building for non-default templates / other providers).
- **Box-local transfer:** the box runs `docker save`/`docker load` against its own `localhost:<vm_port>`, so the ~11 GiB never leaves the box, regardless of where the bake is orchestrated. Uses a **unique ephemeral SSH key per transfer**, destroyed afterward.
- **Cloud-only / no FCT change:** the load mechanism is pure mngr; Playwright is baked in cloud-side via a thin derived image during the seed step (`FROM <fct-built> ; RUN playwright install …`). FCT's shared scripts (used by the desktop Lima path) are not touched.
- **Scope:** pool bakes only. Lease-time slow-path rebuilds (`_rebuild_leased_container`) keep building. Caching applies only to production `--from-tag` bakes; dev `--workspace-dir` bakes always build.
- **Motivation:** this is a latency optimization for cloud slice provisioning; minimalism and low blast radius are explicit goals (single repo, opt-in realizer flag, self-healing where cheap, hard-fail where silent fallback would hide problems).

## Expected behavior

- **First production slice bake on a box** (for a given `minds-v<version>` tag): acquires the per-box lock, builds the FCT image as today, builds the Playwright-derived `fct:<tag>` image, `docker save`s it to the box tar, prunes the builder slice's build cache, releases the lock, then runs the container from `fct:<tag>`. Logs `Built + seeded box tar fct:<tag>`.
- **Subsequent production slice bakes on the same box (same tag):** block until the tar exists, then `docker load` it and run the container from `fct:<tag>` — no build, no Playwright download. Logs `Loaded fct:<tag> from box tar (Ns)`.
- **Concurrent bakes during the first build:** only the lock holder builds; others block-then-load (poll for the tar up to the create budget, ~1800s).
- **Dead builder:** a lock marker older than the TTL is reclaimable, so a crashed builder doesn't wedge the pool — the next bake promotes itself to builder.
- **Playwright is already present** in every loaded slice's image (`/root/.cache/ms-playwright` + the `done.playwright` marker), so `[program:deferred-install]` finds the marker and no-ops on first container boot. The first-boot ~900s install disappears for all cached slices.
- **Desktop (Lima) users are unaffected** — no FCT/script change; they still run `deferred-install` exactly as before.
- **New tag bake:** rebuilds, replaces the box tar, and prunes the prior tag's tar (single tag retained per box).
- **Dev `--workspace-dir` bakes:** always build from the Dockerfile (mutable content) and never read or write a box tar.
- **Failure modes:**
  - A failed `docker load` **hard-fails the bake** (no silent fallback to building); the slice is then torn down by the existing failure path.
  - A failed Playwright derived build retries 3× (exponential backoff) then **hard-fails the seed** (lock releases; next bake retries).
  - Insufficient box disk before a save **fails early** with a clear error (the pre-check never deletes to make room).
  - The ephemeral transfer key is always destroyed (box private key removed, slice `authorized_keys` entry removed) whether the transfer succeeds or fails.
- **OVH / vultr / aws docker providers:** behavior unchanged (the realizer's new skip-if-present is opt-in and off for them).

## Implementation plan

### `libs/mngr_vps` (realizer skip-if-present)

- `imbue/mngr_vps/data_types.py` — add `allow_local_image: bool = Field(default=False, …)` to `RealizePlacementContext` (alongside `base_image`/`docker_build_args` at `:44,46`). When true, an already-present local image is run as-is.
- `imbue/mngr_vps/container_setup.py` — add `image_exists(outer, image) -> bool` near `pull_image` (`:767-769`): wraps `run_docker(outer, ["image", "inspect", image])` and returns False on `MngrError`. No behavior change to `pull_image`/`run_container`.
- `imbue/mngr_vps/docker_realizer.py` — in `realize_placement` (the build-vs-pull branch at `:319-325`), short-circuit **before** build/pull: `if ctx.allow_local_image and image_exists(outer, ctx.base_image): pass  # already present → just run`. The build branch is unreachable here because the slice provider passes empty `docker_build_args` on the cache path.
- `imbue/mngr_vps/instance.py` — `create_host_on_existing_vps` (`:841-861`): add an `allow_local_image: bool = False` parameter, thread it into the `RealizePlacementContext(...)` construction. Default keeps OVH/vultr/aws unchanged.

### `libs/mngr_imbue_cloud` (orchestration + box cache + Playwright)

- **New interface + impls — box image cache** (so the orchestration is unit-testable without a real box):
  - `imbue/mngr_imbue_cloud/interfaces.py` (or extend) — `BoxImageCacheInterface(MutableModel, ABC)` with methods:
    - `try_acquire_build_lock(tag) -> bool` (atomic `mkdir` of a per-box lock dir; reclaims a marker older than the TTL).
    - `release_build_lock(tag) -> None`.
    - `wait_for_tar(tag, timeout) -> bool` (poll for the tar; returns False on timeout; detects builder death → caller may re-acquire).
    - `has_tar(tag) -> bool`.
    - `check_free_disk(required_bytes) -> None` (raises if insufficient; never deletes).
    - `save_image_from_slice(tag, vm_ssh_port, ephemeral_key) -> None` (box-local `ssh root@localhost:<port> docker save | atomic mv`; prune other-tag tars on success).
    - `load_image_into_slice(tag, vm_ssh_port, ephemeral_key) -> None` (box-local `cat tar | ssh root@localhost:<port> docker load`).
    - `clean_stale_tmp(tag) -> None`.
  - `imbue/mngr_imbue_cloud/slices/box_image_cache.py` — `LimaBoxImageCache` impl: runs all box-side commands through the existing `LimaSliceVpsClient._run_on_box` / `_box_ssh_command` (`lima_slice_client.py:139-172`). Tar dir `~/<lima_user>/.cache/mngr-slice-fct/` (sibling of `slice_base_image_path`, `bare_metal.py:75-80`); tar `fct-<sanitized-tag>.tar`; lock dir `…/.lock-<tag>.d`.
  - `imbue/mngr_imbue_cloud/slices/mock_box_image_cache_test.py` — in-memory `MockBoxImageCache` for unit tests (tracks tar presence, lock state, save/load calls, simulated failures).
- **Ephemeral transfer key:**
  - Box generates a unique keypair per transfer (`ssh-keygen` via `_run_on_box`), the orchestrator reads the public key and appends it to the slice's VM-root `authorized_keys` via the existing `outer` (`_make_outer_for_vps_ip`, `slice_provider.py:318-337`).
  - Save/load on the box uses the ephemeral private key against `localhost:<outer_ssh_port>` (the VM-root forwarded port; `lima_slice.py` guest `2200`).
  - Teardown in a `finally`: box removes the keypair; orchestrator removes the `authorized_keys` line — on success or failure.
- **Playwright cloud-side bake (seed path only):**
  - After the base FCT image is built on the builder slice, build a thin derived image tagged `fct:<tag>`:
    `FROM <base> ; RUN cd /docker_build_code && uv run playwright install --with-deps chromium && mkdir -p /var/lib/minds/deferred-install && touch /var/lib/minds/deferred-install/done.playwright`.
  - Build via `run_docker(outer, ["build", "-t", f"fct:{tag}", …])` on the slice; retry 3× with exponential backoff (tenacity), then hard-fail the seed.
  - `/root/.cache/ms-playwright` and `/var/lib/minds/deferred-install/done.playwright` are image layers outside `/mngr` and outside `/docker_build_code`, so they survive `fct_seed` and are inherited by every loaded slice (deferred-install no-ops).
- **Slice provider decision (`imbue/mngr_imbue_cloud/providers/slice_provider.py`):**
  - Add `fct_cache_tag: str | None` to `SliceVpsDockerProviderConfig` (`:45-117`) — set only for production `--from-tag` bakes.
  - In `create_host` (`:248-312`), after the VM + dockerd are up (`wait_for_sshd` at `:282`) and inside the `with … as outer:` (`:284`):
    - If `fct_cache_tag` is None (dev bake): unchanged — pass the template's build args (build path).
    - If set: ensure `fct:<tag>` is present in the slice dockerd:
      - `try_acquire_build_lock`: if acquired → build base (reuse `_build_image_on_vps` / `build_image_on_outer_from_build_args`) → build Playwright-derived `fct:<tag>` → `check_free_disk` → `save_image_from_slice` (atomic + prune) → `docker builder prune -af` on the slice → `release_build_lock`.
      - else → `wait_for_tar` then `load_image_into_slice`; on builder death, re-acquire and become builder; on timeout, hard-fail.
    - Then call `create_host_on_existing_vps(..., image=f"fct:{tag}", build_args=(), allow_local_image=True)` → realizer skip-present → run.
- **Bake orchestration (`imbue/mngr_imbue_cloud/cli/server.py`):**
  - `_build_slice_create_args` (`:472-530`): when the bake source is `--from-tag` (production), emit `-S providers.<inst>.fct_cache_tag=fct:<repo_branch_or_tag>` (tag from `BakeSource.repo_branch_or_tag` / advertised attributes, `data_types.py:61`, `server.py:638`). Omit for dev bakes.
  - Emit explicit info logs for the built-vs-loaded outcome.
- **Box prep (`imbue/mngr_imbue_cloud/slices/bare_metal_prep.py`):**
  - In `build_box_prep_script` (`:34-156`, near the cache-dir block `:103-114`): create + chown `~/.cache/mngr-slice-fct/`. No Docker on the box (it only holds/serves a tar file).

## Implementation phases

1. **Realizer skip-if-present (mngr_vps).** Add `allow_local_image` to `RealizePlacementContext`, `image_exists` helper, the skip branch in `realize_placement`, and the `create_host_on_existing_vps` parameter. Unit-test `image_exists` and the skip branch. System still behaves identically (flag off everywhere). Changelog: `mngr_vps`.
2. **Box image cache interface + impls.** `BoxImageCacheInterface`, `LimaBoxImageCache`, `MockBoxImageCache`. Box prep creates the cache dir. Unit-test lock acquire/reclaim, tar presence, disk pre-check, stale `.tmp` cleanup, single-tag prune (against the mock + a thin real-command-shape test).
3. **Load path (cache hit).** Slice provider reads `fct_cache_tag`; when the tar is present, load it (with ephemeral key lifecycle) and run via the present-image path. `cli/server.py` passes `fct_cache_tag` for from-tag bakes. End state: a box that already has a tar serves loads; a box without one still builds (phase 4 not yet wired) — so guard the load path behind "tar present" only.
4. **Seed path (cache miss) + Playwright bake.** Lock holder builds base → Playwright-derived `fct:<tag>` (with retries) → save (atomic, disk pre-check, prune) → builder-cache prune → release. Block-then-load for waiters; builder-death reclaim. End state: full build-once-per-box behavior.
5. **Polish + verification.** Explicit built-vs-loaded logging, ephemeral-key teardown hardened in `finally`, changelog entries for `mngr_imbue_cloud` + `mngr_vps`, two-slice real-box verification.

## Testing strategy

- **Unit (primary)** — `slice_provider_test.py` + `box_image_cache_test.py` against `MockBoxImageCache`:
  - Cache hit → load path invoked, no build; container runs `fct:<tag>`.
  - Cache miss → lock acquired, base+Playwright built, tar saved (atomic), builder cache pruned, lock released.
  - Lock contention → waiter blocks then loads once the tar appears.
  - Stale lock (marker > TTL) → reclaimed; builder death → waiter promotes and builds.
  - Disk pre-check fails → hard error, no save, nothing deleted.
  - Ephemeral key destroyed in `finally` on both success and failure (assert teardown calls).
  - Playwright derived build fails → 3 retries then hard-fail (assert no partial tar saved).
  - Dev `--workspace-dir` bake → build path, cache untouched (no save/load).
  - `--from-tag` bake → `fct_cache_tag` emitted by `_build_slice_create_args`.
- **mngr_vps unit** — `image_exists` true/false; `realize_placement` skips build+pull when `allow_local_image` and image present; unchanged when flag off.
- **Edge cases** — leftover `.tmp` from an interrupted save cleaned on next seed; single-tag prune removes a prior tag's tar; re-cut tag (documented limitation — stale tar served).
- **Real-box verification (manual, the e2e bar)** — bake ≥2 production slices on a real box at a `minds-v*` tag: confirm slice #1 logs `Built + seeded`, slice #2 logs `Loaded … from box tar`, slice #2 boots a working agent (launch-to-msg-style round-trip), and record the wall-clock saved. No automated acceptance/release test (genuinely hard to exercise without real bare metal).

## Open questions

- **Seeding slice image identity (chosen: unified):** the builder slice runs `fct:<tag>` (with Playwright) via the present path, so even the first slice skips deferred-install. Simpler alternative: let the realizer build+run `mngr-build-<host_id>` normally and save a separate `fct:<tag>` — then only the first slice on a box runs deferred-install. Confirm the unified approach is worth the extra wiring.
- **Disk pre-check sizing:** measure the slice image size (`docker image inspect` size) before save, or use a fixed conservative threshold (e.g. require ≥15 GiB free)? Measured is more accurate; fixed is simpler.
- **Lock TTL vs build time:** TTL = create budget (1800s). A ~20 min build + save could approach it; confirm the waiter timeout exceeds worst-case build+save (or set waiter timeout > builder TTL with margin).
- **Ephemeral key generation site (chosen: box-generated):** box runs `ssh-keygen` so the private key never leaves the box; orchestrator only handles the public key. Confirm vs orchestrator-generated.
- **vultr / aws remote-docker:** they also build the FCT Dockerfile remotely and could reuse the realizer skip-if-present + a per-host tar cache later. Out of scope for this plan — flag as a future follow-up.
- **Multi-release on one box:** single-tag policy assumes a box serves one active release at a time. If a box must host slices from multiple minds envs/releases simultaneously, the box would need N tars (bounded GC) — revisit if that becomes a real deployment.
