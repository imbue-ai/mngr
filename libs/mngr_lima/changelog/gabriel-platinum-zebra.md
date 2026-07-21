The lima provider now supports resizing a host's CPU and memory (via `mngr limit --cpus/--memory` and the minds settings page):

- Desired values persist on the durable host record and are applied with `limactl edit` during `start_host` (the only point the VM is guaranteed stopped), so a VM always boots with exactly the configured values. Actual values are probed exactly from `limactl list --json`; a configured/actual discrepancy means the values apply on the next restart.

- Resize capabilities report the machine's physical CPU/memory as advisory ceilings (requests above them warn but are allowed).

- Fixed create-time resource recording: the recorded resources now reflect what the VM actually booted with (probed from limactl) instead of only what the YAML config said, so `--cpus`/`--memory` start args are no longer ignored in the record.
