- `mngr list` reports a GCP host's real size (the machine type's vCPUs and RAM from the GCE machine-type catalog, and the boot disk size), recorded on the host record at create, instead of the 1 CPU / 1 GB placeholder. The describe needs `compute.machineTypes.get`, now listed with the per-host create permissions. Hosts created before this release answer from a table of the machine types the Imbue Studio create form offers.

- The boot disk size knob is now `root_disk_size_gb` (shared with AWS and Azure). `boot_disk_size_gb` keeps working as a deprecated alias; setting both to different values is a config error.

- The GCP release trip asserts the size `mngr list` reports matches what GCE says the machine type is.
