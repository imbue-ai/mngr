# The pre-baked Lima image (the "fast Lima path")

## The problem

Creating an agent needs an isolated Linux machine with a full toolchain: a pinned Python, `uv`, Node, Claude Code, a checkout of the template repo, and a long list of apt packages. On a Mac, "a Linux machine" means a virtual machine.

Building that toolchain *inside* the VM takes 10-20 minutes, and every user on every machine independently re-derives the same identical result.

So we build it once, freeze the VM's disk into a file, publish that file, and let each user download the finished disk and boot it. That frozen disk is the **pre-baked image**, and this document is how it is produced, published, fetched, and booted.

Measured on the real 20 GiB image (`minds-v0.3.6`, aarch64): Lima reaches `READY` from the pre-baked image in **7.5 seconds**, against 10-20 minutes to build the same toolchain in-VM. The guest arrives with `uv`, `claude`, Python 3.12, the default-workspace-template checkout at the release tag, its `.venv`, and a ~1 GB pre-warmed uv cache already in place.

Three consequences follow from the idea, and they account for nearly all the code:

- **The image is large** (~5 GB), and most of it is unchanged between releases. So it is chunked and only the difference is transferred -- **desync**.
- **The image boots and executes code** on the user's machine, so it must be trusted. A manifest naming its SHA-256 is signed with **minisign**; the app ships only the public key.
- **The image must be usable as a disk** by whatever runs the VM. That turns out to require nothing: see [Why the image is raw](#why-the-image-is-raw).

## What runs where

The VM's contents are Linux and irrelevant here. What matters is that something *on macOS* has to create and boot the VM. That is **Lima** (`limactl`), a native Mac binary, and it is why the desktop app bundles host-side tools at all:

| binary | runs on | job |
|---|---|---|
| `limactl` | macOS host | creates and boots the Linux VM |
| `desync` | macOS host | assembles the image from the chunk store |
| `uv`, `git`, `restic` | macOS host | Python env, cloning, backups |

None of these runs inside the VM. See [desktop-app.md](./desktop-app.md) for how they are staged and signed.

Lima can drive a VM with either QEMU or **vz** (Apple's Virtualization.framework). On Apple Silicon it defaults to vz, so no QEMU is involved in running the VM -- `limactl` links `Virtualization.framework` directly.

## Why the image is raw

A disk image is stored in some file format. Two matter:

- **raw** -- a byte-for-byte dump. Byte X of the disk is byte X of the file.
- **qcow2** -- QEMU's format, with internal tables so only written clusters are stored.

The image is published, downloaded, stored, and booted as **raw**, with no conversion anywhere. Two independent reasons:

**Lima consumes raw directly.** `limactl` embeds `go-qcow2reader` and a pure-Go `nativeimgutil`. Its `proxyimgutil` prefers a `qemu-img` binary but falls back to the Go implementation when one is absent (`exec.ErrNotFound`), and `EnsureDisk` auto-detects the base disk's format (raw, qcow2, or asif). With no `vmType` pinned -- which is what `lima_yaml.py` generates -- Lima selects vz and creates a **raw** `basedisk` and a **raw** `diffdisk`. Verified by booting a VM from a raw base disk with `qemu-img` absent from `PATH`: it reached `READY` with a working guest, and neither disk carried the qcow2 magic.

**Raw is not bigger.** The filesystem already thin-provisions it. A raw image is a *sparse file*: regions never written consume no blocks. Only the apparent size is the full disk size.

```
raw apparent : 20G     # ls -lh -- the length the file claims
raw on disk  : 4.9G    # du -h  -- blocks actually allocated
```

On the real 20 GiB image, the sparse raw occupies **4.9 GiB** on disk against **5.1 GiB** for the equivalent qcow2. qcow2's L1/L2 and refcount tables, plus its 64 KiB cluster granularity, cost more than the filesystem's 4 KiB-granular holes.

Raw is also what desync chunks (qcow2's metadata churn would wreck chunk dedup) and what the signed manifest hashes, so the bytes the signature covers are exactly the bytes Lima boots.

The apparent size is not what anyone downloads. desync stores chunks compressed, so the 20 GiB image (4.9 GiB of real data) becomes a **1.7 GiB** chunk store -- that is the cost of a first install. An upgrade transfers only the chunks that changed, seeded from the image already on disk.

An earlier design converted the assembled raw to qcow2 with a bundled `qemu-img`, which Lima then converted straight back to raw. The app bundles no `qemu-img`.

## Publishing (operator, once per release)

```bash
./scripts/build-lima-image.sh --default-workspace-template-ref "$VERSION"
```

Boots a Lima VM, installs the toolchain inside it, shuts it down, and flattens the VM's disk into an image. This runs on a maintainer's machine, where a Homebrew `qemu-img` is available -- it is a bake-time tool and never ships.

```bash
uv run python scripts/lima_image/publish.py --version "$VERSION" --arch aarch64 \
  --raw-image scripts/lima_image/output-*/mngr-lima-*.raw --bucket ... --secret-key-file ...
```

This chunks the raw image into a content-addressed store, merges this arch's entry into the release's root manifest, signs the manifest with minisign, and uploads chunks + index + manifest + signature to R2. Chunks are content-addressed, so re-publishing a near-identical image uploads only what changed.

### Key custody

The minisign **private key is the trust anchor for code execution**: the app verifies the signature and then boots the image as a VM. Whoever holds that key can hand every user an image the app will run.

So, per [release.md](./release.md)'s one-time tier setup, which is the authoritative runbook:

- **One keypair per tier.** A leaked dev key must not be able to sign something a production app will execute.
- The private key lives **in a password manager or on the operator's machine -- never in the repo, never in CI**, and there is no reason to make it machine-readable: `publish.py` takes it as a local `--secret-key-file`, used by a human a few times a year.
- Only the **public** half and the base URL are committed, into the tier's `client.toml`. Both are public values.
- Generate it unencrypted (`minisign -G -W`), which is what `publish.py` needs for non-interactive signing.

Two infrastructure facts are load-bearing, and both were learned by publishing a real image rather than by reading Cloudflare's docs:

- **Upload over the S3 API, never the REST object API.** `api.cloudflare.com` allows 1200 requests per 5 minutes globally, and one image is ~65,000 chunks. A publish cannot fit in that budget; it dies partway through with `429`. R2's S3 API has no such ceiling and sustains ~12,000 objects/minute.
- **Serve from a custom domain, never `r2.dev`.** The same arithmetic bites on the way back down: a client extract fetches ~65,000 chunks, and the managed `r2.dev` origin is rate-limited, so the extract fails with `unexpected status code 429` and the image never assembles. A custom domain goes through Cloudflare's CDN and is not throttled. This applies to *every* tier, dev included -- it is not a production-only refinement.

## Consuming (the app, on each user's Mac)

`imbue/minds/lima_image/ensure.py` is the whole client, and `ensure_current_lima_image()` reads as the mirror image of the publish step:

1. Fetch the root manifest and **verify its minisign signature**. A 404 means nothing is published for this release: report `VERSION_UNAVAILABLE` and build in-VM.
2. Assemble the raw image from the chunk store with desync, **seeded by the currently-installed image** so only changed chunks are downloaded. Resumable in place.
3. Hash the assembled image and compare against the **signed** manifest. A mismatch raises rather than proceeding.
4. Install it: rename the assembled file into the version directory (a same-filesystem rename, so it is atomic and preserves sparseness), commit the current-image pointer, then prune the previous version.

The previous image is the desync seed *in place* -- it is not copied -- so it must outlive step 2 and is removed only by the prune in step 4. A failed or corrupt download leaves the current image, its index, and the pointer intact.

The create path then points Lima at the local file through the provider's existing per-arch image setting:

```
-S providers.lima.default_image_url_aarch64=<cache>/versions/<version>/AARCH64/image.raw
```

which lands in the Lima YAML as `images: - location: <path>`.

## When the fast path applies

`should_use_prebaked_lima_image()` requires **all** of:

- the Lima launch mode,
- a configured source (see below),
- not the local-worktree dev loop,
- the kill switch unset,
- the **default** template repo (a custom repo would not match the baked toolchain),
- the branch/tag equal to the **current release tag** (the image is baked per release tag).

Anything else falls back to building in-VM, which is correct: the baked image would not be what was asked for.

## Verifying the whole chain without a CDN

The publish and consume halves can be exercised end to end against a local HTTP origin, with the real `desync` and the real signature verification -- no R2, no ToDesktop, no config change. This is how to check the pipeline after touching any of it:

1. Flatten a baked image to raw, and `desync make` it into a chunk store + index (what `publish.py` uploads).
2. Write a `RootManifest` naming the raw image's SHA-256, and sign it with `minisign -S`.
3. Serve that directory over `http://127.0.0.1`, and call `ensure_current_lima_image()` against it with `HttpxManifestFetcher` / `PythonMinisignSignatureVerifier` / `DesyncImageChunkStore`.
4. Assert `READY`, and that the delivered file's SHA-256 equals the published one.
5. Point `limactl` at the delivered `image.raw` and boot it.

`test_lima_image_e2e.py` does exactly this with a small fixture image on every test run. The same recipe works unchanged against a real multi-GB baked image, which is the useful thing to do before a release: it proves everything except the CDN hosting itself, and the CDN is only serving the same bytes.

When driving the client outside the Electron app, set `MINDS_DESYNC_BINARY` to the bundled `desync` -- that is what `backend.js` does. Without it the client falls back to a bare `desync` PATH lookup and fails on a machine that has no system-wide desync.

## Failure modes worth knowing

- **The feature is off unless the tier configures it.** With `lima_image_base_url` or the public key unset, `make_lima_image_source()` returns `None` and every create silently builds in-VM. This is the default, and it is why the fast path can appear "broken" when it is simply not switched on.
- **No published image for this release+arch** reports `VERSION_UNAVAILABLE` and builds in-VM. Publishing for a tag the shipped binary does not request (it requests `FALLBACK_BRANCH`) has the same effect as not publishing at all.
- **A non-default workspace never uses the image**, by design -- a custom repo or branch would not match the baked toolchain.
- **A published-but-unfetchable image is a hard, retryable error**, not a silent fallback: a tampered or truncated download must not quietly become a slow build.

## Configuring a tier

The source is built from two **public** values in the tier's `client.toml`:

```toml
lima_image_base_url = "https://..."       # public base URL of the chunk store
lima_image_minisign_public_key = "RW..."  # the trust anchor
```

If either is unset, `make_lima_image_source()` returns `None`, the gate returns `False`, and the app builds in-VM. This is the single switch that turns the whole feature on for a tier, and it is safe to leave off: an unconfigured tier simply takes the slow path.
