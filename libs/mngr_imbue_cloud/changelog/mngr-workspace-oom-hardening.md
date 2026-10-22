The gen-2 VM's meminfo publisher (`mngr-publish-container-meminfo.service`) reports headroom that earlyoom can act on, and no longer grows gVisor's memory as it runs.

- MemAvailable in `/mngr-vol/.host-meminfo` is now the cgroup limit minus usage plus only the inactive file cache, the same signal the kubelet evicts on. It used to add the active file cache and reclaimable slab too. Near the limit those are what the kernel thrashes rather than frees, so the published figure could not drop below about 5% even when the cgroup was out of memory, and earlyoom's SIGKILL threshold never fired before the cgroup OOM killer took the whole sandbox.

- The publisher no longer creates a new file on every rewrite. gVisor keeps an entry for every distinct inode the sandbox sees, for the sandbox's lifetime, so each fresh copy earlyoom read added one: up to about 345,000 a day per workspace. Each rewrite now goes, with fixed-width fields, into one of two slot files beside it (`.host-meminfo.slot0` and `.slot1`), alternately and in place, and that slot is renamed onto `.host-meminfo` through a hard link. The sandbox only ever sees those two inodes, and a reader still always sees a complete file. A slot is recreated if it is missing, in an older format, or something the container planted there.

Existing VMs pick both up from `minds-admin install-vm-memory-units --apply`.
