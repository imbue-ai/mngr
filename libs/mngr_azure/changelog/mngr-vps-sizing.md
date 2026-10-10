- `mngr list` reports an Azure host's real size (the VM size's vCPUs and RAM from the region's resource SKU catalog, and the OS disk size), recorded on the host record at create, instead of the 1 CPU / 1 GB placeholder. Hosts created before this release answer from a table of the VM sizes the Imbue Studio create form offers.

- The OS disk size knob is now `root_disk_size_gb` (shared with AWS and GCP). `os_disk_size_gb` keeps working as a deprecated alias; setting both to different values is a config error.

- The Azure release trip asserts the size `mngr list` reports matches what the SKU catalog says the VM size is.
