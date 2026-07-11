# The pre-baked Lima image (the "fast Lima path")

## The problem

Creating an agent needs an isolated Linux machine with a full toolchain: a pinned Python, `uv`, Node, Claude Code, a checkout of the template repo, and a long list of apt packages. On a Mac, "a Linux machine" means a virtual machine.

Building that toolchain *inside* the VM takes 10-20 minutes, and every user on every machine independently re-derives the same identical result.

So we build it once, freeze the VM's disk into a file, publish that file, and let each user download the finished disk and boot it. Roughly 15 minutes becomes roughly 2. That frozen disk is the **pre-baked image**, and this document is how it is produced, published, fetched, and booted (issue #2306).

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

An earlier design converted the assembled raw to qcow2 with a bundled `qemu-img`, which Lima then converted straight back to raw. The app bundles no `qemu-img`.

## Publishing (operator, once per release)

```bash
./scripts/build-lima-image.sh --fct-ref "$VERSION"
```

Boots a Lima VM, installs the toolchain inside it, shuts it down, and flattens the VM's disk into an image. This runs on a maintainer's machine, where a Homebrew `qemu-img` is available -- it is a bake-time tool and never ships.

```bash
uv run python scripts/lima_image/publish.py --version "$VERSION" --arch aarch64 \
  --raw-image scripts/lima_image/output-*/mngr-lima-*.raw --bucket ... --secret-key-file ...
```

This chunks the raw image into a content-addressed store, merges this arch's entry into the release's root manifest, signs the manifest with minisign, and uploads chunks + index + manifest + signature to R2. Chunks are content-addressed, so re-publishing a near-identical image uploads only what changed.

The signing **private key never leaves the operator's machine** and is never in CI. See [release.md](./release.md) for the full runbook, including the one-time per-tier setup.

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

## Configuring a tier

The source is built from two **public** values in the tier's `client.toml`:

```toml
lima_image_base_url = "https://..."       # public base URL of the chunk store
lima_image_minisign_public_key = "RW..."  # the trust anchor
```

If either is unset, `make_lima_image_source()` returns `None`, the gate returns `False`, and the app builds in-VM. This is the single switch that turns the whole feature on for a tier, and it is safe to leave off: an unconfigured tier simply takes the slow path.
