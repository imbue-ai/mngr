The container-setup and host-setup notes that still described the retired lima slice path (the slice guest's lima `additionalDisk`, the `lima_slice` provisioning module as the consumer of the pinned docker stack, and the lima-2.2.0 framing of the docker-ce pin rationale) now describe the qemu slice VM and its baked guest image.

The pinned docker install script is private to `host_setup.py` again (`_PINNED_DOCKER_INSTALL_SCRIPT`): the guest-image bake that consumed the exported `PINNED_DOCKER_INSTALL_SCRIPT` was the deleted lima slice provisioning.

The fully rendered bookworm apt versions (`PINNED_DOCKER_APT_VERSION`, `PINNED_CONTAINERD_APT_VERSION`) are removed: their only consumer was the deleted gen-1 box prep. The `*_CORE` pins stay and every install renders the distro suffix from the target's own os-release.
