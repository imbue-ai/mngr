Add `flock` (the `util-linux` package) to the Lima VM provisioning script's required-package check.

`flock` now backs mngr's unified cross-actor host lock and the in-host idle-shutdown watcher, so it must be present on Lima hosts. It is already present on the standard Debian images Lima uses, so this only installs it on minimal/custom images that lack it.
