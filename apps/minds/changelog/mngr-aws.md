# minds

`_build_mngr_create_command` now passes `--vultr-region=<region>` instead of `--vps-region=<region>` to the inner `mngr create --provider vultr` subprocess. The shared `--vps-*` build-args prefix was retired in this branch and the Vultr provider now rejects it with a migration error, so the CLOUD launch mode would otherwise fail on every host creation. The accompanying unit-test assertions are updated to the new prefix.
