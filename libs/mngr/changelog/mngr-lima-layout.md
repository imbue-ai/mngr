- `ByteSize` is the new shared primitive for go-units byte sizes (`512m`, `4GiB`); `DockerMemorySize` refines it and lima's disk sizes build on it, so the grammar lives once.

- The journald cap the VPS hosts apply (`SystemMaxUse=512M`, change-guarded) moved into `providers/ssh_host_setup.py` as `build_cap_journald_command`, so the lima provider applies the same snippet.

- The backend-scoped host resolver behind `mngr docker resize` (discover only that backend's providers, refuse when none is enabled) moved to `cli/backend_hosts.py` and is shared with `mngr lima resize`.

- The hosts concept doc's "Sizing" section names `mngr lima resize` alongside the imbue_cloud and docker commands.
