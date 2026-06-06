Switched the default Lima VM image from Ubuntu 24.04 to a pinned Debian 12
"bookworm" genericcloud image (both `aarch64` and `x86_64`). Now that the agent
typically runs inside a Docker container in the VM (`is_host_in_docker`), the VM
only needs Docker + btrfs + sshd, so a lighter base suffices; this also mirrors
the OVH provider's Debian 12 base. The provisioning script is apt-based and works
on Debian unchanged. Override per-arch via `providers.lima.default_image_url_*`.

Added `providers.lima.default_container_run_args` (default empty): extra
arguments appended to the `docker run` that starts the agent container in
`is_host_in_docker` mode. This is the only config path for injecting inner-
container `docker run` flags on Lima (the lima template's `start_arg` maps to
`limactl` VM args, not the container). Pairs with `docker_runtime="runsc"` to run
the agent container under gVisor -- e.g. set it to
`["--workdir=/", "--security-opt=no-new-privileges"]`, the same hardening the
docker provider applies, where `--workdir=/` avoids runsc aborting when the image
WORKDIR (inside the mounted volume) already exists as the process cwd.
