Add a SMOLVM launch mode: workspaces can now run in smolvm microVMs (libkrun; KVM on Linux, Hypervisor.framework on macOS) with sub-second VM boots.

The mode uses the same FCT docker image as docker mode (built via the smolvm provider's --dockerfile pipeline and cached by image id) and backs /mngr with a btrfs data disk, so host_backup's btrfs_local snapshots work without any privileged steps. Requires a smolvm build with data-disk support on PATH; the provider's capability check guards hosts without one.

smolvm workspaces are shutdown-capable like docker and lima ones: the landing page Start/Stop controls and the quit-time stop prompt apply to them, so an idle-stopped smolvm workspace can be restarted from the UI.
