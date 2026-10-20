Docs: remote workspaces run their container under gVisor (`runsc`) with `/run` and `/tmp` on tmpfs from the bake; `docs/workspace/README.md` documents what does not work inside the sandbox (ptrace tooling, eBPF, FUSE, io_uring, nested container runtimes, unusual ioctls) and the filesystem-metadata slowdown. `docs/deploy/host-pool-setup.md` follows the 20 GiB gen-2 boot disk and describes the runsc gen-2 bake plus the dev-only `pool create --docker-runtime runc` comparison override; `docs/deploy/gen2-telemetry.md` lists the new `SLICE_UNIT_OOM_KILLED` signal and the collector's `systemd` journal scan.

- `docs/deploy/host-pool-setup.md` documents the `baking` pool status, the orphan reap's running/age guards, and `pool reap-orphans`.

- `docs/deploy/host-pool-setup.md` describes the gen-2 slice disk layout (10 GiB boot, 16 + 3.5 GiB/unit data disk, the single workspace quota and its 4 GiB system reserve).
