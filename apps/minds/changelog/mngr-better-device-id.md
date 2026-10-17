minds now owns its device id instead of freeloading on the mngr local provider's `host_id` file.

The id lives at `<minds data_dir>/device_id` and is read-or-created atomically at `minds run` startup, so an install has a real identity from its very first session. Previously the id was read (once, at startup) from `<mngr_host_dir>/host_id`, a file only mngr's local provider creates during discovery -- so fresh installs ran their whole first session with an empty device id: absent-host tombstoning was skipped with a warning, and locally-hosted workspace records were pushed with empty `hosting_device_id` provenance.

On upgrade, the existing mngr `host_id` value is adopted (copied; the original file is left in place), so previously-synced workspace records stay attributed to the install. Fresh installs mint a new `HostId`-shaped id. If the device id file cannot be read, created, or validated, `minds run` exits with a clear error naming the file instead of running without identity.

This removes minds' last dependency on the mngr local provider (a prerequisite for disabling that provider later).
