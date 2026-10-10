- `mngr list` reports an OVH host's real size (the vCores, RAM, and disk of the VPS model OVH reports), recorded on the host record at create, instead of the 1 CPU / 1 GB placeholder. Hosts created before this release on the default `vps-2025-model1` plan answer from a table; any other pre-existing plan lists the size as unknown.

- The OVH create release test asserts the size `mngr list` reports matches what OVH says the VPS model has.
