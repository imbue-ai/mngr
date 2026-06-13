Add the `mngr_smolvm` provider plugin: hosts are smolvm machines (libkrun microVMs; KVM on Linux, Hypervisor.framework on macOS) with sub-second boots.

mngr provisions sshd inside the guest over the smolvm exec channel (apk/apt install on first boot, pre-injected ed25519 host key, root client key) and connects over a forwarded localhost port, so all standard mngr commands work unchanged.

Two storage layouts mirroring the lima provider's flag: the default exposes host_dir to the host machine via virtiofs (offline file reads work while the VM is stopped); `is_host_data_volume_exposed=false` attaches a smolvm-managed btrfs data disk at host_dir, giving consistent unprivileged btrfs snapshots inside the guest (the layout minds workspaces need). The btrfs layout is capability-gated: stock smolvm builds get a clear "requires data-disk support" error.

Image sources: bare Alpine VM (default, no image pull), an OCI reference (`--image`), an existing `.smolmachine` pack (`-b "--from PATH"`), or a locally built docker image via `-b "--image-archive PATH"` (a `docker save` tarball, converted to a content-hash-cached pack).

Idle hosts stop themselves via smolvm's poweroff sentinel. Snapshots and rename are unsupported (lima parity).
