`mngr create -b --gcp-root-disk-gb=N` sizes the boot disk of that host alone, overriding the provider config's `root_disk_size_gb` (which stays the default); the size is recorded on the host's shape.
