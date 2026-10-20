Gen-2 slice VMs now keep the workspace container's memory cap and its earlyoom in step with what gVisor actually sees.

- The bake and the slow-path rebuild cap the container from the VM's own MemTotal, the value its every-boot reconcile oneshot computes. They used the RAM qemu gives the guest, about 241 MiB more, so a baked container's first reboot shrank its cap.

- The reconcile oneshot runs before `minds-autostart.service` and restarts a container docker had already started. gVisor fixes a sandbox's MemTotal when it starts, and `docker update` changes only the host cgroup, so a sandbox started first believed in memory it no longer had.

- A new VM service, `mngr-publish-container-meminfo.service`, publishes each container cgroup's limit and headroom into its volume, at `/mngr-vol/.host-meminfo`, four times a second, for earlyoom inside to read with `--host-meminfo` (earlyoom v1.9.0-imbue.3, which a later default-workspace-template release pins along with the flag): the sandbox's own `/proc/meminfo` cannot see the 0.3-0.9 GB gVisor itself is charged, so earlyoom's 10% threshold could arrive only after the cgroup had killed the whole sandbox.
